# naturgy-cli

Follow `AGENTS.md`. The essentials:

- Run the CLI with `./scripts/run.sh ...`.
- Do not use `uv run naturgy`; macOS may hide the editable `.pth`. Repair with
  `./scripts/fix-venv.sh`, or use `./scripts/sync.sh` instead of bare `uv sync`.
- Tests and lint: `./scripts/check.sh`.
- Claims are real: use `--dry-run` unless the user explicitly asks to send one.
- Battery commands are read-only; do not add mode/assignment writes without explicit confirmation.
- Never commit `.local-secrets/`, tokens, invoice PDFs, or personal-data output.
