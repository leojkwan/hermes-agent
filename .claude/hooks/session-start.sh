#!/bin/bash
# Claude Code cloud sessions only: create .venv from the lockfile, with the dev
# extra (pytest, ruff), so scripts/run_tests.sh and ruff work.
# Running the gateway needs keys and an always-on host, so it stays on the Mac.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

# Put the locked environment first on PATH for later commands (ruff, pytest).
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  printf 'export PATH=%q:"$PATH"\n' "$PWD/.venv/bin" >> "$CLAUDE_ENV_FILE"
fi

# `uv sync --locked` is idempotent: it fails if uv.lock is stale instead of
# rewriting it, and does nothing when .venv already matches.
uv sync --locked --extra dev
