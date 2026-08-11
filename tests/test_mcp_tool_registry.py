"""Every advertised MCP tool has a handler, and every handler is advertised.

This invariant existed before the handler registry, but nothing could check it:
the mapping from tool name to behaviour was a 647-line ``if/elif`` chain inside
a private function, so "is this tool actually wired up?" was answered by reading
the chain. A tool added to ``_all_tools()`` and forgotten in the dispatcher
would advertise itself in ``list_tools()`` and then return ``"Unknown tool"``
when called — advertised, dispatchable, broken.

The hermetic smoke suite would eventually catch that, but only because
``tests/integration/_smoke_args.py`` carries a drift guard requiring an entry
per registered tool. This is the cheap unit-level version, and it names the
failure directly rather than as a smoke-test error string.
"""

from __future__ import annotations

import asyncio
import os

from palinode.mcp import _TOOL_HANDLERS, list_tools


def _advertised_tool_names() -> set[str]:
    """Every tool name ``list_tools()`` emits on the full surface."""
    previous = os.environ.get("PALINODE_MCP_SURFACE")
    os.environ["PALINODE_MCP_SURFACE"] = "full"
    try:
        return {tool.name for tool in asyncio.run(list_tools())}
    finally:
        if previous is None:
            os.environ.pop("PALINODE_MCP_SURFACE", None)
        else:
            os.environ["PALINODE_MCP_SURFACE"] = previous


def test_every_advertised_tool_has_a_handler() -> None:
    """A tool in the schema with no handler returns "Unknown tool" when called."""
    missing = sorted(_advertised_tool_names() - set(_TOOL_HANDLERS))
    assert not missing, (
        f"{missing} are advertised by list_tools() but have no entry in "
        "_TOOL_HANDLERS — calling them returns 'Unknown tool'. Add an "
        "@_handles(...) handler in palinode/mcp.py."
    )


def test_every_handler_is_advertised() -> None:
    """A handler for a tool nobody can see is dead code."""
    orphaned = sorted(set(_TOOL_HANDLERS) - _advertised_tool_names())
    assert not orphaned, (
        f"{orphaned} have handlers but are not advertised by list_tools() — "
        "either the schema entry was dropped or the handler outlived its tool."
    )


def test_handlers_are_coroutines() -> None:
    """``_dispatch_tool`` awaits whatever it finds; a sync handler would raise."""
    not_async = sorted(
        name for name, fn in _TOOL_HANDLERS.items()
        if not asyncio.iscoroutinefunction(fn)
    )
    assert not not_async, f"{not_async} are registered but not coroutines"


def test_the_registry_is_not_empty() -> None:
    """Guard the guard: an empty registry would satisfy both set comparisons."""
    assert len(_TOOL_HANDLERS) >= 30, (
        f"only {len(_TOOL_HANDLERS)} handlers registered — registration is not "
        "running, and the two set comparisons above are passing vacuously."
    )
