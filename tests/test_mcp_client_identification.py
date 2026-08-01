"""The server must be able to read the client's identity from the handshake.

This is load-bearing: ``_auto_inject_suppressed_for`` decides whether to skip
the session-start digest for harnesses that already carry instruction-file /
skill / hook layers, and it decides that from ``clientInfo.name``.

It is also a path that **fails silently**. Both of its SDK touchpoints moved in
the mcp 2.x migration — ``server.request_context`` was removed, and
``clientInfo`` became ``client_info`` — and the whole body sat under a bare
``except`` returning ``""``. An unreadable client and an unidentifiable one
produce the identical value, so the suppression would simply stop applying and
nothing anywhere would say so. Every other test in the suite passed with it
broken.

Hence a test that drives a *real* handshake with a known client name and
asserts the server can see it, rather than one that calls the helper directly
and gets ``""`` legitimately.
"""
from __future__ import annotations

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import Implementation

from palinode import mcp as pmcp


async def _name_seen_by_server(client_name: str, seen: list[str]) -> str:
    """Run a real initialize + tools/call and report the name the server read."""
    async with create_client_server_memory_streams() as ((cr, cw), (sr, sw)):
        async with anyio.create_task_group() as tg:

            async def run_server():
                await pmcp.server.run(
                    sr, sw, pmcp.server.create_initialization_options(),
                    raise_exceptions=True,
                )

            tg.start_soon(run_server)
            async with ClientSession(
                cr, cw, client_info=Implementation(name=client_name, version="1.0")
            ) as client:
                await client.initialize()
                await client.call_tool("palinode_status", {})
            tg.cancel_scope.cancel()

    return seen[0] if seen else "<handler never ran>"


@pytest.mark.parametrize("client_name", ["claude-code", "some-other-harness"])
def test_server_reads_client_name_from_the_handshake(client_name, monkeypatch):
    seen: list[str] = []

    async def _probe(name, arguments):
        # Read the client name from inside a live request, which is the only
        # place it is available.
        seen.append(pmcp._session_init_client_name())
        return pmcp._text("ok")

    monkeypatch.setattr(pmcp, "_dispatch_tool", _probe)
    got = anyio.run(_name_seen_by_server, client_name, seen)
    assert got == client_name, (
        f"server read client name {got!r}, expected {client_name!r} — "
        "clientInfo is unreadable, so auto-inject suppression cannot apply"
    )


def test_outside_a_request_context_is_empty_not_an_error():
    """Tests and tooling call the handlers directly; that must stay quiet."""
    assert pmcp._session_init_client_name() == ""
