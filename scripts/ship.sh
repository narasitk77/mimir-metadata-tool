#!/usr/bin/env bash
#
# ship.sh — one command to close out a finished feature.
#
#   ./scripts/ship.sh "feat: short message" ["optional longer body"]
#
# Does, in order:
#   1. Smoke test  — parse every app/**/*.py, then try to import app.main
#                    (catches syntax + import errors before they reach prod)
#   2. Log         — append a timestamped line to UPDATES.log
#   3. Commit      — git add -A + commit (.env is gitignored, never staged)
#   4. Push        — git push to the current branch's remote
#
# Exit non-zero (and ship NOTHING) if the smoke test fails.
#
set -euo pipefail

# Always operate from the repo root, regardless of where we're called from.
cd "$(dirname "$0")/.."

MSG="${1:-}"
BODY="${2:-}"
if [[ -z "$MSG" ]]; then
  echo "usage: ./scripts/ship.sh \"commit message\" [\"longer body\"]" >&2
  exit 1
fi

# ── 1. Smoke test (shared with the pre-commit hook) ──────────────────────────
echo "▶ smoke test…"
"$(dirname "$0")/smoke-test.sh" || { echo "✗ smoke test FAILED — nothing shipped." >&2; exit 1; }

# ── 2. Bail if there's nothing to ship ────────────────────────────────────────
if [[ -z "$(git status --porcelain)" ]]; then
  echo "✗ no changes — nothing to ship."
  exit 0
fi

# ── 3. Append to the update log (rides in the same commit) ────────────────────
TS="$(date -u +'%Y-%m-%d %H:%M:%S UTC')"
echo "${TS}  ${MSG}" >> UPDATES.log

# ── 4. Commit ─────────────────────────────────────────────────────────────────
git add -A
FULL="$MSG"
[[ -n "$BODY" ]] && FULL="$MSG"$'\n\n'"$BODY"
FULL="$FULL"$'\n\n'"Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
git commit -m "$FULL"

# ── 5. Push ───────────────────────────────────────────────────────────────────
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git push origin "$BRANCH"

echo "✓ shipped $(git rev-parse --short HEAD) → origin/${BRANCH}"
echo "  $MSG"
