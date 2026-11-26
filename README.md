# naturgy-cli

A JSON-first command-line client for **your own** Naturgy España account, over the Área Clientes
"Omega" API (`omegav2.naturgy.es`). It talks directly to Naturgy's REST API over HTTPS, so no
browser is needed at runtime. Its JSON output is meant for scripts and coding agents.

> This is an unofficial client of an undocumented API. It is not affiliated with or endorsed by
> Naturgy. Use it only with your own account. Naturgy's terms of service may restrict automated
> access; you are responsible for how you use it.

## Features

| Command | Description | Status |
| --- | --- | --- |
| `naturgy auth login` | Sign in with NIF/NIE + password and save the session | ✅ |
| `naturgy auth status` | Token expiry, account and optional live check | ✅ |
| `naturgy auth logout` | Revoke the session (best effort) and delete the local session | ✅ |
| `naturgy auth import` | Import a browser `sessionStorage` token export | ✅ |
| `naturgy me` | Holder, contacts, preferred contact and permissions | ✅ |
| `naturgy supplies` | Supplies, contracts, CUPS, addresses, billing accounts, rates | ✅ |
| `naturgy consumption` | Electricity, gas and solar consumption series | ✅ |
| `naturgy readings` | Meter readings per contract | ✅ |
| `naturgy invoices list` | Invoices across supplies, newest first | ✅ |
| `naturgy invoices show` | Detail for one invoice | ✅ |
| `naturgy invoices download` | Invoice PDFs: one, the latest, or all | ✅ |
| `naturgy debt` | Pending invoices and other debt | ✅ |
| `naturgy cases` | Claims and cases already filed with Naturgy | ✅ |
| `naturgy invoices claim` | File a bill review (**sends a claim**, after confirmation) | ✅ |

Everything is read-only except `naturgy invoices claim`.

## Requirements

- A Naturgy España residential account you can log in to at <https://omegav2.naturgy.es>
- macOS or Linux
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)

## Installation

From a checkout (recommended while the API mapping is being validated):

```bash
git clone <your-repo> naturgy-cli
cd naturgy-cli
uv sync
./scripts/run.sh --help          # run this checkout without relying on the editable .pth
# optional: install this checkout as a tool (a regular wheel, no editable .pth)
uv tool install .
```

`scripts/run.sh` puts `src/` on `PYTHONPATH` and runs `python -m naturgy`, so code changes apply
immediately. If `uv run naturgy` fails with `ModuleNotFoundError` after `uv sync`, that is a macOS
`hidden` flag on the editable `.pth`; use `./scripts/run.sh`, or run
`uv sync --no-editable` and then `uv run --no-sync naturgy`.

## Authentication

### Sign in

```bash
naturgy auth login
```

It prompts for your **NIF/NIE** and password (hidden). It normalizes them the same way the web app
does: the NIF loses spaces, dots and hyphens and is uppercased, and literal spaces are removed from
the password. The password is used for one request and is never stored or logged. On success it
writes the session to the token file.

If Naturgy refuses the sign-in, the error includes its code (`PS01` means it does not recognize the
user; `CG09`/`PS04` mean the NIF or password is not valid; `CG08` means too many attempts).

`naturgy auth status` shows the token expiry (decoded from the JWT, offline) and exits `3` when the
token has expired. `naturgy auth status --check` also calls the API to prove the session works.

### Browser token export

If you would rather not type your password into the CLI, log in at <https://omegav2.naturgy.es>,
open the developer tools and copy the `user` entry from `sessionStorage`, then:

```bash
naturgy auth import ./session-user.json
```

The export is the object with `token`, `idToken`, `refreshToken`, `userId`, `clientCode` and
`roleTypeId`.

### Token file location

The CLI looks for the file in this order, and `auth login` writes to the same place:

1. `--tokens PATH`
2. `$NATURGY_TOKENS_FILE`
3. An existing `./.local-secrets/tokens.json` (a checkout's local session)
4. An existing `~/.config/naturgy-cli/tokens.json`, or
   `$XDG_CONFIG_HOME/naturgy-cli/tokens.json`
5. A source checkout without a session yet: `./.local-secrets/tokens.json`
6. An installed copy without a session yet: `~/.config/naturgy-cli/tokens.json`

The file is written atomically with mode `0600`. Concurrent commands hold a lock on
`tokens.json.lock` while they sign in or write.

### No refresh token

Naturgy's Omega API exposes no refresh endpoint (the web app stores a `refreshToken` but never
exchanges it). When the access token expires you have to sign in again with `naturgy auth login` or
import a fresh browser token. `naturgy auth status` tells you when that is needed.

## Usage

Every command writes JSON to stdout. `--version` and `--help` print plain text. Prompts and errors
go to stderr, so stdout stays valid JSON.

```bash
naturgy --version
naturgy auth login
naturgy auth status
naturgy auth status --check

naturgy me
naturgy supplies
naturgy supplies --supply 1234          # service account, billing ref, contract, CUPS or suffix

naturgy consumption                    # last 12 months, monthly
naturgy consumption --period day --from 2026-01-01 --to 2026-02-01
naturgy consumption --family luz --family gas

naturgy readings
naturgy debt
naturgy cases

naturgy invoices list
naturgy invoices show <invoice-id>
naturgy invoices download                          # latest PDF into the current directory
naturgy invoices download all --dir ~/facturas
naturgy invoices download <invoice-id>

naturgy invoices claim latest -m "No reconozco el cargo de 12,40 EUR" --reason wrong-charge
naturgy invoices claim <invoice-id> -m "..." --attach foto.png --dry-run
naturgy invoices claim <invoice-id> -m "..." --yes
```

Global options:

- `--tokens PATH`: session file to use.
- `--supply SELECTOR`: limit to a supply (service account id, billing ref, contract number, CUPS
  or a unique suffix).
- `--raw`: include the original API object as `source` (debugging; may contain personal data).
- `--language es`: `x-language` header.

### Output

The CLI normalizes the API's camelCase payloads into stable snake_case shapes. Every command has a
top-level `count` where it returns a list. With `--raw`, the original object is added as `source`.

```json
// naturgy supplies
{
  "supplies": [
    {
      "service_account_id": "…",
      "client_id": "…",
      "address": "Calle Ejemplo 1 28001 Madrid",
      "address_details": {"zipCode": "28001", "city": "Madrid"},
      "billing_accounts": [
        {"billing_account_id": "…", "ref_num": "…", "is_direct_payment": true, "iban_masked": "ES12 **** **** 3456", "pay_day": "5", "bill_delivery_method": "online"}
      ],
      "products": [
        {"contract_id": "…", "contract_number": "…", "product_family": "luz", "product_name": "…", "product_code": "…", "status": "03", "is_active": true, "rate": "2.0TD", "cups": "ES…", "powers": {"powerPlain": 4.6, "powerValley": 4.6}, "billing_account_id": "…"}
      ]
    }
  ],
  "count": 1
}
```

```json
// naturgy invoices list
{
  "invoices": [
    {
      "invoice_id": "…",
      "type": "NC",
      "status": "03",
      "issued_date": "2026-10-01",
      "billing_date": "2026-10-01",
      "amount": 54.21,
      "pending_amount": 0.0,
      "currency": "EUR",
      "is_pending": false,
      "pdf_document_id": "…",
      "has_pdf": true,
      "service_account_id": "…",
      "billing_account_id": "…",
      "ref_num": "…",
      "product_family": "luz",
      "relations": [{"contract_number": "…", "product_family_id": "luz", "invoice_atr_id": "…"}]
    }
  ],
  "count": 1,
  "is_contingency_mode": false
}
```

Invoice PDFs are saved as `factura_YYYY-MM_<id>.pdf`. Existing files are never overwritten; a file
appears under its final name only when it is complete, and it is written with mode `0600`.

`unlimited`/missing numeric values are `null` rather than a made-up zero. Dates are passed through
as Naturgy sends them (usually ISO `YYYY-MM-DD`).

### Bill reviews (claims)

`naturgy invoices claim TARGET --message TEXT` files a bill review ("Revisa tu factura") about one
invoice, the same request the web app sends. `TARGET` is an invoice id from `invoices list` or
`latest`. The message is limited to 4,000 characters.

Reasons (`--reason`):

| Key | Code | Meaning |
| --- | --- | --- |
| `wrong-reading` | 01 | Lectura incorrecta |
| `wrong-invoice` | 02 | Factura incorrecta |
| `wrong-charge` | 03 | Cargo incorrecto (default) |
| `others` | 05 | Otros |
| `download-invoice` | 06 | Descarga de factura |
| `sending-invoice` | 07 | Envío de factura |
| `billed-invoice` | 08 | Factura ya cobrada (contract-level; needs `--supply`) |

Attachments (`--attach`, repeatable) must be PNG, JPEG or PDF, up to 2 MB each and 10 MB total.

- `--dry-run` runs the read-only checks, prints the exact JSON body (attachment bytes redacted) and
  sends nothing.
- On a terminal it shows the invoice, reason, attachments and full text, then asks
  `¿Enviar esta reclamación a Naturgy? [s/N]`. Only `s` / `si` / `sí` sends it.
- Without a terminal it refuses unless `--yes` is passed.
- A claim is sent once and never retried. If the outcome is uncertain (timeout, 5xx, or a response
  without a ticket), it exits `5` with `outcome_unknown`; run `naturgy cases` before trying again.
- If the invoice's account already has an open claim, it exits `5` with `claim_already_open` and
  sends nothing.

## Errors and exit codes

Errors go to stderr as JSON:

```json
{"error":{"type":"auth_expired","message":"not signed in: run `naturgy auth login`","exit_code":3}}
```

| Exit code | Types | Meaning |
| --- | --- | --- |
| 0 | | Success |
| 1 | `internal` | Unexpected error (only the exception type is reported) |
| 2 | `usage` | Invalid arguments, dates, reasons or attachments |
| 2 | `cancelled` | The claim was not confirmed; nothing was sent |
| 3 | `auth_expired` | Not signed in, or the session expired/was rejected |
| 3 | `login_failed` | Sign-in refused (NIF/password or another check); the `code` says which |
| 3 | `access_denied` | That account or supply was refused, not the session |
| 5 | `blocked`, `upstream_unavailable` | Bot protection, rate limit, timeout or Naturgy 5xx |
| 5 | `claim_already_open` | The invoice's account already has an open claim; nothing was sent |
| 5 | `outcome_unknown` | A claim may or may not have been filed; check `naturgy cases` |
| 7 | `api_changed` | Unexpected status or response shape; the API probably changed |

Requests are sent one at a time with a short delay and are never retried automatically. The only
automatic request is the initial `/clients` lookup that scopes the session.

## Limitations

- The API is undocumented and may change without notice. `--raw` and `scripts/probe.py` exist to
  make the mapping easy to update.
- There is no token refresh; see above.
- The API sits behind a bot-protection layer that can return empty `200` responses. The CLI sends
  browser-like headers and reports that case as `blocked` (exit 5), never as empty data.
- The client document number and tokens are sensitive. The token file is `0600` and values are
  never printed. Output may contain personal data (names, addresses, CUPS, invoice ids); do not
  commit it. `--raw` may include the full IBAN; the normalized output masks it.
- This project is not affiliated with Naturgy.

## Development

```bash
uv sync
uv run --no-sync pytest
```

The tests are offline and use synthetic fixtures. Live read-only tests are opt-in:

```bash
NATURGY_LIVE=1 uv run --no-sync pytest -m live
```

### Discovery probe

To validate the wire shapes against a real account without sharing personal data:

```bash
./scripts/run.sh auth login          # once; saves .local-secrets/tokens.json
uv run python scripts/probe.py       # reuses that session
```

If there is no usable session it prompts for NIF/NIE and password once. `--save-tokens` also saves
the session in that case. The probe calls the read-only endpoints and writes
`.local-secrets/shapes.json` with **only keys, types, array lengths and sample shapes**. It never
sends a claim, never stores the password and never prints a token. The file is gitignored and mode
`0600`.

```
src/naturgy/
  cli.py          Typer app: commands and exit codes
  auth.py         sign-in, status, logout, browser token import
  client.py       OmegaClient: headers, pacing, error mapping, WAF detection
  api.py          read operations, normalization, PDFs and filing a claim
  models.py       token-set model
  output.py       JSON rendering
  tokens.py       token file, lock, JWT expiry
  errors.py       typed errors and exit codes
  config.py       hosts, headers, token-file lookup
scripts/probe.py  read-only shape discovery
tests/            offline unit tests and opt-in live tests
```

## License

MIT. See [LICENSE](LICENSE).
