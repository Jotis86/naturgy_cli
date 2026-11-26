#!/bin/sh
# Repair the editable install after macOS marks its .pth file as hidden.
# Python 3.12+ skips hidden .pth files, so `import naturgy` fails until the flag is cleared.
# Usage: ./scripts/fix-venv.sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VENV="$ROOT/.venv"

if [ ! -x "$VENV/bin/python" ]; then
    echo "no virtualenv at $VENV; run: uv sync" >&2
    exit 1
fi

if [ "$(uname -s)" = "Darwin" ] && command -v chflags >/dev/null 2>&1; then
    chflags -R nohidden "$VENV"
fi

if ! "$VENV/bin/python" -c "import naturgy" >/dev/null 2>&1; then
    echo "naturgy is still not importable; use ./scripts/run.sh or: uv sync --no-editable" >&2
    exit 1
fi

printf 'ok: naturgy imports from %s\n' \
    "$("$VENV/bin/python" -c 'import naturgy; print(naturgy.__file__)')"
