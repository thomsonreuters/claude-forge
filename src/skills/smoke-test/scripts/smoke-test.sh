#!/usr/bin/env bash
# Forge smoke test -- read-only installation verification.
# Runs a fixed whitelist of probes and asserts no filesystem side effects.
#
# Usage:
#   bash smoke-test.sh
#
# Exit codes:
#   0  All checks passed
#   1  One or more checks failed

set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on PATH. Smoke test requires python3 for mtime snapshots." >&2
    exit 1
fi

PASS=0
FAIL=0
RESULTS=()

# --- Snapshot "must not change" paths ---
snapshot_mtime() {
    if [ -e "$1" ]; then
        python3 -c 'import os,sys; print(int(os.path.getmtime(sys.argv[1])))' "$1"
    else
        echo "absent"
    fi
}

SNAP_SETTINGS=$(snapshot_mtime "$HOME/.claude/settings.json")
SNAP_LOCAL=$(snapshot_mtime "$HOME/.claude/settings.local.json")
SNAP_COMMANDS=$(snapshot_mtime "$HOME/.claude/commands")
SNAP_AGENTS=$(snapshot_mtime "$HOME/.claude/agents")
SNAP_SKILLS=$(snapshot_mtime "$HOME/.claude/skills")
SNAP_FORGE=$(snapshot_mtime "$HOME/.forge")
SNAP_INSTALLED=$(snapshot_mtime "$HOME/.forge/installed.json")

# --- Probe helpers ---
check() {
    local name="$1"
    shift
    if output=$("$@" 2>&1); then
        PASS=$((PASS + 1))
        local short="${output:0:60}"
        RESULTS+=("$(printf '  %-28s [PASS]  %s' "$name" "$short")")
    else
        FAIL=$((FAIL + 1))
        local short="${output:0:60}"
        RESULTS+=("$(printf '  %-28s [FAIL]  %s' "$name" "$short")")
    fi
}

check_file() {
    local name="$1"
    local path="$2"
    local desc="$3"
    if [ -f "$path" ]; then
        PASS=$((PASS + 1))
        RESULTS+=("$(printf '  %-28s [PASS]  %s' "$name" "$desc")")
    else
        FAIL=$((FAIL + 1))
        RESULTS+=("$(printf '  %-28s [FAIL]  not found' "$name")")
    fi
}

# --- Run probes (read-only only -- no forge subcommands that trigger pending-work queue) ---
check "forge on PATH" command -v forge
check "forge --version" forge --version
check_file "installed.json" "$HOME/.forge/installed.json" "exists"

# Direct file read -- no Forge CLI invocation, no startup side effects
if [ -f "$HOME/.forge/installed.json" ] && command -v jq >/dev/null 2>&1; then
    check "tracking version" jq -r '.version // "unknown"' "$HOME/.forge/installed.json"
fi

# --- Assert no side effects ---
assert_unchanged() {
    local name="$1"
    local path="$2"
    local before="$3"
    local after
    after=$(snapshot_mtime "$path")
    if [ "$before" = "$after" ]; then
        PASS=$((PASS + 1))
        RESULTS+=("$(printf '  %-28s [PASS]  unchanged' "$name")")
    else
        FAIL=$((FAIL + 1))
        RESULTS+=("$(printf '  %-28s [FAIL]  MODIFIED (%s -> %s)' "$name" "$before" "$after")")
    fi
}

assert_unchanged "settings.json intact" "$HOME/.claude/settings.json" "$SNAP_SETTINGS"
assert_unchanged "settings.local intact" "$HOME/.claude/settings.local.json" "$SNAP_LOCAL"
assert_unchanged "commands dir intact" "$HOME/.claude/commands" "$SNAP_COMMANDS"
assert_unchanged "agents dir intact" "$HOME/.claude/agents" "$SNAP_AGENTS"
assert_unchanged "skills dir intact" "$HOME/.claude/skills" "$SNAP_SKILLS"
assert_unchanged "~/.forge intact" "$HOME/.forge" "$SNAP_FORGE"
assert_unchanged "installed.json intact" "$HOME/.forge/installed.json" "$SNAP_INSTALLED"

# --- Print results ---
TOTAL=$((PASS + FAIL))
echo ""
echo "Forge Smoke Test"
echo "------------------------------------"
for line in "${RESULTS[@]}"; do
    echo "$line"
done
echo "------------------------------------"
echo "  $PASS/$TOTAL passed"

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "  Some checks failed. Run 'forge extension enable --scope user' to install."
    exit 1
fi
exit 0
