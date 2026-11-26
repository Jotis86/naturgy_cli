#!/bin/sh
# Run the CLI from this checkout without relying on the editable-install .pth.
# Usage: ./scripts/run.sh --help
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" exec "$ROOT/.venv/bin/python" -m naturgy "$@"
