#!/usr/bin/env bash
# End-to-end test for agentize GitHub mode.
#
# Creates a unique public scratch repo on the owner's GitHub account, runs
#   NO_COLOR=1 python agentize.py --github --repos OWNER/NAME --dry-run
# against it, asserts the tool reports "generated (dry-run", then ALWAYS
# deletes the scratch repo — including on failure (EXIT trap).
#
# Usage: scripts/e2e_github.sh
# Works in git-bash on Windows and in POSIX shells on Linux/macOS CI.
# Skips (exit 0) when the `gh` CLI is missing or unauthenticated.

set -euo pipefail

OWNER="ahm3d-karim"

# --- Preflight: skip gracefully when gh is missing or unauthenticated --------
if ! command -v gh >/dev/null 2>&1 || ! gh auth status >/dev/null 2>&1; then
  echo "SKIP: gh not available/authenticated"
  exit 0
fi

NAME="agentize-e2e-$(date +%s)"
FULL="$OWNER/$NAME"

# --- Cleanup: ALWAYS delete the scratch repo, even on failure ----------------
cleanup() {
  if [[ -n "${NAME:-}" ]]; then
    gh repo delete "$FULL" --yes >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# --- Create the scratch repo --------------------------------------------------
echo "Creating scratch repo $FULL ..."
gh repo create "$NAME" --public --add-readme >/dev/null

# --- Run agentize GitHub mode against it (dry-run) ---------------------------
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" # repo root
echo "Running: NO_COLOR=1 python agentize.py --github --repos $FULL --dry-run"
set +e
OUTPUT="$(NO_COLOR=1 python agentize.py --github --repos "$FULL" --dry-run 2>&1)"
RC=$?
set -e

# --- Assert: dry-run of a repo without AGENTS.md prints "generated (dry-run" --
if [[ "$OUTPUT" == *"generated (dry-run"* ]]; then
  echo "PASS"
  echo "$OUTPUT"
  exit 0
fi
echo "FAIL"
echo "--- captured output (exit code $RC) ---"
echo "$OUTPUT"
echo "--- end captured output ---"
exit 1
