---
name: Bug report
about: Something broke, printed unexpected JSON, or failed against the live API
title: ""
labels: bug
---

## What happened

## Command

```bash
naturgy ...
```

## Environment

- `naturgy --version`:
- OS and version:
- Installed with (uv tool / pipx / Homebrew / checkout):

## Error output

Paste the JSON error written to stderr, with values redacted.

## Redaction checklist

- [ ] No tokens, `tokens.json`, `.local-secrets/` or `sessionStorage` exports
- [ ] No NIF/NIE, CUPS, IBAN, addresses, invoice numbers or amounts
- [ ] No invoice PDFs and no `--raw` output
