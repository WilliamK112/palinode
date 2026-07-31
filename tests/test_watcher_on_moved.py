"""the watcher has no on_moved handler work: a rename must reconcile both paths, not
silently orphan the old one.

Chunk ids are derived from the file path, so before ``on_moved`` existed a
``mv`` left every chunk (and every entity row) stranded under the old path.
Real SQLite on tmp_path; the embedder is patched so the destination indexes.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from watchdog.events import FileMovedEvent

from palinode.core import store
from palinode.core.config import config
from palinode.indexer.watcher import PalinodeHandler

_VEC = [0.04] * 1024


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.capture.cross_refs, "enabled", False)
    monkeypatch.setattr(config.auto_summary, "enabled", False)
    store.init_db()
    return tmp_path


def _chunks_for(path: str) -> int:
    db = store.get_db()
    n = db.execute(
        "SELECT count(*) FROM chunks WHERE file_path = ?", (path,)
    ).fetchone()[0]
    db.close()
    return n


def _entities_for(path: str) -> set[str]:
    db = store.get_db()
    rows = db.execute(
        "SELECT entity_ref FROM entities WHERE file_path = ?", (path,)
    ).fetchall()
    db.close()
    return {r["entity_ref"] for r in rows}


def test_rename_moves_chunks_and_entities_to_the_new_path(tmp_store):
    projects = tmp_store / "projects"
    projects.mkdir()
    src = projects / "old.md"
    dest = projects / "new.md"
    src.write_text(
        "---\nid: proj-x\ncategory: projects\nentities:\n- person/alice\n---\n\n"
        "# X\n\nA fact worth indexing.\n"
    )

    handler = PalinodeHandler()
    try:
        with patch("palinode.core.embedder.embed", return_value=_VEC):
            handler._process_file(str(src))
            assert _chunks_for(str(src)) >= 1
            assert _entities_for(str(src)) == {"person/alice"}

            # The rename on disk, then the event watchdog would deliver.
            src.rename(dest)
            handler.on_moved(FileMovedEvent(str(src), str(dest)))

        # Old path fully retired, new path fully indexed.
        assert _chunks_for(str(src)) == 0, "old path chunks must not survive a rename"
        assert _entities_for(str(src)) == set(), "old path entity rows must be gone"
        assert _chunks_for(str(dest)) >= 1
        assert _entities_for(str(dest)) == {"person/alice"}
    finally:
        handler.shutdown()
