# Security policy

`naturgy-cli` signs in to an undocumented Naturgy API with your own credentials and keeps a session
token on disk, so bug reports can easily carry personal data. Please treat them as sensitive.

## Reporting a vulnerability

Do **not** open a public issue. Use GitHub's private reporting form:

<https://github.com/Jotis86/naturgy_cli/security/advisories/new>

Include the affected version (`naturgy --version`), the exact command, and what happened. If you
cannot use GitHub advisories, open a minimal issue asking for a private channel, without details.

## What to redact

Never paste, in issues, pull requests or advisories:

- tokens or `sessionStorage` exports (`token`, `idToken`, `refreshToken`)
- `~/.config/naturgy-cli/tokens.json`, anything in `.local-secrets/`, or `scripts/probe.py` output
- NIF/NIE, CUPS, IBAN, addresses, invoice numbers, amounts or invoice PDFs
- `--raw` output, which adds the original API objects

Error messages include Naturgy's error codes only; those are safe to share.

## In scope

- Credentials written with weak permissions, logged, or leaked in output
- A claim sent without an explicit confirmation (`--yes`)
- Unsafe handling of `--attach` paths or of the token file lock
- Request pacing problems that could get an account blocked

## Out of scope

- The Naturgy API itself and any change it makes without notice; this project does not control it
- The fact that automated access may violate Naturgy's terms of service
- Missing features: use issues for those
