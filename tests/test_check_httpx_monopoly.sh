#!/bin/bash
# tests/test_check_httpx_monopoly.sh — bash tests for the ADR-010 HTTP-layer
# monopoly enforcer.
#
# The companion to tests/test_check_shipping_{paths,leaks}.sh. It exists because
# check-httpx-monopoly.sh spent months in the tree enforcing nothing: it was
# wired to no workflow, and it aborted on macOS's system bash 3.2 (an empty
# array expanded under `set -u`). Neither failure was visible, because nothing
# ran the script and nothing tested it. A linter with no test is a linter that
# can rot silently — which is exactly what happened.
#
# Verifies:
#   - the clean repo passes (exit 0)
#   - a direct httpx import in a scanned file fails (exit 1)
#   - the allowed client (palinode/cli/_api.py) may use httpx
#   - out-of-scope trees (core/, ingest/, migration/) are NOT scanned
#   - the script survives an empty GRANDFATHERED array under `set -u`
#
# This script does NOT depend on pytest. Invoke directly:
#   bash tests/test_check_httpx_monopoly.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK="$REPO_ROOT/scripts/check-httpx-monopoly.sh"

if [ ! -f "$CHECK" ]; then
    echo "FAIL: scripts/check-httpx-monopoly.sh missing"
    exit 1
fi

pass=0
fail=0

ok() { echo "  ok: $1"; pass=$((pass + 1)); }
ng() { echo "  FAIL: $1"; fail=$((fail + 1)); }

cleanup() { rm -f "$REPO_ROOT/palinode/cli/_httpx_monopoly_probe.py"; }
trap cleanup EXIT

echo "== check-httpx-monopoly =="

# ── 1. clean tree passes ─────────────────────────────────────────────────────
if bash "$CHECK" >/dev/null 2>&1; then
    ok "clean tree passes"
else
    ng "clean tree should pass but did not (exit $?)"
fi

# ── 2. a direct httpx user in palinode/cli/ is caught ────────────────────────
cat > "$REPO_ROOT/palinode/cli/_httpx_monopoly_probe.py" <<'PY'
import httpx


def probe():
    return httpx.get("http://example.invalid")
PY
if bash "$CHECK" >/dev/null 2>&1; then
    ng "a direct httpx call in palinode/cli/ was NOT caught"
else
    ok "direct httpx use in palinode/cli/ is caught"
fi
cleanup

# ── 3. the sanctioned client is exempt ───────────────────────────────────────
# _api.py imports and uses httpx on every line of its body; if the allowlist
# regressed, test 1 would already fail. Assert the allowlist names it.
if grep -q 'palinode/cli/_api.py' "$CHECK"; then
    ok "palinode/cli/_api.py is on the allowlist"
else
    ng "palinode/cli/_api.py missing from the allowlist"
fi

# ── 4. out-of-scope trees are not scanned ────────────────────────────────────
# core/, ingest/ and migration/ talk to Ollama, not to the palinode API, and are
# deliberately outside ADR-010's scope. They DO import httpx; if the scan scope
# widened by accident the clean-tree case (test 1) would fail. Assert the scope
# is still the CLI only, so a future widening is a deliberate edit.
if grep -q 'palinode/cli/\*\.py' "$CHECK"; then
    ok "scan scope is still palinode/cli/*.py only"
else
    ng "scan scope changed — core/ingest/migration use httpx legitimately"
fi

# ── 5. empty-array guard: the bash 3.2 regression ────────────────────────────
# `GRANDFATHERED=()` is empty by design. Under `set -u`, bash 3.2 treats
# "${arr[@]}" on an empty array as unbound and aborts, which made the script
# crash rather than answer. Assert the guarded expansion form is present.
if grep -q 'GRANDFATHERED\[@\]+' "$CHECK"; then
    ok "empty-array expansion is guarded for bash 3.2"
else
    ng "unguarded \${GRANDFATHERED[@]} — will abort on bash 3.2 under set -u"
fi

echo
echo "passed: $pass  failed: $fail"
[ "$fail" -eq 0 ] || exit 1
