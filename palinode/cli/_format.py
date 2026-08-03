import sys
import json
from enum import Enum
from typing import Any, Optional

import click
from rich.console import Console

console = Console()


def emit_json(data: Any) -> None:
    """Write JSON to stdout verbatim, never through a display renderer.

    ``rich.Console`` is for humans, and routing machine-readable output through
    it corrupts that output three separate ways:

    1. **Wrapping.** Console width falls back to 80 columns when stdout is not a
       terminal, and rich hard-wraps — injecting newlines *inside* string
       literals, which makes the document unparseable.
    2. **Markup.** ``console.print`` interprets ``[...]`` as style tags and
       consumes them, so a memory whose text contains ``[bold]`` or ``[red]``
       comes back with those substrings **silently deleted**.
    3. **Colour.** Rich may emit ANSI escapes depending on how it resolves the
       stream, which are not JSON.

    (2) is the reason this helper exists rather than a wider console: widening
    fixes (1) and leaves (3), and leaves (2) producing output that *parses
    cleanly and is wrong*. Trading a loud failure for a silent one is worse than
    the original bug.

    The three CLI modules that already used ``click.echo`` for JSON were never
    affected; this makes that the single path.
    """
    click.echo(json.dumps(data, indent=2))

class OutputFormat(str, Enum):
    TEXT = "text"
    JSON = "json"

def get_default_format() -> OutputFormat:
    """Detect if output is a TTY and return default format."""
    if sys.stdout.isatty():
        return OutputFormat.TEXT
    return OutputFormat.JSON

def print_result(data: Any, fmt: Optional[OutputFormat] = None):
    """Print data in the requested or default format."""
    if fmt is None:
        fmt = get_default_format()
    
    if fmt == OutputFormat.JSON:
        emit_json(data)
    else:
        # Commands should implement their own custom printing for TEXT
        # This is a fallback
        if isinstance(data, (dict, list)):
            console.print(data)
        else:
            console.print(str(data))

def print_error(msg: str):
    console.print(f"[red]Error:[/red] {msg}")

def print_success(msg: str):
    console.print(f"[green]✓[/green] {msg}")
