"""
Check: claude_md_palinode_block

Walks the user's HOME directory for ~/.claude/CLAUDE.md (global) and any
CLAUDE.md in ancestor directories of cwd up to HOME (project-level).  For
each file found, checks whether it carries a Palinode memory block heading
(``has_memory_block`` — the same detector `palinode init` and `doctor --fix`
use, so the three can never disagree about what "already wired up" means).
A file that merely mentions "palinode" in prose does not count.

If NEITHER the global nor any project CLAUDE.md has the block, warns. This
is the #1 install-day footgun: the MCP tools are registered and work fine,
but the LLM is never told to use them at session boundaries.

Severity: warn (neither file has the block)

Tag: fast (pure filesystem reads, no network, no SQLite)

 """
from __future__ import annotations

import os
from pathlib import Path

from palinode.cli.init import has_memory_block
from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


def _find_claude_md_files(home: Path, cwd: Path) -> list[Path]:
    """Return a list of candidate CLAUDE.md file paths to inspect.

    Checks:
      1. ~/.claude/CLAUDE.md  (global)
      2. Every CLAUDE.md in cwd and its ancestors up to home (project-level)

    The list is deduplicated and only includes paths that actually exist.
    """
    candidates: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        resolved = p.resolve()
        if resolved not in seen and resolved.exists():
            seen.add(resolved)
            candidates.append(resolved)

    # Global Claude Code config.
    _add(home / ".claude" / "CLAUDE.md")

    # Project-level: cwd up to (and including) home.
    current = cwd.resolve()
    while True:
        _add(current / "CLAUDE.md")
        if current == home or current == current.parent:
            break
        current = current.parent

    return candidates


def _has_memory_block(path: Path) -> bool:
    """Return True if *path* already carries a Palinode memory block heading."""
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
        return has_memory_block(content)
    except OSError:
        return False


@register(tags=("fast",))
def claude_md_palinode_block(ctx: DoctorContext) -> CheckResult:
    """Warn when no CLAUDE.md in scope has a Palinode memory block.

    The MCP tools work fine without a CLAUDE.md block, but the LLM will not
    proactively call palinode_save / palinode_search at session boundaries
    unless it is instructed to.  This is the #1 install-day footgun.
    """
    home = Path.home()
    cwd = Path(os.getcwd())

    candidates = _find_claude_md_files(home, cwd)

    if not candidates:
        return CheckResult(
            name="claude_md_palinode_block",
            severity="warn",
            passed=False,
            message=(
                "No CLAUDE.md files found (checked ~/.claude/CLAUDE.md and "
                "project directories up to home).  "
                "The LLM will not know to use palinode tools at session boundaries."
            ),
            remediation=(
                "Run 'palinode init' in your project directory to scaffold a "
                "CLAUDE.md with the palinode memory block, or add the block manually:\n"
                "  ## Memory (Palinode)\n"
                "  Call palinode_search at session start, palinode_save after milestones."
            ),
        )

    # Check each file.
    found_with_block: list[Path] = []
    found_without_block: list[Path] = []

    for p in candidates:
        if _has_memory_block(p):
            found_with_block.append(p)
        else:
            found_without_block.append(p)

    if found_with_block:
        # At least one CLAUDE.md has the block — the LLM will see it.
        files_str = ", ".join(str(p) for p in found_with_block)
        return CheckResult(
            name="claude_md_palinode_block",
            severity="warn",
            passed=True,
            message=f"Palinode memory block found in: {files_str}",
            remediation=None,
        )

    # No CLAUDE.md has the block — a prose mention of "palinode" doesn't count.
    checked_str = "\n".join(f"  {p}" for p in candidates)
    return CheckResult(
        name="claude_md_palinode_block",
        severity="warn",
        passed=False,
        message=(
            "None of the CLAUDE.md files in scope have a '## Memory (Palinode)' "
            "block.  The LLM will not call palinode tools at session boundaries."
        ),
        remediation=(
            "Add a '## Memory (Palinode)' section to at least one of:\n"
            f"{checked_str}\n\n"
            "Or run 'palinode init --no-mcp --no-hook --no-slash' to add the "
            "memory block automatically.  Minimum block:\n"
            "  ## Memory (Palinode)\n"
            "  Call palinode_search at session start, "
            "palinode_save after milestones, palinode_session_end at wrap."
        ),
    )
