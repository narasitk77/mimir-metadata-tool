#!/usr/bin/env bash
#
# auto-ship-hook.sh — Claude Code **Stop hook** (safety net "กันลืม").
#
# When Claude finishes a turn, if the mimir repo still has uncommitted changes,
# ship them automatically (verify → log → commit → push) with an auto-message.
#
# Normally a NO-OP: during a turn Claude runs ship.sh with a real, descriptive
# message, so the tree is already clean when this fires. This only rescues the
# "forgot to ship" case so finished work is never lost.
#
# Wired via settings.json → hooks.Stop. Designed to NEVER block Claude and
# NEVER loop: it always exits 0, and bails immediately if re-entered.
#
REPO="/Users/narasitk/Desktop/mimir-metadata-tool"

# Read the Stop-hook payload from stdin; if we're already inside a stop-hook
# continuation, do nothing (defensive — we never block, but belt-and-braces).
PAYLOAD="$(cat 2>/dev/null || true)"
case "$PAYLOAD" in
  *'"stop_hook_active":true'*) exit 0 ;;
esac

cd "$REPO" 2>/dev/null || exit 0

# Clean tree → nothing to do (the normal path).
[[ -z "$(git status --porcelain)" ]] && exit 0

COUNT="$(git status --porcelain | wc -l | tr -d ' ')"
FILES="$(git status --porcelain | awk '{print $2}' | head -6 | tr '\n' ' ')"
TS="$(date '+%Y-%m-%d %H:%M')"
MSG="auto-ship: caught ${COUNT} uncommitted file(s) at turn end — ${TS}"
BODY="Stop-hook safety net (ship.sh was not run manually this turn). Files: ${FILES}"

# ship.sh runs the smoke test (+ pre-commit hook re-runs it). If verification
# fails, ship.sh exits non-zero and the changes are left for manual review —
# we still exit 0 here so Claude is never blocked.
"$REPO/scripts/ship.sh" "$MSG" "$BODY" >/tmp/mimir-auto-ship.log 2>&1 || true
exit 0
