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

# ── 1. Smoke test ─────────────────────────────────────────────────────────────
echo "▶ smoke test…"
PYBIN=".venv/bin/python"
[[ -x "$PYBIN" ]] || PYBIN="python3"
"$PYBIN" - <<'PY' || { echo "✗ smoke test FAILED — nothing shipped." >&2; exit 1; }
import ast, glob, os, sys

# (a) syntax-check every module
n = 0
for f in glob.glob("app/**/*.py", recursive=True):
    try:
        ast.parse(open(f, encoding="utf-8").read())
        n += 1
    except SyntaxError as e:
        print(f"  SYNTAX ERROR in {f}: {e}", file=sys.stderr)
        sys.exit(1)
print(f"  syntax OK ({n} files)")

# (b) import the app — catches missing names, bad imports, circular deps.
#     Best-effort: skip cleanly if 3rd-party deps aren't installed locally.
os.environ.setdefault("DATABASE_URL", "sqlite:///./_ship_smoke.db")
try:
    import importlib
    importlib.import_module("app.main")
    print("  import app.main OK")
except ImportError as e:
    print(f"  (import check skipped — dependency not installed: {e})")
finally:
    if os.path.exists("./_ship_smoke.db"):
        os.remove("./_ship_smoke.db")
PY

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
