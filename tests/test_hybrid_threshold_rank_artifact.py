"""End-to-end regression coverage for the hybrid-search post-RRF-threshold
investigation — one root cause behind three symptoms: thresholds that are
rank artifacts in disguise, a search result count that silently saturates
regardless of the requested limit, and a default limit tuned assuming that
ceiling didn't exist.

A production measurement found that ``rank_hybrid``'s post-RRF fused score
is a function of an item's RANK within RRF's k=60 formula, not of its
relevance: two semantically unrelated queries against the same store
produced byte-identical post-RRF score sequences. The measured consequence:
``POST /search`` with ``hybrid: true`` silently saturated at a rank-locked
result count no matter how large ``limit`` was. The pure-ranker
characterisation in ``tests/test_ranker.py`` proves the mechanism on plain
dicts; this file proves it end-to-end through ``store.search_hybrid`` and
the FastAPI ``/search`` endpoint, against a real SQLite store (no mocked DB —
repo rule). Only the embedder is bypassed by inserting deterministic unit
vectors directly, the same pattern as ``test_recall_feedback.py`` /
``test_telemetry_recall_exclusion.py``.

The fix: ``rank_hybrid``'s ``threshold`` now filters each candidate's OWN
per-arm score (real cosine for the vector arm, normalized BM25 for the FTS
arm) BEFORE fusion, instead of the fused/RRF score after. The
``search.default_limit`` bump (10→15, see ``config.py``) is safe once this
rank-locked ceiling is gone.
"""
from __future__ import annotations

import math
import os
import random

import pytest

from palinode.core import store
from palinode.core.config import config

EMBED_DIM = 1024


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


def _query_embedding() -> list[float]:
    """The fixed point every cluster chunk is a small perturbation of."""
    return _normalize([1.0] + [0.0] * (EMBED_DIM - 1))


def _cluster_embedding(seed: int, noise: float = 0.02) -> list[float]:
    """A vector genuinely, substantially similar to :func:`_query_embedding`
    — cosine ~0.93-0.94 at the default noise — not merely rank-adjacent to
    it. Distinct per seed so 120 of these are 120 distinct chunks, not exact
    duplicates."""
    rng = random.Random(seed)
    base = [1.0] + [0.0] * (EMBED_DIM - 1)
    vec = [v + rng.uniform(-noise, noise) for v in base]
    return _normalize(vec)


def _index_chunk(*, chunk_id: str, file_path: str, content: str, embedding: list[float]) -> None:
    from datetime import UTC, datetime
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    store.upsert_chunks([{
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


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Point config at a fresh tmp dir + real SQLite DB. No git, no Ollama."""
    memory_dir = str(tmp_path)
    db_path = os.path.join(memory_dir, ".palinode.db")
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", db_path)
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.decay, "enabled", False)
    store._db_checked = False
    os.makedirs(os.path.join(memory_dir, "insights"), exist_ok=True)
    store.init_db()
    yield memory_dir
    store._db_checked = False


def test_search_hybrid_not_capped_at_rank_locked_ceiling():
    """Direct reproduction of the saturation defect against a real store:
    120 chunks, each genuinely relevant to the query (real cosine well above
    the representative 0.6 floor used below; this fixture deliberately stays
    in the "clearly
    above any plausible threshold" band since it's testing the CARDINALITY
    mechanism, not calibration), content deliberately absent of the search
    terms so the FTS arm contributes nothing (the single-list-only shape
    that still produces a rank-locked ceiling — see the pure-ranker
    characterisation). Request top_k=100.
    """
    query_emb = _query_embedding()
    cosines = []
    for i in range(120):
        emb = _cluster_embedding(i)
        cosine = sum(a * b for a, b in zip(query_emb, emb, strict=True))
        cosines.append(cosine)
        _index_chunk(
            chunk_id=f"c{i}", file_path=f"insights/c{i}.md",
            content=f"unrelated filler content number {i}",
            embedding=emb,
        )
    # Sanity: the scenario is genuinely "everything is relevant", not an
    # artifact of a lenient threshold — every chunk clears 0.6 on its own
    # real cosine score.
    assert min(cosines) >= 0.6, f"weakest cluster member only {min(cosines):.3f} cosine — widen noise/seed count"

    results = store.search_hybrid(
        query_text="something with no term in common with any chunk",
        query_embedding=query_emb,
        top_k=100,
        threshold=0.6,
    )
    assert len(results) == 100, (
        f"got {len(results)} — top_k should be the only cardinality control "
        "once every candidate clears the per-arm relevance floor; a count "
        "stuck well below top_k despite 120 relevant chunks being available "
        "is the hybrid-search saturation defect"
    )


class TestSearchApiExplicitZeroThreshold:
    """The API `or`-bug regression: `threshold=0.0 or api_threshold` treats
    the caller's explicit "no floor" as falsy and silently reinstates the
    default. The saturation-defect environment note that surfaced this said
    "threshold: 0.0" was set explicitly and the ceiling still fired."""

    @pytest.fixture()
    def api(self, monkeypatch):
        import importlib

        from fastapi.testclient import TestClient

        for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
            monkeypatch.delenv(_k, raising=False)
        import palinode.api.server as srv
        srv = importlib.reload(srv)

        # A chunk with real cosine near zero to the query — well below
        # api_threshold but a legitimate hit once the floor is genuinely
        # disabled. Compared against config.search.api_threshold directly
        # below rather than a hardcoded literal, so this stays correct
        # whatever that value is measured to (see config.py's SearchConfig
        # docstring).
        below_default = _cluster_embedding(seed=1, noise=1.3)
        below_default = _normalize(below_default)
        query_emb = _query_embedding()
        cosine = sum(a * b for a, b in zip(query_emb, below_default, strict=True))
        assert cosine < config.search.api_threshold, (
            f"fixture cosine {cosine:.3f} is not below api_threshold — "
            "widen the noise so the test actually exercises threshold=0.0"
        )
        _index_chunk(
            chunk_id="below-default", file_path="insights/below-default.md",
            content="weakly related filler", embedding=below_default,
        )
        monkeypatch.setattr(
            "palinode.core.embedder.embed", lambda *_a, **_k: query_emb
        )
        srv._rate_counters.clear()
        with TestClient(srv.app, raise_server_exceptions=True) as c:
            yield c
        srv._rate_counters.clear()

    def test_explicit_zero_threshold_actually_disables_the_floor(self, api):
        res = api.post(
            "/search",
            json={"query": "anything", "threshold": 0.0, "limit": 20, "hybrid": True},
        )
        assert res.status_code == 200, res.text
        paths = {r["file_path"] for r in res.json()}
        assert "insights/below-default.md" in paths, (
            "threshold=0.0 was requested explicitly — a candidate below the "
            "default api_threshold must still surface. If this fails, "
            "`req.threshold or config.search.api_threshold` is silently "
            "treating the explicit 0.0 as unset."
        )

    def test_omitted_threshold_still_uses_the_default(self, api):
        """The other half of the `or`→`is not None` fix: when the caller
        genuinely omits `threshold`, the default must still apply — this
        isn't a "disable thresholding entirely" regression."""
        res = api.post("/search", json={"query": "anything", "limit": 20, "hybrid": True})
        assert res.status_code == 200, res.text
        paths = {r["file_path"] for r in res.json()}
        assert "insights/below-default.md" not in paths
