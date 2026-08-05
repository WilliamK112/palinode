"""Search results must surface typed relationship links to the agent.

`contradicts` records a conflict with no winner picked. Its entire value is at
read time: the store knowing two memories disagree is worth nothing if the
surface that answers questions never says so.

The API has always returned these inside a result's `metadata`, so a direct HTTP
caller could reach them. The MCP renderer — what an agent actually sees — did
not, which made the feature write-only in practice: links could be recorded and
never acted on.

Measured motivation, from the BEAM evaluation: across 3,200 answers every arm
cited both sides of a conflict at roughly 0.45-0.58, while *detecting* the
conflict topped out at 0.412. Retrieval was not the bottleneck; noticing was.
Full-context reading of the whole conversation was the worst arm at it, failing
to name a conflict 89% of the time while holding both statements. A link the
store already knows about is exactly the signal that closes that gap.
"""
from __future__ import annotations

from palinode.mcp import _format_results


def _result(**meta):
    return {
        "file_path": "/store/insights/alpha.md",
        "score": 0.9,
        "snippet": "Widget throughput is 900 units per hour.",
        "metadata": meta,
    }


def test_contradicts_is_rendered() -> None:
    out = _format_results([_result(contradicts=["insights/beta"])])
    assert "contradicts" in out
    assert "insights/beta" in out


def test_backed_by_is_rendered() -> None:
    out = _format_results([_result(backed_by=["research/paper"])])
    assert "backed by" in out
    assert "research/paper" in out


def test_both_links_render_together() -> None:
    out = _format_results([
        _result(contradicts=["insights/beta"], backed_by=["research/paper"])
    ])
    assert "insights/beta" in out and "research/paper" in out


def test_absent_links_add_no_noise() -> None:
    """The overwhelming majority of memories have no links; they must stay clean."""
    out = _format_results([_result()])
    assert "contradicts" not in out
    assert "backed by" not in out
    assert "[]" not in out, "empty link list leaked an empty bracket group"


def test_malformed_links_do_not_break_rendering() -> None:
    """Reads never raise — `parse_link_refs` soft-fails, and so must this.

    A store written by an older version, or hand-edited, can carry a bare string
    or a non-list. Rendering a search result is not the place to discover that.
    """
    for bad in ("insights/beta", None, 42, {"nested": "dict"}, ["", "  "]):
        out = _format_results([_result(contradicts=bad)])
        assert "Widget throughput" in out, f"rendering broke on contradicts={bad!r}"

    # The one that IS meaningful: a bare string is coerced to a single ref.
    out = _format_results([_result(contradicts="insights/beta")])
    assert "insights/beta" in out


def test_epistemic_marker_still_renders_alongside() -> None:
    """Guard against the new label displacing the existing one."""
    out = _format_results([
        _result(contradicts=["insights/beta"], epistemic="unverified")
    ])
    assert "[unverified]" in out
    assert "insights/beta" in out
