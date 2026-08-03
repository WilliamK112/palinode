"""CLI JSON output must survive the trip to stdout byte-for-byte.

``CLAUDE.md`` states the contract: *"CLI is TTY-aware: human-readable when
interactive, JSON when piped."* The piped branch is the one whose output is meant
to be machine-read, and it was the one being rendered through ``rich.Console`` —
a display renderer, which mangles it three ways.

The fixtures below are adversarial on purpose. The previous tests presumably
passed because their payloads were short and bracket-free, which is exactly the
data the bug does not touch: it needs a line past the console width, or a
``[...]`` sequence, before it bites.

The markup case is the one that matters most. Widening the console — the obvious
fix — resolves the wrapping and leaves markup consumption in place, which turns
an unparseable document into a **parseable document with content silently
deleted**. That is a strictly worse failure, so it is pinned explicitly.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

from palinode.cli._format import OutputFormat, emit_json, print_result


# Longer than any plausible console width, so wrapping would split it.
LONG_VALUE = "x" * 400

# Bracket sequences rich parses as style tags and removes. `[bold]` and `[red]`
# are real styles; `[2026-08-02]` is the shape a log line or a dated memory takes.
MARKUP_VALUE = "has [bold] and [red] and [2026-08-02] and [/] in it"

ADVERSARIAL = {
    "long": LONG_VALUE,
    "markup": MARKUP_VALUE,
    "unicode": "em—dash, curly ’quotes’, emoji 🧠",
    "nested": {"deep": [LONG_VALUE, MARKUP_VALUE]},
    "empty": "",
    "number": 42,
    "null": None,
}


def _captured(capsys) -> str:
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# emit_json — the single choke point
# ---------------------------------------------------------------------------

def test_emit_json_round_trips_exactly(capsys):
    """The whole contract in one assertion: what goes in comes out."""
    emit_json(ADVERSARIAL)

    assert json.loads(_captured(capsys)) == ADVERSARIAL


def test_emit_json_preserves_markup_like_substrings(capsys):
    """The silent-loss case: `[bold]` must not be eaten.

    Asserted separately from the round-trip because this is the failure that
    survives a width-only fix, and it produces valid JSON while doing it.
    """
    emit_json({"note": MARKUP_VALUE})

    parsed = json.loads(_captured(capsys))
    assert parsed["note"] == MARKUP_VALUE
    for tag in ("[bold]", "[red]", "[2026-08-02]", "[/]"):
        assert tag in parsed["note"], f"{tag} was consumed as markup"


def test_emit_json_does_not_wrap_long_lines(capsys):
    """A value longer than any console width stays on one line."""
    emit_json({"long": LONG_VALUE})

    out = _captured(capsys)
    assert json.loads(out)["long"] == LONG_VALUE
    assert max(len(line) for line in out.splitlines()) > 200, (
        "output was wrapped — a display renderer is still in the path"
    )


def test_emit_json_emits_no_ansi_escapes(capsys):
    """ANSI colour codes are not JSON."""
    emit_json(ADVERSARIAL)

    assert "\x1b[" not in _captured(capsys)


# ---------------------------------------------------------------------------
# print_result — the shared TTY-aware entry point
# ---------------------------------------------------------------------------

def test_print_result_json_round_trips_exactly(capsys):
    print_result(ADVERSARIAL, OutputFormat.JSON)

    assert json.loads(_captured(capsys)) == ADVERSARIAL


def test_print_result_defaults_to_json_when_not_a_tty(capsys, monkeypatch):
    """The documented contract, end to end: piped output is valid JSON.

    ``capsys`` already replaces stdout with a non-tty, which is the condition
    ``get_default_format`` keys on — and the same condition that used to trigger
    the 80-column fallback that corrupted the result.
    """
    monkeypatch.setattr("sys.stdout.isatty", lambda: False, raising=False)

    print_result(ADVERSARIAL)

    assert json.loads(_captured(capsys)) == ADVERSARIAL


# ---------------------------------------------------------------------------
# Structural guard — the pattern must not come back
# ---------------------------------------------------------------------------

CLI_DIR = pathlib.Path(__file__).resolve().parent.parent / "palinode" / "cli"
RICH_JSON = re.compile(r"console\.print\(\s*json\.dumps")


def test_no_cli_module_renders_json_through_rich():
    """Four modules had this; a fifth would reintroduce the defect silently.

    A grep-shaped test rather than a behavioural one because the failure is
    invisible until someone pipes a large or bracket-bearing payload — by which
    point it is a bug report, not a test failure.
    """
    offenders = [
        path.relative_to(CLI_DIR.parent.parent)
        for path in sorted(CLI_DIR.rglob("*.py"))
        if RICH_JSON.search(path.read_text())
    ]

    assert offenders == [], (
        f"{offenders} render JSON through rich.Console — use emit_json() from "
        "palinode.cli._format instead — rendering JSON through a display "
        "renderer wraps it, eats markup, and may colour it"
    )


@pytest.mark.parametrize("module", ["lint", "review"])
def test_converted_modules_import_the_helper(module):
    """The two modules converted off the rich path still use it."""
    source = (CLI_DIR / f"{module}.py").read_text()
    assert "emit_json" in source, f"{module}.py no longer routes JSON through emit_json"
