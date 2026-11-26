#!/bin/sh
# Run the offline checks: lint and tests with coverage.
# Usage: ./scripts/check.sh [pytest args...]
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

PYTHON="$ROOT/.venv/bin/python"
RUFF="$ROOT/.venv/bin/ruff"
if [ ! -x "$PYTHON" ] || [ ! -x "$RUFF" ]; then
    echo "missing .venv tools; run: uv sync" >&2
    exit 1
fi

"$RUFF" check .
"$PYTHON" -m pytest -q --cov=naturgy --cov-report=term-missing "$@"
