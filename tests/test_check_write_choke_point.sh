#!/bin/bash
# tests/test_check_write_choke_point.sh — bash tests for the mutation
# choke-point enforcer.
#
# The companion to tests/test_check_shipping_{paths,leaks}.sh and
# tests/test_check_httpx_monopoly.sh: the invariant core/git_tools.py states
# in its own module docstring — every memory-file write routes through
# write_memory_file / move_memory_file, every commit through
# commit_memory_file(s), never a repo-wide `git add *.md` sweep — was prose
# only until scripts/check-write-choke-point.sh existed to scan for a
# bypass. A linter with no test can rot silently, same lesson as the
# httpx-monopoly linter that ran wired to nothing for months.
#
# Verifies:
#   - the clean repo passes (exit 0)
#   - a raw open(path, "w") outside git_tools.py fails
#   - a raw open(path, "a") outside git_tools.py fails
#   - a raw `git add` / `git commit` / `git push` subprocess call outside
#     git_tools.py fails
#   - the allowed non-memory writes (audit logs, migration scratch files,
#     the CLI scaffolder, …) are exempt
#   - git_tools.py itself is exempt (it IS the choke point)
#   - the GIT_ALLOWED array survives bash 3.2's `set -u` empty-array trap
#
# This script does NOT depend on pytest. Invoke directly:
#   bash tests/test_check_write_choke_point.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK="$REPO_ROOT/scripts/check-write-choke-point.sh"

if [ ! -f "$CHECK" ]; then
    echo "FAIL: scripts/check-write-choke-point.sh missing"
    exit 1
fi

pass=0
fail=0

ok() { echo "  ok: $1"; pass=$((pass + 1)); }
ng() { echo "  FAIL: $1"; fail=$((fail + 1)); }

PROBE="$REPO_ROOT/palinode/_write_choke_point_probe.py"
cleanup() { rm -f "$PROBE"; }
trap cleanup EXIT

echo "== check-write-choke-point =="

# ── 1. clean tree passes ─────────────────────────────────────────────────────
if bash "$CHECK" >/dev/null 2>&1; then
    ok "clean tree passes"
else
    ng "clean tree should pass but did not"
fi

# ── 2. a raw open(w) outside git_tools.py is caught ──────────────────────────
cat > "$PROBE" <<'PY'
def bad_write(path, content):
    with open(path, "w") as f:
        f.write(content)
PY
if bash "$CHECK" "palinode/_write_choke_point_probe.py" >/dev/null 2>&1; then
    ng "a raw open(path, 'w') was NOT caught"
else
    ok "raw open(path, 'w') outside git_tools.py is caught"
fi
cleanup

# ── 3. a raw open(a) outside git_tools.py is caught ──────────────────────────
cat > "$PROBE" <<'PY'
def bad_append(path, content):
    with open(path, "a") as f:
        f.write(content)
PY
if bash "$CHECK" "palinode/_write_choke_point_probe.py" >/dev/null 2>&1; then
    ng "a raw open(path, 'a') was NOT caught"
else
    ok "raw open(path, 'a') outside git_tools.py is caught"
fi
cleanup

# ── 4. a raw `git add` subprocess call outside git_tools.py is caught ───────
cat > "$PROBE" <<'PY'
import subprocess

def bad_commit(rel, msg):
    subprocess.run(["git", "add", rel])
    subprocess.run(["git", "commit", "-m", msg])
PY
if bash "$CHECK" "palinode/_write_choke_point_probe.py" >/dev/null 2>&1; then
    ng "a raw ['git', 'add'/'commit'] subprocess call was NOT caught"
else
    ok "raw git add/commit subprocess call outside git_tools.py is caught"
fi
cleanup

# ── 5. a raw `git push` subprocess call outside git_tools.py is caught ──────
cat > "$PROBE" <<'PY'
import subprocess

def bad_push():
    subprocess.run(["git", "push"])
PY
if bash "$CHECK" "palinode/_write_choke_point_probe.py" >/dev/null 2>&1; then
    ng "a raw ['git', 'push'] subprocess call was NOT caught"
else
    ok "raw git push subprocess call outside git_tools.py is caught"
fi
cleanup

# ── 6. a benign file (no open(w|a), no git mutation) passes ─────────────────
cat > "$PROBE" <<'PY'
def read_only(path):
    with open(path) as f:
        return f.read()
PY
if bash "$CHECK" "palinode/_write_choke_point_probe.py" >/dev/null 2>&1; then
    ok "a read-only open() is not flagged"
else
    ng "a plain read-only open() was wrongly flagged"
fi
cleanup

# ── 7. git_tools.py itself is exempt (it IS the choke point) ────────────────
if bash "$CHECK" "palinode/core/git_tools.py" >/dev/null 2>&1; then
    ok "git_tools.py itself is exempt from both checks"
else
    ng "git_tools.py was wrongly flagged — it is the choke point, not a bypass"
fi

# ── 8. the allowlisted non-memory writes stay exempt ─────────────────────────
if grep -q 'palinode/core/audit.py' "$CHECK" && grep -q 'palinode/consolidation/write_time.py' "$CHECK"; then
    ok "the OPEN_ALLOWED allowlist names the known non-memory writers"
else
    ng "OPEN_ALLOWED missing an expected entry — check scripts/check-write-choke-point.sh"
fi
if bash "$CHECK" "palinode/core/audit.py" >/dev/null 2>&1; then
    ok "palinode/core/audit.py (allowlisted) passes"
else
    ng "palinode/core/audit.py should be allowlisted but was flagged"
fi

# ── 9. GIT_ALLOWED is empty by design and must not crash bash 3.2 ───────────
if grep -q 'GIT_ALLOWED\[@\]+' "$CHECK"; then
    ok "empty-array expansion (GIT_ALLOWED) is guarded for bash 3.2"
else
    ng "unguarded \${GIT_ALLOWED[@]} — will abort on bash 3.2 under set -u"
fi
if grep -q 'OPEN_ALLOWED\[@\]+' "$CHECK"; then
    ok "empty-array expansion (OPEN_ALLOWED) is guarded for bash 3.2"
else
    ng "unguarded \${OPEN_ALLOWED[@]} — will abort on bash 3.2 under set -u"
fi

echo
echo "passed: $pass  failed: $fail"
[ "$fail" -eq 0 ] || exit 1
