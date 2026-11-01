#!/usr/bin/env sh
set -eu
python -m compileall -q harness tests/harness
if command -v ruff >/dev/null 2>&1; then ruff check harness tests/harness; else echo "SKIP  ruff not installed"; fi
if python -c 'import mypy' >/dev/null 2>&1; then python -m mypy harness; else echo "SKIP  mypy not installed"; fi
# (no roadmap here: `harness validate` runs inside each project)
python -m pytest -q tests/harness
