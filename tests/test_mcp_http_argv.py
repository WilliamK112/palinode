"""
tests/test_mcp_http_argv.py — ``palinode-mcp-http`` honours ``--host`` / ``--port``.

Before the fix ``main_http()`` parsed no argv at all, so ``--port 6341`` in a
hand-written unit was silently ignored and a rig on a non-default port bound
6341. The tests drive the real entry point with ``uvicorn.run`` replaced at
the seam and assert the bind target uvicorn is handed:

- flag > env > default, for both host and port;
- env-only still works (the path the shipped systemd/nix units use);
- unknown flags and positionals exit non-zero (argparse's normal error path);
- ``main_sse()`` passes argv through after its deprecation warning.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def clean_mcp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "PALINODE_MCP_HTTP_HOST",
        "PALINODE_MCP_HTTP_PORT",
        "PALINODE_MCP_SSE_HOST",
        "PALINODE_MCP_SSE_PORT",
        "PALINODE_MCP_BIND_INTENT",
        "PALINODE_API_TOKEN",
        "PALINODE_API_TOKEN_FILE",
    ):
        monkeypatch.delenv(var, raising=False)


def _capture_uvicorn(monkeypatch: pytest.MonkeyPatch) -> dict:
    import uvicorn

    captured: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    return captured


def test_port_flag_is_honoured(clean_mcp_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    import palinode.mcp as mcp_mod

    monkeypatch.setenv("PALINODE_MCP_HTTP_HOST", "127.0.0.1")
    captured = _capture_uvicorn(monkeypatch)
    mcp_mod.main_http(["--port", "1"])
    assert captured["port"] == 1


def test_flag_beats_env_beats_default(
    clean_mcp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import palinode.mcp as mcp_mod

    monkeypatch.setenv("PALINODE_MCP_HTTP_HOST", "127.0.0.2")
    monkeypatch.setenv("PALINODE_MCP_HTTP_PORT", "7000")
    captured = _capture_uvicorn(monkeypatch)
    mcp_mod.main_http(["--host", "127.0.0.3", "--port", "7001"])
    assert captured["host"] == "127.0.0.3"
    assert captured["port"] == 7001


def test_env_only_still_works(clean_mcp_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    import palinode.mcp as mcp_mod

    monkeypatch.setenv("PALINODE_MCP_HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("PALINODE_MCP_HTTP_PORT", "7002")
    captured = _capture_uvicorn(monkeypatch)
    mcp_mod.main_http([])
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 7002


def test_no_flags_no_env_uses_defaults(
    clean_mcp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import palinode.mcp as mcp_mod

    monkeypatch.setenv("PALINODE_MCP_HTTP_HOST", "127.0.0.1")
    captured = _capture_uvicorn(monkeypatch)
    mcp_mod.main_http([])
    assert captured["port"] == 6341


@pytest.mark.parametrize("argv", [["--bogus"], ["6341"], ["--port"], ["--port", "x"]])
def test_bad_argv_exits_nonzero(
    clean_mcp_env: None, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    import palinode.mcp as mcp_mod

    captured = _capture_uvicorn(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        mcp_mod.main_http(argv)
    assert exc.value.code != 0
    assert not captured, "uvicorn must not be reached on a bad command line"


def test_main_sse_passes_argv_through(
    clean_mcp_env: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import palinode.mcp as mcp_mod

    captured = _capture_uvicorn(monkeypatch)
    with caplog.at_level("WARNING", logger="palinode.mcp"):
        mcp_mod.main_sse(["--host", "127.0.0.1", "--port", "7003"])
    assert any("palinode-mcp-sse is deprecated" in r.getMessage() for r in caplog.records)
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 7003
