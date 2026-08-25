"""Tests for the daily-files-dominate-search-results penalty.

Rewritten for the double-daily-penalty fix: hybrid search applied the daily penalty TWICE (once
inside ``store.search`` — the inner vector-arm fetch — and again in
``ranker.rank_hybrid``), so a daily file configured for ``daily_penalty =
0.3`` actually scored ``0.3 * 0.3 = 0.09``. The old version of this file
patched ``palinode.core.store.search`` — the exact function that contained
penalty application #1 — so its stub returned unpenalised scores and the
test observed exactly one penalty and asserted that was correct. It was not
mocking near the bug; it was replacing half of it.

Post-fix, ``rank_hybrid`` is the ONLY place a score is mutated (``store``'s
copy of the context-boost/daily-penalty blocks is deleted, and the
vector-only path now routes through ``rank_hybrid`` too, via
``store.search_hybrid(..., use_fts=False)``). So:

- The ranker-level tests below call ``ranker.rank_hybrid`` directly on plain
  dicts — no DB, no mocking of ``store.search`` / ``store.search_fts`` /
  ``store.get_db`` — following the pattern in ``tests/test_ranker.py``. A
  single set of these now covers what used to need separate "hybrid" and
  "vector" copies, because both paths run the same ranker.
- ``TestPenaltyAppliedExactlyOnceEndToEnd`` proves the fix end-to-end through
  the real orchestrator (``store.search_hybrid``, both the hybrid and
  ``use_fts=False`` vector-only entry points) against a real SQLite store
  (repo rule: never mock the DB) — the level at which the original bug
  actually lived, and the level no test exercised.
"""
from __future__ import annotations

import math
import os

import pytest

from palinode.core import ranker, store
from palinode.core.config import config
from tests._store_helpers import upsert_chunks


# ---------------------------------------------------------------------------
# Ranker-level tests (plain dicts, no DB) — pattern from tests/test_ranker.py
# ---------------------------------------------------------------------------

def _res(path, *, score=0.5, section="root", metadata=None, **extra):
    r = {"file_path": path, "section_id": section, "score": score, "id": path}
    if metadata is not None:
        r["metadata"] = metadata
    r.update(extra)
    return r


@pytest.fixture(autouse=True)
def _decay_and_context_off(monkeypatch):
    # Isolate the penalty stage from decay re-rank / context boost, exactly
    # as tests/test_ranker.py's own autouse fixture does. daily_penalty is
    # deliberately left alone here — it's the thing under test.
    monkeypatch.setattr(config.decay, "enabled", False)
    monkeypatch.setattr(config.context, "enabled", False)


def _run(vec, fts=(), **kw):
    params = dict(top_k=10, threshold=0.0, hybrid_weight=0.5, priority_weight=0.025)
    params.update(kw)
    return ranker.rank_hybrid(vec, list(fts), **params)


def _order(results):
    return [r["file_path"] for r in results]


def test_is_daily_file():
    """_is_daily_file should match daily/ paths in various forms."""
    assert ranker._is_daily_file("daily/2026-04-12.md") is True
    assert ranker._is_daily_file("/home/user/palinode/daily/2026-04-12.md") is True
    assert ranker._is_daily_file("projects/daily-standup.md") is False
    assert ranker._is_daily_file("decisions/use-daily-builds.md") is False
    assert ranker._is_daily_file("daily/notes/misc.md") is True


def test_daily_penalty_config_default():
    """SearchConfig should have daily_penalty default of 0.3."""
    assert config.search.daily_penalty == 0.3


def test_daily_penalty_demotes_daily_files(monkeypatch):
    """Daily files should be penalized below real memories, in both arms."""
    monkeypatch.setattr(config.search, "daily_penalty", 0.3)
    daily = _res("daily/2026-04-12.md")   # rank 0 in both arms, would win without penalty
    real = _res("projects/palinode.md")   # rank 1 in both arms
    out = _run([daily, real], [daily, real])
    assert len(out) == 2
    assert _order(out)[0] == "projects/palinode.md"
    assert _order(out)[1] == "daily/2026-04-12.md"
    assert out[1]["score"] < out[0]["score"]


def test_include_daily_fully_skips_the_penalty(monkeypatch):
    """include_daily=True must return the daily file at its UNPENALISED
    score — not a partially-penalised one. This is the double-daily-penalty
    corollary: the pre-fix bug meant include_daily=True only skipped the
    ranker's copy of the penalty, because store.search had already demoted
    (and re-sorted) the candidates before rank_hybrid ever saw them.
    """
    daily = _res("daily/2026-04-12.md")
    real = _res("projects/palinode.md")

    monkeypatch.setattr(config.search, "daily_penalty", 1.0)
    baseline = _run([daily, real], [daily, real])
    baseline_daily_score = next(r["score"] for r in baseline if r["file_path"] == daily["file_path"])

    monkeypatch.setattr(config.search, "daily_penalty", 0.3)
    out = _run([daily, real], [daily, real], include_daily=True)
    assert _order(out)[0] == "daily/2026-04-12.md"
    penalized_daily_score = next(r["score"] for r in out if r["file_path"] == daily["file_path"])
    assert penalized_daily_score == pytest.approx(baseline_daily_score), (
        "include_daily=True must match the no-penalty baseline exactly — "
        "any deviation means some fraction of a penalty is still being applied"
    )


def test_daily_penalty_one_means_no_penalty(monkeypatch):
    """daily_penalty=1.0 should be a no-op (no score change, no re-sort)."""
    monkeypatch.setattr(config.search, "daily_penalty", 1.0)
    daily = _res("daily/2026-04-12.md")
    real = _res("projects/palinode.md")
    out = _run([daily, real], [])
    assert _order(out)[0] == "daily/2026-04-12.md"


def test_no_daily_files_unaffected(monkeypatch):
    """When no daily files are present, the penalty stage is a no-op."""
    monkeypatch.setattr(config.search, "daily_penalty", 0.3)
    a = _res("projects/palinode.md")
    b = _res("decisions/adr-001.md")
    out = _run([a, b], [])
    assert _order(out) == ["projects/palinode.md", "decisions/adr-001.md"]


def test_daily_penalty_absolute_path(monkeypatch):
    """Daily penalty should work with absolute file paths containing /daily/."""
    monkeypatch.setattr(config.search, "daily_penalty", 0.3)
    daily = _res("/home/user/palinode/daily/2026-04-12.md")
    real = _res("/home/user/palinode/projects/palinode.md")
    out = _run([daily, real], [])
    assert _order(out)[0] == "/home/user/palinode/projects/palinode.md"


def test_penalty_applied_exactly_once_not_squared(monkeypatch):
    """Direct reproduction of the double-daily-penalty defect shape at the
    ranker level: the penalized score must equal the unpenalized (baseline) score times the
    configured penalty exactly ONCE. Pre-fix (penalty applied twice
    somewhere upstream and again here) this ratio would come out as
    penalty**2 (0.09), not penalty (0.3).
    """
    daily = _res("daily/2026-04-12.md")
    real = _res("projects/palinode.md")

    monkeypatch.setattr(config.search, "daily_penalty", 1.0)
    baseline = _run([daily, real], [daily, real])
    baseline_by_path = {r["file_path"]: r["score"] for r in baseline}

    monkeypatch.setattr(config.search, "daily_penalty", 0.3)
    penalized = _run([daily, real], [daily, real])
    penalized_by_path = {r["file_path"]: r["score"] for r in penalized}

    assert penalized_by_path["daily/2026-04-12.md"] == pytest.approx(
        baseline_by_path["daily/2026-04-12.md"] * 0.3
    ), "daily file's score must be baseline * penalty exactly once, not squared"
    assert penalized_by_path["daily/2026-04-12.md"] != pytest.approx(
        baseline_by_path["daily/2026-04-12.md"] * 0.3 * 0.3
    ), "regression guard: this is what the doubled-penalty bug produced"
    # A non-daily file's score must be completely unaffected by the penalty.
    assert penalized_by_path["projects/palinode.md"] == pytest.approx(
        baseline_by_path["projects/palinode.md"]
    )


# ---------------------------------------------------------------------------
# End-to-end: real SQLite store, both store.search_hybrid entry points
# ---------------------------------------------------------------------------

EMBED_DIM = 1024


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


def _query_embedding() -> list[float]:
    return _normalize([1.0] + [0.0] * (EMBED_DIM - 1))


def _near_embedding(noise: float = 0.05) -> list[float]:
    """A vector close to, but distinct from, the query embedding — a
    genuine second candidate that isn't a perfect match."""
    base = [1.0] + [0.0] * (EMBED_DIM - 1)
    base[1] = noise
    return _normalize(base)


def _index_chunk(*, chunk_id: str, file_path: str, content: str, embedding: list[float]) -> None:
    from datetime import UTC, datetime
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    upsert_chunks([{
        "id": chunk_id,
        "file_path": file_path,
        "section_id": None,
        "category": "insights",
        "content": content,
        "metadata": {},
        "created_at": now_iso,
        "last_updated": now_iso,
        "embedding": embedding,
    }])


@pytest.fixture()
def _real_store(tmp_path, monkeypatch):
    """Real SQLite store in tmp_path — no mocked DB, no mocked search
    functions (repo rule). Same pattern as
    tests/test_hybrid_threshold_rank_artifact.py."""
    memory_dir = str(tmp_path)
    db_path = os.path.join(memory_dir, ".palinode.db")
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", db_path)
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.decay, "enabled", False)
    monkeypatch.setattr(config.context, "enabled", False)
    store._db_checked = False
    os.makedirs(os.path.join(memory_dir, "insights"), exist_ok=True)
    os.makedirs(os.path.join(memory_dir, "daily"), exist_ok=True)
    store.init_db()

    query_emb = _query_embedding()
    # Content has no overlap with the query text below, so the FTS arm
    # contributes nothing and this exercises the vector arm in isolation —
    # same isolation trick as test_hybrid_threshold_rank_artifact.py.
    _index_chunk(
        chunk_id="daily-1", file_path="daily/2026-08-09.md",
        content="unrelated filler alpha", embedding=query_emb,
    )
    _index_chunk(
        chunk_id="real-1", file_path="insights/palinode-overview.md",
        content="unrelated filler beta", embedding=_near_embedding(),
    )
    yield query_emb
    store._db_checked = False


class TestPenaltyAppliedExactlyOnceEndToEnd:
    """Proves the double-daily-penalty fix at the level the bug actually
    lived: the real ``store.search_hybrid`` orchestrator, not a mocked
    stand-in for it."""

    def _scores(self, query_emb, **kw):
        results = store.search_hybrid(
            query_text="something with no term in common with any chunk",
            query_embedding=query_emb,
            top_k=10,
            threshold=0.0,
            record_access=False,
            **kw,
        )
        return {r["file_path"]: r["score"] for r in results}

    def test_hybrid_path_exactly_once(self, monkeypatch, _real_store):
        query_emb = _real_store

        monkeypatch.setattr(config.search, "daily_penalty", 1.0)
        baseline = self._scores(query_emb)

        monkeypatch.setattr(config.search, "daily_penalty", 0.3)
        penalized = self._scores(query_emb)

        assert penalized["daily/2026-08-09.md"] == pytest.approx(
            baseline["daily/2026-08-09.md"] * 0.3
        ), "hybrid path: daily score must be baseline * penalty exactly once"
        assert penalized["insights/palinode-overview.md"] == pytest.approx(
            baseline["insights/palinode-overview.md"]
        ), "hybrid path: a non-daily file's score must be untouched by the penalty"

        include_daily = self._scores(query_emb, include_daily=True)
        assert include_daily["daily/2026-08-09.md"] == pytest.approx(
            baseline["daily/2026-08-09.md"]
        ), "include_daily=True must return the daily file at its unmodified score"

    def test_vector_only_path_exactly_once(self, monkeypatch, _real_store):
        """The use_fts=False path — what the API's hybrid=false search now
        routes through instead of calling store.search directly."""
        query_emb = _real_store

        monkeypatch.setattr(config.search, "daily_penalty", 1.0)
        baseline = self._scores(query_emb, use_fts=False)

        monkeypatch.setattr(config.search, "daily_penalty", 0.3)
        penalized = self._scores(query_emb, use_fts=False)

        assert penalized["daily/2026-08-09.md"] == pytest.approx(
            baseline["daily/2026-08-09.md"] * 0.3
        ), "vector-only path: daily score must be baseline * penalty exactly once"
        assert penalized["insights/palinode-overview.md"] == pytest.approx(
            baseline["insights/palinode-overview.md"]
        ), "vector-only path: a non-daily file's score must be untouched by the penalty"

        include_daily = self._scores(query_emb, use_fts=False, include_daily=True)
        assert include_daily["daily/2026-08-09.md"] == pytest.approx(
            baseline["daily/2026-08-09.md"]
        ), "vector-only path: include_daily=True must return the unmodified score"
