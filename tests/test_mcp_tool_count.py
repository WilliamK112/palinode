"""
Test: MCP tool count consistency

Ensures that docs/MCP-SETUP.md's available-tools table and palinode/mcp.py's registered
tools stay in sync. This test is the enforcement mechanism for Option C of issue the
canonical MCP tool-count maintenance — prose tool counts are removed from docs; the
table itself is the source of truth, and this assertion catches any drift. """
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MCP_SETUP_MD = REPO_ROOT / "docs" / "MCP-SETUP.md"


def _count_docs_table_rows() -> int:
    """Count rows in the available-tools table in MCP-SETUP.md.

    Lines that start with '| palinode_' (after stripping whitespace) are tool
    rows.  The header and separator lines do not match this pattern.
    """
    text = MCP_SETUP_MD.read_text(encoding="utf-8")
    rows = [
        line
        for line in text.splitlines()
        if re.match(r"\|\s*`palinode_", line)
    ]
    return len(rows)


def _registered_tool_names() -> list[str]:
    """Return the tool names the MCP server actually advertises.

    Asks the server, rather than grepping ``palinode/mcp.py`` for
    ``name="palinode_``. The old source-regex counted *syntax*, so it was
    coupled to how the tool list happens to be written: building the
    definitions from a loop, a decorator, or a table — any of the natural ways
    to break up a 1,000-line literal — would have failed this test while the
    advertised surface was byte-identical. A test that blocks a refactor it
    cannot observe is testing the wrong thing.

    ``PALINODE_MCP_SURFACE=full`` because the docs table documents the complete
    inventory; ``core`` deliberately advertises a subset (it hides advanced
    tools from discovery without changing dispatch), and the parity test pins
    ``full`` for the same reason.
    """
    from palinode.mcp import list_tools as mcp_list_tools

    previous = os.environ.get("PALINODE_MCP_SURFACE")
    os.environ["PALINODE_MCP_SURFACE"] = "full"
    try:
        tools = asyncio.run(mcp_list_tools())
    finally:
        if previous is None:
            os.environ.pop("PALINODE_MCP_SURFACE", None)
        else:
            os.environ["PALINODE_MCP_SURFACE"] = previous
    return [tool.name for tool in tools]


def _count_registered_tools() -> int:
    return len(_registered_tool_names())


def _docs_table_tool_names() -> list[str]:
    """Return the tool names listed in the MCP-SETUP.md available-tools table."""
    text = MCP_SETUP_MD.read_text(encoding="utf-8")
    return re.findall(r"^\|\s*`(palinode_[a-z_]+)`", text, re.MULTILINE)


def test_mcp_tool_count_matches_docs() -> None:
    docs_count = _count_docs_table_rows()
    code_count = _count_registered_tools()
    assert docs_count == code_count, (
        f"docs/MCP-SETUP.md table has {docs_count} rows "
        f"but the MCP server advertises {code_count} tools. "
        "Update the docs table (or the tool registration) so they match."
    )


def test_mcp_tool_names_match_docs() -> None:
    """The docs table lists the same tools the server advertises, by name.

    Counting alone would pass if one tool were renamed and another removed in
    the same change — now that the server is introspected rather than grepped,
    the names are available, so assert on them.
    """
    documented = set(_docs_table_tool_names())
    advertised = set(_registered_tool_names())

    undocumented = sorted(advertised - documented)
    stale = sorted(documented - advertised)

    assert not undocumented and not stale, (
        f"MCP tool/doc drift.\n"
        f"  advertised but undocumented: {undocumented or 'none'}\n"
        f"  documented but not advertised: {stale or 'none'}\n"
        "Update docs/MCP-SETUP.md's available-tools table."
    )
