"""
tests/test_mcp_sse_alias_deprecation.py — the ``palinode-mcp-sse`` alias is
deprecated, and no shipping asset points at it any more.

Two halves:

1. Runtime: ``main_sse()`` and the ``PALINODE_MCP_SSE_*`` env vars still work,
   warn at startup, and lose to the canonical ``PALINODE_MCP_HTTP_*`` names
   when both are set.
2. Grep guard: no shipping file outside ``docs/CHANGELOG.md`` mentions the
   deprecated names, except the alias definition itself and the one-place
   deprecation notice. Deploy templates, ``server.json``, README, and the docs
   all name ``palinode-mcp-http`` — the deprecation cannot be executed while
   the docs still tell every new install to use the alias.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

DEPRECATED = re.compile(r"palinode-mcp-sse|PALINODE_MCP_SSE_")

# Shipping surfaces that must be clean. docs/CHANGELOG.md is history and is
# never rewritten; docs/MILESTONES.md is a roadmap doc and is skipped where
# it exists.
SCAN_FILES = ["README.md", "SECURITY.md", "server.json"]
SCAN_DIRS = ["deploy", "nix", "docs", "examples", "palinode"]
SCAN_EXCLUDE = {"docs/CHANGELOG.md", "docs/MILESTONES.md"}

# The alias definition, its mirror comments, the runtime fallback, and the
# single documented deprecation notice. Each must say "deprecated" so a reader
# who lands on the string is told it is going away.
ALIAS_ALLOWLIST = {
    "pyproject.toml",
    "flake.nix",
    "palinode/mcp.py",
    "docs/MCP-SETUP.md",
}


def _shipping_files() -> list[Path]:
    files = [REPO_ROOT / f for f in SCAN_FILES] + [REPO_ROOT / f for f in ALIAS_ALLOWLIST]
    for d in SCAN_DIRS:
        files.extend(p for p in (REPO_ROOT / d).rglob("*") if p.is_file())
    seen: set[Path] = set()
    out: list[Path] = []
    for p in files:
        rel = p.relative_to(REPO_ROOT).as_posix()
        if p in seen or rel in SCAN_EXCLUDE or "__pycache__" in rel:
            continue
        seen.add(p)
        out.append(p)
    return out


def test_no_shipping_file_points_at_deprecated_alias() -> None:
    offenders: list[str] = []
    for path in _shipping_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        hits = [
            f"{rel}:{n}: {line.strip()}"
            for n, line in enumerate(text.splitlines(), 1)
            if DEPRECATED.search(line)
        ]
        if not hits:
            continue
        if rel in ALIAS_ALLOWLIST:
            if "deprecated" not in text.lower():
                offenders.append(f"{rel}: mentions the alias without saying it is deprecated")
            continue
        offenders.extend(hits)
    assert not offenders, (
        "Deprecated palinode-mcp-sse / PALINODE_MCP_SSE_* still referenced by "
        "shipping files (flip to palinode-mcp-http / PALINODE_MCP_HTTP_*):\n"
        + "\n".join(offenders)
    )


def test_deploy_templates_use_http_entry_point() -> None:
    systemd = (REPO_ROOT / "deploy/systemd/palinode-mcp.service.template").read_text()
    nix = (REPO_ROOT / "nix/services/mcp-service.nix").read_text()
    assert "/palinode-mcp-http" in systemd
    assert 'PALINODE_MCP_HTTP_PORT=${MCP_PORT}' in systemd, (
        "the shipped unit passes the port via env, not --port (#1064)"
    )
    assert "/palinode-mcp-http" in nix
    assert "PALINODE_MCP_HTTP_PORT = toString cfg.port" in nix


def test_server_json_names_http_entry_point() -> None:
    import json

    manifest = json.loads((REPO_ROOT / "server.json").read_text())
    positional = [
        arg["value"]
        for pkg in manifest["packages"]
        for arg in pkg.get("packageArguments", [])
        if arg.get("type") == "positional"
    ]
    assert "palinode-mcp-http" in positional
    assert "palinode-mcp-sse" not in positional


# ---------------------------------------------------------------------------
# Runtime: alias + legacy env vars still work, warn, and lose to canonical
# ---------------------------------------------------------------------------


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
    monkeypatch.setenv("PALINODE_MCP_HTTP_HOST", "127.0.0.1")


def _capture_uvicorn(monkeypatch: pytest.MonkeyPatch) -> dict:
    import uvicorn

    captured: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    return captured


def test_main_sse_warns_and_still_serves(
    clean_mcp_env: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import palinode.mcp as mcp_mod

    captured = _capture_uvicorn(monkeypatch)
    with caplog.at_level("WARNING", logger="palinode.mcp"):
        mcp_mod.main_sse([])

    msgs = [r.getMessage() for r in caplog.records]
    assert any("palinode-mcp-sse is deprecated" in m and "palinode-mcp-http" in m for m in msgs), msgs
    assert captured["port"] == 6341


def test_legacy_env_only_warns(
    clean_mcp_env: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import palinode.mcp as mcp_mod

    monkeypatch.setenv("PALINODE_MCP_SSE_PORT", "7777")
    captured = _capture_uvicorn(monkeypatch)
    with caplog.at_level("WARNING", logger="palinode.mcp"):
        mcp_mod.main_http([])

    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "PALINODE_MCP_SSE_PORT is deprecated" in m and "PALINODE_MCP_HTTP_PORT" in m for m in msgs
    ), msgs
    assert captured["port"] == 7777


def test_canonical_env_wins_and_does_not_warn(
    clean_mcp_env: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import palinode.mcp as mcp_mod

    monkeypatch.setenv("PALINODE_MCP_HTTP_PORT", "6350")
    monkeypatch.setenv("PALINODE_MCP_SSE_PORT", "7777")
    captured = _capture_uvicorn(monkeypatch)
    with caplog.at_level("WARNING", logger="palinode.mcp"):
        mcp_mod.main_http([])

    msgs = [r.getMessage() for r in caplog.records]
    assert captured["port"] == 6350
    assert captured["host"] == "127.0.0.1"
    assert not any("deprecated" in m for m in msgs), msgs
