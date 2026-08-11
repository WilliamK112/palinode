"""E2E regression coverage: MCP tool output stays memory-relative for a
``PALINODE_DIR`` with no "palinode" substring and no repeated segment.

Same in-process MCP -> API -> SQLite -> filesystem harness as
``test_mcp_e2e.py`` (``httpx.MockTransport`` shim over FastAPI's
``TestClient``), but ``isolated_env`` here deliberately roots memory_dir at a
directory shaped like the issue's own examples (``second-brain``,
``srv/notes``) — the configuration the old ``rsplit("/palinode/", 1)``
client-side split silently failed on for every custom install, since its
fallback returned the absolute path unchanged whenever that literal wasn't
present.

Only the confirmation/rendering text is asserted here (not the underlying
API response shape — see ``tests/test_rel_path_api_parity.py`` for that);
this file is the "does a human/agent actually see a leaked absolute path"
check.
"""
from __future__ import annotations

import asyncio
import os
from unittest import mock

import httpx
import pytest
from fastapi.testclient import TestClient

from palinode.core.config import config

EMBED_DIM = 1024


def _fake_embed(text: str, backend: str = "local") -> list[float]:
    return [0.1] * EMBED_DIM


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    """memory_dir with no "palinode" substring anywhere in its path."""
    memory_dir = str(tmp_path / "second-brain")
    db_path = os.path.join(memory_dir, ".palinode.db")

    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", db_path)
    monkeypatch.setattr(config.git, "auto_commit", False)

    for d in ("people", "projects", "decisions", "insights", "research", "inbox", "daily"):
        os.makedirs(os.path.join(memory_dir, d), exist_ok=True)

    from palinode.core import store
    store.init_db()

    with (
        mock.patch("palinode.core.embedder.embed", side_effect=_fake_embed),
        mock.patch("palinode.api.server._generate_description", return_value="Test description"),
        mock.patch("palinode.api.server._generate_summary", return_value=""),
    ):
        yield memory_dir


@pytest.fixture()
def api_tc(isolated_env):
    from palinode.api.server import app, _rate_counters
    _rate_counters.clear()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def dispatch(api_tc):
    """Invoke ``_dispatch_tool`` in-process, routed through the TestClient."""

    def _mock_transport_handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        raw_query = url.query
        if isinstance(raw_query, bytes):
            raw_query = raw_query.decode("latin-1")
        path_with_qs = url.path + ("?" + raw_query if raw_query else "")
        tc_resp = api_tc.request(
            method=request.method,
            url=path_with_qs,
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx.Response(
            status_code=tc_resp.status_code,
            headers=dict(tc_resp.headers),
            content=tc_resp.content,
        )

    async def _call(name: str, arguments: dict) -> list:
        from palinode.mcp import _dispatch_tool

        class _InProcessAsyncClient(httpx.AsyncClient):
            def __init__(self, **kwargs):
                kwargs.pop("transport", None)
                super().__init__(
                    transport=httpx.MockTransport(_mock_transport_handler),
                    **kwargs,
                )

        with mock.patch("palinode.mcp.httpx.AsyncClient", _InProcessAsyncClient):
            return await _dispatch_tool(name, arguments)

    return _call


def _run(coro):
    return asyncio.run(coro)


def _index_file(fp: str, content: str, category: str) -> None:
    from palinode.core import store
    chunks = [{
        "id": f"mcp-e2e-relpath-{os.path.basename(fp)}",
        "file_path": fp,
        "section_id": None,
        "category": category,
        "content": content,
        "metadata": {},
        "created_at": "2026-04-26T00:00:00Z",
        "last_updated": "2026-04-26T00:00:00Z",
        "embedding": _fake_embed("x"),
    }]
    store.upsert_chunks(chunks)


def test_save_confirmation_is_relative_not_absolute(dispatch, isolated_env):
    """palinode_save's "Saved to ..." message must not leak the absolute path."""
    result = _run(dispatch("palinode_save", {
        "content": "Save confirmation must stay relative.",
        "type": "Insight",
        "slug": "relpath-save-target",
    }))
    text = result[0].text
    assert not text.startswith("Error")
    assert not text.startswith("Save failed")
    assert "Saved to insights/relpath-save-target.md" in text
    assert isolated_env not in text


def test_search_result_path_is_relative_not_absolute(dispatch, api_tc, isolated_env):
    resp = api_tc.post("/save", json={
        "content": "Palinode uses hybrid BM25+vector search for relpath test.",
        "type": "Insight",
        "slug": "relpath-search-target",
    })
    assert resp.status_code == 200
    fp = resp.json()["file_path"]
    assert fp.startswith(isolated_env)  # sanity: API still returns absolute file_path

    _index_file(fp, "Palinode uses hybrid BM25+vector search for relpath test.", "insights")

    result = _run(dispatch("palinode_search", {
        "query": "hybrid search relpath",
        "threshold": 0.0,
        "limit": 5,
    }))
    text = result[0].text
    assert "insights/relpath-search-target.md" in text
    assert isolated_env not in text


def test_dedup_suggest_result_path_is_relative_not_absolute(dispatch, api_tc, isolated_env):
    resp = api_tc.post("/save", json={
        "content": "Duplicate content for relpath dedup suggest test alpha beta gamma.",
        "type": "Insight",
        "slug": "relpath-dedup-target",
    })
    assert resp.status_code == 200
    fp = resp.json()["file_path"]
    _index_file(fp, "Duplicate content for relpath dedup suggest test alpha beta gamma.", "insights")

    result = _run(dispatch("palinode_dedup_suggest", {
        "content": "Duplicate content for relpath dedup suggest test alpha beta gamma.",
        "min_similarity": 0.0,
        "top_k": 5,
    }))
    text = result[0].text
    assert "insights/relpath-dedup-target.md" in text
    assert isolated_env not in text
