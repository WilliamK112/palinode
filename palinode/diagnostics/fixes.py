"""
palinode doctor `--fix` mode whitelist.

Registers the *only* fix actions doctor is allowed to apply.  The design
constraint is non-negotiable:

    Doctor never moves user data, even with --fix.

The whitelist below is the entire safe surface.  Adding a new entry must be
justified explicitly in the PR description; data-touching fixes are off-
limits regardless of how convenient they would be.

Whitelist
---------
1. ``memory_dir_exists``       → create the configured memory_dir if missing.
2. ``audit_log_writable``      → create the parent dir of audit.log_path if
                                 it is relative and missing.  Never creates
                                 the log file itself; the audit subsystem
                                 owns that.
3. ``claude_md_palinode_block`` → append a Memory (Palinode) block to an
                                 existing CLAUDE.md.  Never creates
                                 CLAUDE.md from nothing — that file is
                                 user-owned.

Explicitly NOT fixable (and the reason is "doctor never moves user data"):

  - db_path_under_memory_dir  → suggests where db_path *should* point, but
    moving the DB file would constitute data motion.
  - phantom_db_files          → prints the suggested ``mv`` commands; doctor
    never executes them.  Phantom DB files often contain partial writes from
    a stale watcher; the user must inspect them before any move.
  - watcher_indexes_correct_db → editing the systemd unit file is a deploy
    action, not a doctor concern.  Prints the remediation only.
"""
from __future__ import annotations

import logging
from pathlib import Path

from palinode.cli.init import MEMORY_BLOCK_CORE, _slugify, has_memory_block
from palinode.diagnostics.registry import register_fix
from palinode.diagnostics.types import CheckResult, DoctorContext, FixResult

_logger = logging.getLogger("palinode.doctor.fix")


# ---------------------------------------------------------------------------
# fix #1: memory_dir_exists
# ---------------------------------------------------------------------------

def fix_memory_dir_exists(ctx: DoctorContext, result: CheckResult) -> FixResult:
    """Create the configured memory_dir (and any missing parents)."""
    target = Path(ctx.config.memory_dir).expanduser().resolve()
    if target.exists():
        return FixResult(
            applied=False,
            message=f"memory_dir already exists at {target}; nothing to do.",
        )
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return FixResult(
            applied=False,
            message=f"Could not create {target}: {exc}",
        )
    _logger.info("doctor --fix: created memory_dir at %s", target)
    return FixResult(applied=True, message=f"Created directory {target}")


# ---------------------------------------------------------------------------
# fix #2: audit_log_writable
# ---------------------------------------------------------------------------

def fix_audit_log_writable(ctx: DoctorContext, result: CheckResult) -> FixResult:
    """Create the parent dir of a relative-and-missing audit log path.

    Conservative scope:
      - Only creates the *parent directory* of audit.log_path.  Never the
        log file itself — the audit subsystem owns that.
      - Only acts when audit.log_path is relative.  Absolute paths under
        operator control are out of scope (they may live on a separate
        mount with intentional permissions).
      - Anchors the relative path under config.memory_dir so the resulting
        directory is colocated with the memory store (matches the design-
        doc remediation: "Set audit.log_path to an absolute path under
        memory_dir").
    """
    audit = getattr(ctx.config, "audit", None)
    if audit is None or not getattr(audit, "enabled", False):
        return FixResult(
            applied=False,
            message="audit logging is disabled; nothing to do.",
        )
    log_path_str = getattr(audit, "log_path", "")
    if not log_path_str:
        return FixResult(
            applied=False,
            message="audit.log_path is empty; nothing to do.",
        )
    log_path = Path(log_path_str)
    if log_path.is_absolute():
        return FixResult(
            applied=False,
            message=(
                f"audit.log_path is absolute ({log_path}); doctor leaves "
                "operator-managed absolute paths alone.  Edit "
                "palinode.config.yaml manually if needed."
            ),
        )
    memory_dir = Path(ctx.config.memory_dir).expanduser().resolve()
    parent = (memory_dir / log_path).parent
    if parent.exists():
        return FixResult(
            applied=False,
            message=f"Audit log parent {parent} already exists; nothing to do.",
        )
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return FixResult(
            applied=False,
            message=f"Could not create {parent}: {exc}",
        )
    _logger.info("doctor --fix: created audit log parent dir at %s", parent)
    return FixResult(
        applied=True,
        message=f"Created audit log parent directory {parent}",
    )


# ---------------------------------------------------------------------------
# fix #3: claude_md_palinode_block
# ---------------------------------------------------------------------------

# The block this fix appends, and the marker it uses to detect one is
# already present, are imported from `palinode.cli.init` — the SAME block
# `palinode init` writes and the SAME marker the `claude_md_palinode_block`
# check tests for. `MEMORY_BLOCK_CORE` is the harness-neutral rendering (no
# Claude-Code-only machinery: hooks, /wrap, /clear) because this fix only
# ever appends to an existing file — it never installs hooks or slash
# commands the way `palinode init` does.


def fix_claude_md_palinode_block(ctx: DoctorContext, result: CheckResult) -> FixResult:
    """Append a Palinode memory block to an existing CLAUDE.md.

    Strict guard: only appends when CLAUDE.md ALREADY exists.  Never creates
    CLAUDE.md from scratch — that is a user-owned project file and creating
    it without consent would be presumptuous.

    The CLAUDE.md path is resolved from the doctor result message when the
        check provides one; otherwise we look in cwd, which matches the
        heuristic ("In each cwd ancestor, look for CLAUDE.md"). The
    fix never walks the filesystem on its own — if no CLAUDE.md exists in
    cwd, the fix declines with a clear message.
    """
    candidate = Path.cwd() / "CLAUDE.md"
    if not candidate.exists():
        return FixResult(
            applied=False,
            message=(
                f"No CLAUDE.md at {candidate}; doctor will not create one. "
                "CLAUDE.md is user-owned — create it manually, then re-run "
                "'palinode doctor --fix'."
            ),
        )
    try:
        content = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        return FixResult(
            applied=False,
            message=f"Could not read {candidate}: {exc}",
        )
    if has_memory_block(content):
        return FixResult(
            applied=False,
            message=(
                f"{candidate} already contains a Palinode memory block; "
                "nothing to do."
            ),
        )
    slug = _slugify(candidate.parent.name)
    block = MEMORY_BLOCK_CORE.format(project_slug=slug)
    try:
        with candidate.open("a", encoding="utf-8") as fh:
            # Same separator convention as `palinode init`'s
            # `_write_memory_block`: guarantee the file ends in a newline,
            # then add one blank line before the block.
            if not content.endswith("\n"):
                fh.write("\n")
            fh.write("\n" + block)
    except OSError as exc:
        return FixResult(
            applied=False,
            message=f"Could not append to {candidate}: {exc}",
        )
    _logger.info("doctor --fix: appended Palinode memory block to %s", candidate)
    return FixResult(
        applied=True,
        message=f"Appended Palinode memory block to {candidate}",
    )


# ---------------------------------------------------------------------------
# Registration — THE WHITELIST.
# ---------------------------------------------------------------------------
# Adding any line below requires explicit reasoning in the PR description.
# Doctor never moves user data, even with --fix.

register_fix("memory_dir_exists", fix_memory_dir_exists)
register_fix("audit_log_writable", fix_audit_log_writable)
register_fix("claude_md_palinode_block", fix_claude_md_palinode_block)
