# naturgy-cli

JSON-first CLI for the owner's Naturgy España (Omega) account. Read `README.md` before changing
behavior.

## Commands

- Run the CLI from this checkout with `./scripts/run.sh ...` (it puts `src/` on `PYTHONPATH`).
- Do **not** use `uv run naturgy` for development: on macOS the editable `.pth` can carry
  `UF_HIDDEN`, and Python 3.12+ skips hidden `.pth` files (`ModuleNotFoundError: No module named
  'naturgy'`). Use `./scripts/run.sh`, or repair the venv with `./scripts/fix-venv.sh`.
- Prefer `./scripts/sync.sh` over bare `uv sync`; it also clears the hidden flag on macOS.
- Tests and lint: `./scripts/check.sh`.
- Stable local install: `uv tool install .` (a wheel, no editable `.pth`).

## Safety

- `naturgy invoices claim` sends a real claim to Naturgy. Use `--dry-run` unless the user
  explicitly asks to send it.
- The `naturgy battery` commands are read-only. Do not add mode changes or balance assignments
  without explicit confirmation.
- Never commit `.local-secrets/`, tokens, invoice PDFs, or command output containing personal data.
- The API is undocumented and rate-limited. Keep the pacing and do not add automatic retries for
  the claim request.
