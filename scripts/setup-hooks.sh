#!/usr/bin/env bash
#
# setup-hooks.sh — run ONCE after cloning to activate the version-controlled
# git hooks. Points git at scripts/git-hooks (so the pre-commit smoke test
# runs on every commit) and makes the scripts executable.
#
set -euo pipefail
cd "$(dirname "$0")/.."

git config core.hooksPath scripts/git-hooks
chmod +x scripts/*.sh scripts/git-hooks/* 2>/dev/null || true

echo "✓ git hooks activated — core.hooksPath = scripts/git-hooks"
echo "  pre-commit smoke test will now run on every commit."
