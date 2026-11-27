---
name: naturgy
description: Answers questions about the user's own Naturgy España account by running the `naturgy` CLI — invoices, electricity/gas/solar consumption, meter readings, pending debt, filed claims and the virtual battery. Use when the user asks about their Naturgy bill, how much energy they used, what they owe, or their battery balance.
when_to_use: The user mentions Naturgy, a factura, la luz, el gas, consumo, lectura del contador, deuda pendiente, reclamación, or batería virtual. Not for other energy providers.
---

# naturgy

`naturgy` is a CLI for the user's **own** Naturgy España (Omega) account. It is already installed
(`brew install Jotis86/tap/naturgy`, or `uv tool install naturgy-cli`). It talks to Naturgy's real
API: every command prints JSON to stdout and JSON errors to stderr with a non-zero exit code.
`--version` and `--help` are the only plain-text output.

## Session

Check before answering anything about the account:

```bash
naturgy auth status
```

Exit code 0 means there is a valid session; **exit code 3 means it expired**. Do not run
`naturgy auth login`: it prompts for the password on a terminal and the password must be typed by
the user, never by you. If the session expired, say so and ask the user to run `naturgy auth login`
themselves.

## Commands

| Command | Returns |
| --- | --- |
| `naturgy me` | Holder, contacts, preferred contact, permissions |
| `naturgy supplies` | `supplies[]`: address, CUPS, contracts, rates, billing accounts, masks IBAN |
| `naturgy consumption` | `series[]` per supply and family; monthly by default |
| `naturgy readings` | `readings[]` per contract |
| `naturgy debt` | `accounts[]` with `total_pending` and `items[]` |
| `naturgy cases` | `cases[]`: claims already filed |
| `naturgy invoices list` | `invoices[]`, newest first |
| `naturgy invoices show <id>` | One invoice with its line items |
| `naturgy invoices download [<id>\|latest\|all]` | Saves PDFs, returns `directory` and `invoices[]` |
| `naturgy battery status` | `balance`, `currency`, `is_active`, `breakdown[]` |
| `naturgy battery history` | `movements[]` (charges, discharges, expirations) |
| `naturgy battery evolution` | Monthly evolution of the balance |
| `naturgy battery assignments` | Balance assigned per supply point |

Useful flags:

- `--supply <selector>` limits a command to one supply (service account id, billing ref, contract
  number, CUPS, or a unique suffix).
- `naturgy consumption --period day --from 2026-01-01 --to 2026-02-01 --family luz`.
- `naturgy invoices download all --dir ~/facturas` writes every PDF into a directory.
- `naturgy battery history --page 1 --limit 20`.

List responses include a `count`. Money is in EUR as numbers, dates are ISO strings.

## Rules

1. **Never run `naturgy invoices claim`.** It files a real claim with Naturgy. Only run it when the
   user explicitly asks to file one, and then only with `--dry-run` first.
2. **Never pass `--raw`.** It adds the untouched API objects, including personal data, to the
   output.
3. Keep it to a handful of calls per question. The API is undocumented and rate-limited: there is
   no retry, and hammering it can get the account blocked.
4. Do not write files except through `naturgy invoices download`, and only when the user asks.
5. If a command fails, report the JSON error and its exit code; do not invent data or guess.

## Recipes

Monthly electricity spend, last six months:

```bash
naturgy consumption --period month --family luz
```

What is pending, with the total:

```bash
naturgy debt
```

The latest invoice, readable:

```bash
naturgy invoices list | jq '.invoices[0] | {id: .invoice_id, date: .issue_date, amount: .total_amount, status: .status}'
```

Answer in the user's language, with units (kWh, EUR, dates), and mention the period each number
covers. If the account has several supplies, say which one each figure belongs to.
