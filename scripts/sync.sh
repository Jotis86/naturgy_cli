#!/bin/sh
# `uv sync` plus the macOS editable-install repair.
# Usage: ./scripts/sync.sh [uv sync args...]
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

uv sync "$@"
"$ROOT/scripts/fix-venv.sh"
