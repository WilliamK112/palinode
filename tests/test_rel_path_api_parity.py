"""API-level regression coverage for `rel_path` (server-side path relativization).

MCP used to relativize absolute paths client-side by string-splitting on the
hardcoded literal "/palinode/", which silently returned the untouched
absolute path for any memory_dir without that substring — the configuration
every custom install actually has. The fix moves relativization to the API,
which is the one surface that actually knows `config.memory_dir`: every
payload that carries a filesystem path now also carries `rel_path` (or, for
`/topic-coverage`, alongside `best_match`), additive to the existing field.

Every fixture here deliberately points `memory_dir` at a directory with
*neither* a "palinode" substring *nor* a repeated path segment — the exact
shape the old hardcoded-literal approach failed silently on — so a
regression back to guessing the relative path from a literal would show up
as an absolute path (or a wrong split) in these assertions.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from palinode.api import server as srv
from palinode.api.server import app
from palinode.core.config import config
from tests._store_helpers import upsert_chunks

DIM = 1024
_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _hash_dim(token: str) -> int:
    h = hashlib.sha256(token.encode()).digest()
    return int.from_bytes(h[:4], "big") % DIM


def fake_embed(text: str) -> list[float]:
    """Deterministic bag-of-words embedder (mirrors test_embedding_tools.py)."""
    tokens = _tokens(text)
    if not tokens:
        return []
    vec = [0.0] * DIM
    counts = Counter(tokens)
    for tok, count in counts.items():
        idx = _hash_dim(tok)
        vec[idx] += math.sqrt(count)
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return []
    return [v / norm for v in vec]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient rooted at a memory_dir with no "palinode" substring."""
    memory_dir = tmp_path / "second-brain"
    memory_dir.mkdir()
    db_path = memory_dir / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(memory_dir))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)

    from palinode.core import embedder
    monkeypatch.setattr(embedder, "embed", lambda text, backend="local": fake_embed(text))

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _ingest(tmp_path_memory_dir, rel_path: str, content: str) -> None:
    """Insert a chunk with an absolute file_path, mirroring what /save does."""
    import os

    full = os.path.join(str(tmp_path_memory_dir), rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    chunk_id = hashlib.sha256(full.encode()).hexdigest()[:32]
    upsert_chunks(
        [
            {
                "id": chunk_id,
                "file_path": full,
                "section_id": "root",
                "category": rel_path.split("/", 1)[0] if "/" in rel_path else "",
                "content": content,
                "embedding": fake_embed(content),
                "metadata": {},
                "created_at": "2026-04-26T00:00:00Z",
                "last_updated": "2026-04-26T00:00:00Z",
            }
        ],
        skip_unchanged=False,
    )


def test_save_returns_rel_path_alongside_file_path(client):
    resp = client.post(
        "/save",
        json={"content": "A saved note.", "type": "Insight", "slug": "rel-path-save"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["rel_path"] == "insights/rel-path-save.md"
    # Additive: file_path is untouched and still absolute.
    assert data["file_path"].endswith("insights/rel-path-save.md")
    assert data["file_path"] != data["rel_path"]


def test_search_results_carry_rel_path(client):
    memory_dir = config.memory_dir
    _ingest(memory_dir, "decisions/rel-path-search.md", "Content about hybrid search ranking.")

    resp = client.post(
        "/search",
        json={"query": "hybrid search ranking", "threshold": 0.0, "limit": 5},
    )
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) >= 1
    assert results[0]["rel_path"] == "decisions/rel-path-search.md"
    assert results[0]["file_path"] != results[0]["rel_path"]


def test_search_associative_results_carry_rel_path(client):
    memory_dir = config.memory_dir
    abs_path = f"{memory_dir}/people/rel-path-alice.md"
    rows = [{"file_path": abs_path, "content": "Discussion with Alice.", "score": 0.9}]

    # Stub the store call directly (mirrors test_search_associative_snippet.py)
    # rather than depending on the entity-graph mechanics — this test is
    # about response shaping, not entity spreading activation.
    with patch.object(srv.store, "search_associative", return_value=rows):
        resp = client.post(
            "/search-associative",
            json={"query": "alice", "seed_entities": ["person/alice"]},
        )
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 1
    assert results[0]["rel_path"] == "people/rel-path-alice.md"
    assert results[0]["file_path"] == abs_path


def test_dedup_suggest_returns_rel_path(client):
    memory_dir = config.memory_dir
    _ingest(memory_dir, "projects/rel-path-dedup.md", "deployment rollout staging procedures")

    resp = client.post(
        "/dedup-suggest",
        json={"content": "deployment rollout staging procedures", "min_similarity": 0.0, "top_k": 5},
    )
    assert resp.status_code == 200
    results = resp.json()
    assert results
    assert results[0]["rel_path"] == "projects/rel-path-dedup.md"


def test_orphan_repair_returns_rel_path(client):
    memory_dir = config.memory_dir
    _ingest(memory_dir, "projects/rel-path-orphan.md", "notes about the broken wikilink target")

    resp = client.post(
        "/orphan-repair",
        json={"broken_link": "broken wikilink target", "min_similarity": 0.0, "top_k": 5},
    )
    assert resp.status_code == 200
    results = resp.json()
    assert results
    assert results[0]["rel_path"] == "projects/rel-path-orphan.md"


def test_topic_coverage_returns_rel_path(client):
    memory_dir = config.memory_dir
    _ingest(memory_dir, "insights/rel-path-topic.md", "machine learning deployment pipeline notes")

    resp = client.post(
        "/topic-coverage",
        json={"query": "machine learning deployment pipeline", "min_similarity": 0.0},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["covered"] is True
    assert data["rel_path"] == "insights/rel-path-topic.md"
    assert data["best_match"] != data["rel_path"]


def test_save_rel_path_correct_for_repeated_segment_memory_dir(tmp_path, monkeypatch):
    """A memory_dir whose own basename repeats (.../palinode/palinode) — the
    other failure mode named in the issue: the old rsplit split at the wrong
    occurrence."""
    memory_dir = tmp_path / "palinode" / "palinode"
    memory_dir.mkdir(parents=True)
    db_path = memory_dir / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(memory_dir))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)

    from palinode.core import embedder
    monkeypatch.setattr(embedder, "embed", lambda text, backend="local": fake_embed(text))

    with TestClient(app, raise_server_exceptions=True) as c:
        resp = c.post(
            "/save",
            json={"content": "note", "type": "Insight", "slug": "rel-path-repeated"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["rel_path"] == "insights/rel-path-repeated.md"
