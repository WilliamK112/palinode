#!/bin/bash
# check-write-choke-point.sh — enforce the core/git_tools.py mutation choke point.
#
# core/git_tools.py documents the invariant in its own module docstring:
# every memory-file write routes through write_memory_file (or
# move_memory_file for a relocation) and every commit through
# commit_memory_file / commit_memory_files — concentrating both in one place
# so a future signer has a single observation point for the mutation chain,
# and so a commit always stages an explicit file list rather than a
# repo-wide `git add *.md` sweep. Until this script, that invariant was
# prose only: nothing scanned the tree for a bypass. Two shapes are checked:
#
#   1. a raw open(path, "w"|"a"|...) (or Path.open(...)) outside git_tools.py
#   2. a raw `git add` / `git commit` / `git push` subprocess argv outside
#      git_tools.py
#
# Both checks carry an explicit, commented allowlist for legitimate
# non-memory writes (audit/diagnostic logs, scaffolding written into a
# *different* repo, throwaway probes, migration scratch files) — never a
# people/projects/decisions/insights/daily/research/inbox/archive/prompts
# write. The git-subcommand allowlist is empty by design: nothing besides
# the choke point should ever spawn `git add`/`git commit`/`git push`.
#
# Usage:
#   ./scripts/check-write-choke-point.sh                   # scan whole tree
#   ./scripts/check-write-choke-point.sh path/to/file [...] # specific files
#
# Exit code: 0 = clean, 1 = bypass found.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# The choke point itself — the one file allowed to touch subprocess
# git add/commit/push and to open() a memory path directly.
CHOKE_POINT="palinode/core/git_tools.py"

# Files that legitimately call open(..., "w"|"a") (or Path.open(...)) on a
# path outside the memory-file tree this invariant protects. Each entry says
# why. Add to this list only with a comment — an unexplained entry here is a
# leak waiting to happen, same as an unexplained scrub-path carve-out.
OPEN_ALLOWED=(
    # JSONL tool-call audit trail under memory_dir/.audit/ — a telemetry
    # log, not a memory file; never git-committed by design (one entry per
    # tool call would be commit-spam).
    "palinode/core/audit.py"
    # JSONL retrieval-event log under memory_dir/.audit/ — same as above.
    "palinode/core/retrieval_log.py"
    # Dev smoke-test run log (--record mode) — unrelated to memory content.
    "palinode/cli/mcp_smoke.py"
    # Throwaway watcher-liveness canary: written, polled, then os.remove()'d
    # within the same request — never committed, never meant to persist.
    "palinode/api/routers/health.py"
    # Disk-backed pending-check JSON marker (write-time contradiction
    # queue) — a job marker, not a memory file. This file's git add/commit
    # bypass (_git_commit_dedup) is fixed and is NOT exempted below — only
    # the marker write is a legitimate non-memory open().
    "palinode/consolidation/write_time.py"
    # One-time Mem0 migration scratch JSON (export/classify intermediates
    # under memory_dir/migration/) — never recalled, never committed.
    "palinode/migration/mem0_export.py"
    "palinode/migration/mem0_classify.py"
    # `palinode init` scaffolds .claude/ files into the *target* repo the
    # CLI is run from, not palinode's own memory_dir.
    "palinode/cli/init.py"
    # Diagnostics baseline log (index-size trend) — not memory content.
    "palinode/diagnostics/checks/index_size.py"
    # `doctor --fix` appends the Palinode integration block to the *user's*
    # CLAUDE.md, not palinode's own memory_dir.
    "palinode/diagnostics/fixes.py"
)

# Nothing besides the choke point may spawn `git add` / `git commit` /
# `git push` as a raw subprocess call. Empty by design: a new entry here
# would mean a new bypass, not a grandfathered one.
GIT_ALLOWED=(
)

# Determine scan set: explicit files, or the whole tree.
files=()
if [[ $# -gt 0 ]]; then
    files=("$@")
else
    while IFS= read -r f; do
        [[ -n "$f" ]] && files+=("$f")
    done < <(find palinode -name '*.py' 2>/dev/null | sort)
fi

if [[ ${#files[@]} -eq 0 ]]; then
    echo "check-write-choke-point: no files to scan"
    exit 0
fi

# See the httpx-monopoly linter for why this guard exists: under `set -u`,
# bash 3.2 (still the system bash on macOS) treats expansion of an EMPTY
# array as unbound and aborts. `${arr[@]+"${arr[@]}"}` expands to nothing
# when unset/empty and to the elements otherwise, on both bash 3.2 and bash 5.
is_open_allowed() {
    local f="$1"
    local a
    for a in ${OPEN_ALLOWED[@]+"${OPEN_ALLOWED[@]}"}; do
        [[ "$f" == "$a" || "$f" == */"$a" ]] && return 0
    done
    return 1
}

is_git_allowed() {
    local f="$1"
    local a
    for a in ${GIT_ALLOWED[@]+"${GIT_ALLOWED[@]}"}; do
        [[ "$f" == "$a" || "$f" == */"$a" ]] && return 0
    done
    return 1
}

# Matches open(<anything>, "w") / open(<anything>, "a") and the wb/ab/w+/a+
# variants, whether the builtin `open(` or a `pathlib.Path.open(` call —
# both spell the same raw-write bypass. Double-quoted mode strings only:
# that is the convention every real caller in this tree already uses (see
# OPEN_ALLOWED above), same scoping choice check-httpx-monopoly.sh makes for
# its import-statement grep.
OPEN_WRITE_RE='open\([^)]*,[[:space:]]*"(w|a|wb|ab|w\+|a\+)"'

# Matches the argv-list shape every git subprocess call in this tree uses:
# ["git", "add", ...] / ["git", "commit", ...] / ["git", "push", ...].
GIT_MUTATE_RE='"git"[[:space:]]*,[[:space:]]*"(add|commit|push)"'

violations=0

for f in "${files[@]}"; do
    [[ -f "$f" ]] || continue
    [[ "$f" == "$CHOKE_POINT" || "$f" == */"$CHOKE_POINT" ]] && continue

    if ! is_open_allowed "$f"; then
        while IFS= read -r hit; do
            [[ -n "$hit" ]] || continue
            echo "fail: $f:$hit — raw open(w|a) outside the git_tools write choke point"
            violations=$((violations + 1))
        done < <(grep -nE "$OPEN_WRITE_RE" "$f" 2>/dev/null || true)
    fi

    if ! is_git_allowed "$f"; then
        while IFS= read -r hit; do
            [[ -n "$hit" ]] || continue
            echo "fail: $f:$hit — raw git add/commit/push outside the git_tools commit choke point"
            violations=$((violations + 1))
        done < <(grep -nE "$GIT_MUTATE_RE" "$f" 2>/dev/null || true)
    fi
done

if [[ $violations -gt 0 ]]; then
    echo
    echo "write choke-point violation: $violations site(s) bypass core/git_tools.py."
    echo "Route the write through git_tools.write_memory_file / move_memory_file and"
    echo "the commit through git_tools.commit_memory_file(s), or — for a genuine"
    echo "non-memory write — add an explicit, commented entry to OPEN_ALLOWED /"
    echo "GIT_ALLOWED in this script."
    exit 1
fi

exit 0
