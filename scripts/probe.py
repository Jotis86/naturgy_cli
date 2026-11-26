#!/usr/bin/env python3
"""Read-only discovery probe for the Naturgy Omega API.

Run it yourself, in your terminal:

    uv sync
    uv run python scripts/probe.py

It asks for your NIF/NIE and password locally (the password is hidden and never stored), calls the
read-only endpoints the CLI needs, and writes ONLY a structure map to
`.local-secrets/shapes.json`: key names and types, no values. That is enough to finish the output
models without sharing names, NIFs, CUPS, IBANs, amounts or invoice numbers.

It never sends a claim and never prints a token.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from naturgy import config
from naturgy.client import OmegaClient
from naturgy.errors import AuthError, NaturgyError
from naturgy.tokens import FileTokenStore

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][0-9:.+Z-]+)?$")
SLASH_DATE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
NUMERIC = re.compile(r"^-?\d+(?:[.,]\d+)?$")
BASE64ISH = re.compile(r"^[A-Za-z0-9+/=]{200,}$")
CONSUMPTION_FAMILIES = {"luz", "gas", "solar", "recarga"}


def shape(value: Any, sample: int = 3) -> Any:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        text = value.strip()
        if ISO_DATE.match(text):
            return "string(iso-date)"
        if SLASH_DATE.match(text):
            return "string(date)"
        if NUMERIC.match(text):
            return "string(number)"
        if BASE64ISH.match(text):
            return "string(base64)"
        return "string"
    if isinstance(value, list):
        return {
            "__type__": "array",
            "__count__": len(value),
            "__sample__": [shape(item, sample=sample) for item in value[:sample]],
        }
    if isinstance(value, dict):
        return {str(key): shape(item, sample=sample) for key, item in value.items()}
    return f"__{type(value).__name__}__"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _unique(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _extract(data: Any, *keys: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Naturgy API shape probe.")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / ".local-secrets" / "shapes.json",
        help="Output file (default: .local-secrets/shapes.json).",
    )
    parser.add_argument(
        "--save-tokens",
        action="store_true",
        help="Also save the session to the normal token file (so the CLI works without signing in again).",
    )
    parser.add_argument("--nif", help="Skip the NIF prompt. The password is always prompted.")
    parser.add_argument(
        "--tokens",
        type=Path,
        default=config.default_tokens_path(),
        help="Existing session file to reuse (default: the normal token file).",
    )
    parser.add_argument(
        "--limit", type=int, default=3, help="Maximum supplies/accounts to probe (default: 3)."
    )
    args = parser.parse_args(argv)

    store = FileTokenStore(args.tokens)
    client = OmegaClient()
    info: dict[str, Any] | None = None
    tokens = None

    if store.path.exists():
        try:
            saved = store.load()
            if saved is not None:
                client.close()
                client = OmegaClient(saved)
                info = client.get_client_info()
                tokens = saved
                print(f"Reusing the session at {store.path}", file=sys.stderr)
        except AuthError:
            print("The saved session is not usable; asking for credentials.", file=sys.stderr)
            client.close()
            client = OmegaClient()
            tokens = None
        except NaturgyError as error:
            print(
                json.dumps(
                    {
                        "error": {
                            "type": error.error_type,
                            "code": error.code,
                            "http_status": error.http_status,
                        }
                    }
                ),
                file=sys.stderr,
            )
            return error.exit_code

    if tokens is None:
        nif = (args.nif or input("NIF/NIE: ")).strip()
        password = getpass.getpass("Contraseña (no se guarda): ")
        if not nif or not password:
            print("NIF and password are required.", file=sys.stderr)
            return 2
        print("Signing in...", file=sys.stderr)
        try:
            tokens = client.login(nif, password)
            info = client.get_client_info()
        except NaturgyError as error:
            print(
                json.dumps(
                    {
                        "error": {
                            "type": error.error_type,
                            "code": error.code,
                            "http_status": error.http_status,
                        }
                    }
                ),
                file=sys.stderr,
            )
            return error.exit_code
        if args.save_tokens:
            with store.lock():
                store.save(tokens)
            print(f"Session saved to {store.path}", file=sys.stderr)

    endpoints: dict[str, Any] = {}
    errors: dict[str, Any] = {}
    summary: dict[str, Any] = {}

    try:
        if info is None:
            info = client.get_client_info()

        def record(name: str, call: Callable[[], Any]) -> Any:
            print(f"  {name}", file=sys.stderr)
            try:
                data = call()
            except NaturgyError as error:
                errors[name] = {
                    "type": error.error_type,
                    "code": error.code,
                    "http_status": error.http_status,
                }
                return None
            endpoints[name] = {"shape": shape(data)}
            return data

        record("GET /clients?loginDate=false", lambda: {"client": info})
        record("GET /authorization", client.authorization)

        contracts = record("GET /contractAccountsNew", lambda: client.request("GET", "contractAccountsNew"))
        supplies = _extract(contracts, "serviceAccountList", "serviceAccountListList", "contracts", "supplies")
        summary["supplies"] = len(supplies)

        invoices_by_account: list[dict[str, Any]] = []
        first_invoices: list[dict[str, Any]] = []
        for supply_index, supply in enumerate(supplies[: max(1, args.limit)]):
            for account_index, account in enumerate(
                _list(supply.get("billingAccountList") or supply.get("billingAccount"))[
                    : max(1, args.limit)
                ]
            ):
                if not isinstance(account, dict):
                    continue
                ref_num = _text(account.get("refNum")) or _text(account.get("billingAccountId"))
                products = [item for item in _list(account.get("products")) if isinstance(item, dict)]
                numbers = _unique(product.get("contractNumber") for product in products)
                cups = _unique(
                    _dict(product.get("techData")).get("cups") for product in products
                )
                if not ref_num:
                    continue
                name = f"GET /invoices#supply{supply_index}-account{account_index}"
                data = record(
                    name,
                    lambda ref_num=ref_num, numbers=numbers, cups=cups: client.request(
                        "GET",
                        "invoices",
                        params={
                            "contractAccountId": ref_num,
                            "contractNumbers": ",".join(numbers),
                            "cups": ",".join(cups),
                        },
                    ),
                )
                if isinstance(data, dict):
                    first_invoices = _extract(data, "InvoicesPDS", "invoicesPDS", "invoices", "Invoices")[:1]
                invoices_by_account.append(data or {})

                first_product = products[0] if products else {}
                tech = _dict(first_product.get("techData"))
                family = _text(_dict(first_product.get("productType")).get("productTypeFamily"))
                if family in CONSUMPTION_FAMILIES and tech.get("cups") and first_product.get("contractNumber"):
                    end = date.today()
                    start = date(end.year - 1, 1, 1)
                    record(
                        f"GET /consumptions#supply{supply_index}-month",
                        lambda product=first_product, tech=tech, family=family, start=start, end=end: client.request(
                            "GET",
                            "consumptions",
                            params={
                                "contractAccountId": "",
                                "contractNumber": product.get("contractNumber"),
                                "period": "month",
                                "startDate": start.strftime("%d/%m/%Y"),
                                "endDate": end.strftime("%d/%m/%Y"),
                                "productType": family,
                                "cups": tech.get("cups"),
                            },
                        ),
                    )
                record(
                    f"GET /itemsDebt#supply{supply_index}-account{account_index}",
                    lambda ref_num=ref_num: client.request(
                        "GET", "itemsDebt", params={"cuentaContrato": ref_num}
                    ),
                )

        numbers = _unique(
            product.get("contractNumber")
            for supply in supplies[: max(1, args.limit)]
            for account in _list(supply.get("billingAccountList") or supply.get("billingAccount"))
            if isinstance(account, dict)
            for product in _list(account.get("products"))
            if isinstance(product, dict)
        )
        if numbers:
            record(
                "GET /readings",
                lambda: client.request(
                    "GET", "readings", params={"contractNumbers": ",".join(numbers)}
                ),
            )

        record("GET /cases", lambda: client.request("GET", "cases"))
        record("GET /intents", lambda: client.request("GET", "intents"))

        first_invoice = next((item for item in first_invoices if isinstance(item, dict)), None)
        if first_invoice and first_invoice.get("invoiceId"):
            invoice_id = first_invoice["invoiceId"]
            record(
                "GET /invoiceDetail",
                lambda: client.request(
                    "GET", "invoiceDetail", params={"invoiceNum": invoice_id}
                ),
            )
            pdf_id = _text(first_invoice.get("pdfDocumentId"))
            doc_number = (
                _text(info.get("docNumber"))
                or _text(tokens.user.get("clientCode"))
                or tokens.nif
            )
            if pdf_id and doc_number:
                record(
                    "GET /pdfDocument",
                    lambda: client.request(
                        "GET",
                        "pdfDocument",
                        params={
                            "pdfDocumentType": "invoice",
                            "docNumber": doc_number,
                            "documentId": pdf_id,
                        },
                    ),
                )
                summary["pdf_document_id_present"] = True

        summary["invoices_in_first_call"] = len(first_invoices)
        summary["endpoints_ok"] = len(endpoints)
        summary["endpoints_failed"] = len(errors)
    except NaturgyError as error:
        print(
            json.dumps(
                {
                    "error": {
                        "type": error.error_type,
                        "code": error.code,
                        "http_status": error.http_status,
                    }
                }
            ),
            file=sys.stderr,
        )
        return error.exit_code
    finally:
        client.close()

    payload = {
        "generated_at": date.today().isoformat(),
        "note": "Structure only: strings are replaced with type placeholders; no values are stored.",
        "summary": summary,
        "endpoints": endpoints,
        "errors": errors,
    }
    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(out.parent, 0o700)
    temporary = out.with_name(out.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, out)
    os.chmod(out, 0o600)

    print(
        json.dumps(
            {
                "ok": True,
                "out": str(out),
                **summary,
                "errors": sorted(errors),
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        raise SystemExit(2)
