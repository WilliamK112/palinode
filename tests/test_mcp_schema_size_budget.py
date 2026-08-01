"""Per-tool MCP schema size budget.

A tool schema that is too large does not fail loudly. At least one MCP client
caps tool schemas at 4,096 bytes and, on exceeding it, **replaces the schema
with an empty object** rather than truncating or erroring — logging a warning
on its own side, which nobody operating the server ever sees.

The tool still appears in the model's tool list — it simply has no parameter
contract, so the model is invited to call a write tool it has been told nothing
about. Reads keep working (``palinode_search`` and ``palinode_read`` are far
under the cap), so the agent looks healthy while its save path is the broken
one.

The condition is invisible from this side: it happens in the client, after
Palinode has done everything right, and nothing in the API, the MCP server, or
an aggregator's own tool accounting can observe it. Staying under a plausible
client cap is therefore the only lever available here, and this test is the
only thing that can notice when a new field spends the remaining room.

**When this test fails, split the tool — do not compress prose.** Shrinking
descriptions buys a few hundred bytes and leaves the next field to re-break it;
moving a cohesive parameter cluster to its own tool fixes the class. The
provenance cluster on ``palinode_save`` (``claims``/``sources``/``backed_by``/
``contradicts``) is the standing candidate: ~1.4 KB for structures a typical
save never sends.
"""
from __future__ import annotations

import json

import pytest

from palinode.mcp import _all_tools

#: The observed client cap, in bytes, applied per tool. Not Palinode's own
#: limit — a downstream client's. It is treated as a budget rather than a guess
#: because the failure mode when a client enforces one is a write tool with no
#: contract, reported nowhere.
SCHEMA_BUDGET_BYTES = 4096


def _wire_size(tool) -> int:
    """Bytes a client sees for one tool, serialized compactly.

    Name, description and input schema together — the shape a client caps on.
    Compact separators because that is the favourable end of the range: a
    client that pretty-prints, or that namespaces tool names with a prefix of
    its own, only ever sees more than this.
    """
    return len(json.dumps(
        {"name": tool.name,
         "description": tool.description,
         "inputSchema": tool.input_schema},
        separators=(",", ":"),
    ))


@pytest.mark.parametrize("tool", _all_tools(), ids=lambda t: t.name)
def test_tool_schema_fits_the_client_budget(tool):
    size = _wire_size(tool)
    assert size <= SCHEMA_BUDGET_BYTES, (
        f"{tool.name} serializes to {size} B, over the {SCHEMA_BUDGET_BYTES} B "
        f"client budget by {size - SCHEMA_BUDGET_BYTES} B. A client enforcing "
        f"this cap may drop the schema entirely and leave the model calling "
        f"{tool.name} with no parameter contract. Split a parameter cluster "
        f"onto its own tool rather than trimming descriptions — see the module "
        f"docstring."
    )
