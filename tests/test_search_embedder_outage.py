"""Regression: /search must surface an embedder outage as an error, not a
silent empty result set.

Before this fix, `embedder.embed()` returned `[]` on a backend failure and
`search_api` treated that identically to "the embedder is fine but this query
happens to have no results" (`if not query_emb: return []`) — HTTP 200 with an
empty list, indistinguishable from a genuine no-match. Every route in
`palinode/api/routers/search.py` already wraps its body in a broad
`except Exception as e: raise _safe_500(e, ...)`, so once `embed()` raises
instead of returning falsy, the failure now surfaces as a proper 500 with the
real cause logged server-side — no code change needed in the routers
themselves, which is the point of fixing the contract at the boundary.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from palinode.api import server as srv
from palinode.api.server import app
from palinode.core.config import config
from palinode.core.embedder import EmbeddingUnavailable


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    srv._rate_counters.clear()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    srv._rate_counters.clear()


def _boom(text, backend="local"):
    raise EmbeddingUnavailable(
        backend="local", model="bge-m3", text_len=len(text),
        cause="connection refused",
    )


def test_search_surfaces_500_instead_of_silent_empty_result(client):
    with patch("palinode.core.embedder.embed", side_effect=_boom):
        res = client.post("/search", json={"query": "does this exist"})
    assert res.status_code == 500
    assert res.json()["detail"] == "Search failed"


def test_dedup_suggest_surfaces_500_instead_of_silent_empty_result(client):
    with patch("palinode.core.embedder.embed", side_effect=_boom):
        res = client.post("/dedup-suggest", json={"content": "some draft content"})
    assert res.status_code == 500
    assert res.json()["detail"] == "Dedup suggest failed"


def test_search_empty_query_bypasses_embedder_entirely(client):
    """Recency-only mode must not even reach the embedder — confirms the 500
    above is caused by the embed call, not by test-fixture DB absence."""
    with patch("palinode.core.embedder.embed", side_effect=_boom):
        res = client.post("/search", json={"query": ""})
    assert res.status_code == 200
    assert res.json() == []
