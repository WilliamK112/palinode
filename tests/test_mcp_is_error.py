"""Tool failures reach the host flagged, and bad arguments are named.

The dispatcher reports failure in-band — text opening with one of
``DISPATCH_ERROR_PREFIXES`` — and the audit log has classified on that prefix
for a long time. The client-facing ``CallToolResult.is_error`` did not: every
failure arrived as a *successful* result, and a missing required argument
surfaced as the raw ``KeyError`` (``"Error: 'file_path'"``).

These tests drive a real ``tools/call`` over the SDK's in-memory transport, so
what is asserted is what a host sees, not what the adapter returns to a direct
caller.
"""
from __future__ import annotations

import anyio
import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CallToolResult

from palinode import mcp as pmcp


async def _roundtrip(name: str, arguments: dict) -> CallToolResult:
    """One real initialize + tools/call against the server object."""
    async with create_client_server_memory_streams() as ((cr, cw), (sr, sw)):
        async with anyio.create_task_group() as tg:

            async def run_server():
                await pmcp.server.run(
                    sr, sw, pmcp.server.create_initialization_options(),
                    raise_exceptions=True,
                )

            tg.start_soon(run_server)
            async with ClientSession(cr, cw) as client:
                await client.initialize()
                result = await client.call_tool(name, arguments)
            tg.cancel_scope.cancel()
    return result


def _text_of(result: CallToolResult) -> str:
    return result.content[0].text


def test_missing_required_argument_is_flagged_and_named():
    result = anyio.run(_roundtrip, "palinode_read", {})
    assert result.is_error is True
    assert _text_of(result) == "Error: file_path is required"


def test_several_missing_required_arguments_are_all_named():
    result = anyio.run(_roundtrip, "palinode_save", {"type": "Insight"})
    assert result.is_error is True
    assert "content" in _text_of(result)


def test_non_numeric_limit_is_flagged_and_named():
    result = anyio.run(_roundtrip, "palinode_search", {"query": "x", "limit": "abc"})
    assert result.is_error is True
    text = _text_of(result)
    assert "limit" in text and "abc" in text


def test_unknown_tool_is_flagged():
    result = anyio.run(_roundtrip, "palinode_nope", {})
    assert result.is_error is True
    assert _text_of(result).startswith("Unknown tool")


def test_no_results_search_is_not_an_error(monkeypatch):
    """A search that ran and found nothing is an answer, not a failure."""

    async def _empty(path, json=None, timeout=30.0):
        return httpx.Response(200, json=[])

    monkeypatch.setattr(pmcp, "_post", _empty)
    result = anyio.run(_roundtrip, "palinode_search", {"query": "x"})
    assert not result.is_error
    assert not _text_of(result).startswith(pmcp.DISPATCH_ERROR_PREFIXES)


def test_api_failure_text_is_flagged(monkeypatch):
    """A handler's own in-band failure text carries the flag too."""

    async def _boom(path, json=None, timeout=30.0):
        return httpx.Response(500, text="backend down")

    monkeypatch.setattr(pmcp, "_post", _boom)
    result = anyio.run(_roundtrip, "palinode_search", {"query": "x"})
    assert result.is_error is True
    assert _text_of(result).startswith("Search failed")


def test_every_required_arg_in_the_schema_is_enforced():
    """The check is schema-driven, so it covers every tool with a ``required``
    list — not just the two that used to hand-roll it."""
    tools = pmcp._all_tools()
    required = {t.name: list(t.input_schema.get("required", [])) for t in tools}
    assert required["palinode_read"] == ["file_path"]  # guard the guard
    for name, keys in required.items():
        if not keys:
            continue
        message = pmcp._validate_arguments(name, {})
        assert message is not None and message.startswith("Error:"), name
        for key in keys:
            assert key in message, (name, key)


@pytest.mark.parametrize("value", [1, "5", 5.0])
def test_coercible_numeric_values_pass_validation(value):
    assert pmcp._validate_arguments("palinode_search", {"query": "x", "limit": value}) is None
