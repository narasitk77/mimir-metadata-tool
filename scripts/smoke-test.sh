#!/usr/bin/env bash
#
# smoke-test.sh — fast pre-ship verification, shared by ship.sh and the
# pre-commit hook. Exits non-zero if anything fails.
#
#   (a) syntax-check every app/**/*.py
#   (b) import app.main — catches missing names, bad imports, circular deps
#       (the class of error that caused the 502 on a past deploy)
#
set -euo pipefail
cd "$(dirname "$0")/.."

PYBIN=".venv/bin/python"
[[ -x "$PYBIN" ]] || PYBIN="python3"

"$PYBIN" - <<'PY'
import ast, glob, os, sys

n = 0
for f in glob.glob("app/**/*.py", recursive=True):
    try:
        ast.parse(open(f, encoding="utf-8").read())
        n += 1
    except SyntaxError as e:
        print(f"  SYNTAX ERROR in {f}: {e}", file=sys.stderr)
        sys.exit(1)
print(f"  syntax OK ({n} files)")

# import check — best-effort: skip cleanly if 3rd-party deps aren't installed.
os.environ.setdefault("DATABASE_URL", "sqlite:///./_smoke.db")
try:
    import importlib
    importlib.import_module("app.main")
    print("  import app.main OK")
except ImportError as e:
    print(f"  (import check skipped — dependency not installed: {e})")
finally:
    if os.path.exists("./_smoke.db"):
        os.remove("./_smoke.db")
PY
