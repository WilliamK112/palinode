"""SQLite hygiene in ``palinode.core.store`` (WAL + indexes).

``init_db()`` turns on WAL journaling and adds the ``chunks(file_path)`` /
``chunks(created_at)`` indexes; ``get_db()`` opens with a busy timeout. WAL is
persistent, so a pre-existing DELETE-journal database migrates on the next
``init_db()`` with no schema-version bookkeeping.

The concurrency test is real ``multiprocessing`` against a real ``tmp_path``
database — the failure it guards against (``database is locked`` escaping a
writer while a reader holds a long transaction) only exists across OS-level
connections.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
from pathlib import Path

import pytest

from palinode.core import store
from palinode.core.config import config


@pytest.fixture(autouse=True)
def _reset_db_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "_db_checked", False)


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    return path


def _index_names(db: sqlite3.Connection) -> set[str]:
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'chunks'"
    ).fetchall()
    return {r[0] for r in rows}


def test_init_db_enables_wal_and_creates_chunk_indexes(db_path: Path) -> None:
    store.init_db()

    db = store.get_db()
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == int(
            store.DB_BUSY_TIMEOUT_SECONDS * 1000
        )
        assert {"idx_chunks_file_path", "idx_chunks_created_at"} <= _index_names(db)
    finally:
        db.close()


def test_init_db_is_idempotent(db_path: Path) -> None:
    store.init_db()
    store.init_db()

    db = store.get_db()
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert {"idx_chunks_file_path", "idx_chunks_created_at"} <= _index_names(db)
    finally:
        db.close()


def test_pre_existing_delete_journal_db_migrates_on_init(db_path: Path) -> None:
    """A database created before WAL + indexes shipped (DELETE journal, no chunk indexes)
    gains WAL + indexes on the next ``init_db()`` and keeps its rows."""
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE chunks (
            id TEXT PRIMARY KEY, file_path TEXT NOT NULL, section_id TEXT,
            category TEXT, content TEXT NOT NULL, metadata JSON,
            created_at TEXT, last_updated TEXT
        )
        """
    )
    legacy.execute(
        "INSERT INTO chunks (id, file_path, content, created_at) VALUES (?, ?, ?, ?)",
        ("c1", "/m/a.md", "hello", "2026-01-01T00:00:00Z"),
    )
    legacy.commit()
    assert legacy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert not {"idx_chunks_file_path", "idx_chunks_created_at"} & _index_names(legacy)
    legacy.close()

    store.init_db()

    db = store.get_db()
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert {"idx_chunks_file_path", "idx_chunks_created_at"} <= _index_names(db)
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
        # The planner actually uses the new indexes on the two hot shapes.
        plan = " ".join(
            r[3] for r in db.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM chunks WHERE file_path = ?", ("/m/a.md",)
            )
        )
        assert "idx_chunks_file_path" in plan
        plan = " ".join(
            r[3] for r in db.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM chunks ORDER BY created_at DESC LIMIT 5"
            )
        )
        assert "idx_chunks_created_at" in plan
    finally:
        db.close()


def test_read_only_uri_connection_still_works_under_wal(db_path: Path) -> None:
    """doctor's checks open ``file:...?mode=ro`` — WAL must not break that,
    both while a writer connection is open (sidecars present) and after all
    connections close (sidecars checkpointed away)."""
    store.init_db()

    writer = store.get_db()
    try:
        writer.execute(
            "INSERT INTO chunks (id, file_path, content) VALUES ('c1', '/m/a.md', 'x')"
        )
        writer.commit()
        assert os.path.exists(f"{db_path}-wal")
        ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            assert ro.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                ro.execute("INSERT INTO chunks (id, file_path, content) VALUES ('c2', 'p', 'y')")
        finally:
            ro.close()
    finally:
        writer.close()

    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    try:
        assert ro.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
    finally:
        ro.close()


def test_get_db_names_the_fix_when_extension_loading_is_unavailable(
    db_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Python built without loadable-extension support raises
    ``AttributeError`` from ``enable_load_extension``; surface it as a
    ``RuntimeError`` that says what to do instead of failing deep in the first
    query."""
    class _NoExtConn:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real
            self.closed = False

        def close(self) -> None:
            self.closed = True
            self._real.close()

        def __getattr__(self, name: str):
            if name == "enable_load_extension":
                raise AttributeError(name)
            return getattr(self._real, name)

    real_connect = sqlite3.connect
    made: list[_NoExtConn] = []

    def _connect(*args, **kwargs):
        conn = _NoExtConn(real_connect(*args, **kwargs))
        made.append(conn)
        return conn

    monkeypatch.setattr(store.sqlite3, "connect", _connect)
    with pytest.raises(RuntimeError, match="enable-loadable-sqlite-extensions"):
        store.get_db()
    assert made and made[0].closed


# --- multi-process concurrency ---------------------------------------------

def _writer_proc(db_path: str, dims: int, prefix: str, n: int, errors) -> None:
    from palinode.core import store as _store
    from palinode.core.config import config as _config

    _config.db_path = db_path
    _config.embeddings.primary.dimensions = dims
    _store._db_checked = True
    try:
        for i in range(n):
            with _store.transaction() as db:
                _store.write_chunk_row(
                    db.cursor(),
                    chunk_id=f"{prefix}-{i}",
                    file_path=f"/m/{prefix}/{i}.md",
                    section_id="root",
                    category="test",
                    content=f"{prefix} content {i}",
                    metadata_json="{}",
                    content_hash=f"h{prefix}{i}",
                    meta_hash="m",
                    created_at="2026-01-01T00:00:00Z",
                    last_updated="2026-01-01T00:00:00Z",
                    embedding=[float(i % 7)] * dims,
                )
    except Exception as exc:  # pragma: no cover - reported via queue
        errors.put(f"{prefix}: {type(exc).__name__}: {exc}")


def _reader_proc(db_path: str, dims: int, rounds: int, errors) -> None:
    from palinode.core import store as _store
    from palinode.core.config import config as _config

    _config.db_path = db_path
    _config.embeddings.primary.dimensions = dims
    _store._db_checked = True
    try:
        for _ in range(rounds):
            db = _store.get_db()
            try:
                db.execute("SELECT count(*) FROM chunks").fetchone()
                db.execute(
                    "SELECT id FROM chunks_vec WHERE embedding MATCH ? AND k = 5",
                    (json.dumps([1.0] * dims),),
                ).fetchall()
            finally:
                db.close()
    except Exception as exc:  # pragma: no cover - reported via queue
        errors.put(f"reader: {type(exc).__name__}: {exc}")


def test_concurrent_writers_and_reader_across_processes(
    db_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dims = 8
    monkeypatch.setattr(config.embeddings.primary, "dimensions", dims)
    store.init_db()

    ctx = multiprocessing.get_context("spawn")
    errors = ctx.Queue()
    n = 60
    procs = [
        ctx.Process(target=_writer_proc, args=(str(db_path), dims, "a", n, errors)),
        ctx.Process(target=_writer_proc, args=(str(db_path), dims, "b", n, errors)),
        ctx.Process(target=_reader_proc, args=(str(db_path), dims, 200, errors)),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
    assert all(p.exitcode == 0 for p in procs), [p.exitcode for p in procs]

    failures = []
    while not errors.empty():
        failures.append(errors.get_nowait())
    assert failures == []

    db = store.get_db()
    try:
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2 * n
        assert db.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == 2 * n
    finally:
        db.close()
