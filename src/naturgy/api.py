"""Read operations against the Omega API, plus filing an invoice claim.

The field names of the wire payloads are inferred from the web app and are validated with
`scripts/probe.py` against a real account. Every normalizer is deliberately tolerant: a missing
field becomes null instead of crashing, and `--raw` exposes the original object when something
needs checking.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from . import config
from .client import OmegaClient
from .errors import (
    ApiChangedError,
    AuthError,
    BlockedError,
    ClaimAlreadyOpenError,
    ClaimCheckFailedError,
    OutcomeUnknownError,
    UpstreamUnavailableError,
    UsageError,
)
from .models import TokenSet
from .tokens import FileTokenStore, token_status

ACTIVE_PRODUCT_STATUSES = {"02", "03", "04", "05", "06", "07", "09", "17", "18", "22", "23"}
CONSUMPTION_FAMILIES = {"luz", "gas", "solar", "recarga"}
SOLAR_FAMILY_BY_PRODUCT_CODE = {"FV001": "1", "S0012": "2"}

CLAIM_MANAGEMENT_TYPE = "claimInvoice"
CLAIM_DEFAULT_REASON = "wrong-charge"
CLAIM_REASONS: dict[str, tuple[str, str]] = {
    "wrong-reading": ("01", "lectura incorrecta"),
    "wrong-invoice": ("02", "factura incorrecta"),
    "wrong-charge": ("03", "cargo incorrecto"),
    "others": ("05", "otros"),
    "download-invoice": ("06", "descarga de factura"),
    "sending-invoice": ("07", "envío de factura"),
    "billed-invoice": ("08", "factura ya cobrada"),
}
CLAIM_CLOSED_STATES = {"Cerrado", "Cerrada", "closed", "canceled", "cancelado", "Anulado", "KO"}
BATTERY_HISTORY_MAX_LIMIT = 100


# ---------------------------------------------------------------------- entry point


def run_with_client(
    store: FileTokenStore,
    operation: Callable[[OmegaClient, TokenSet], Any],
    *,
    language: str | None = None,
) -> Any:
    tokens = store.load()
    if tokens is None:
        raise AuthError(
            f"not signed in: no token file at {store.path}; run `naturgy auth login`"
        )
    access = token_status(tokens)["access_token"]
    if access.get("expired") is True:
        expires = access.get("expires_at")
        detail = f" at {expires}" if expires else ""
        raise AuthError(f"the session expired{detail}; run `naturgy auth login`")
    client = OmegaClient(tokens, language=language)
    try:
        try:
            changed = client.ensure_identity()
        except UsageError as exc:
            raise UpstreamUnavailableError(
                "Naturgy rejected the client identity request; try again later",
                http_status=exc.http_status,
                code=exc.code,
            ) from exc
        if changed and client.tokens is not None:
            with store.lock():
                store.save(client.tokens)
        try:
            client.get_client_info()
        except UsageError as exc:
            raise UpstreamUnavailableError(
                "Naturgy rejected the client profile request; try again later",
                http_status=exc.http_status,
                code=exc.code,
            ) from exc
        return operation(client, client.tokens or tokens)
    finally:
        client.close()


# ---------------------------------------------------------------------- account


def get_me(client: OmegaClient, tokens: TokenSet, *, raw: bool = False) -> dict[str, Any]:
    info = client.get_client_info()
    authorization = client.authorization()
    result: dict[str, Any] = {
        "account": _normalize_account(info, tokens),
        "authorization": _normalize_authorization(authorization),
    }
    if raw:
        result["source"] = info
        result["authorization_source"] = authorization
    return result


def _normalize_account(info: dict[str, Any], tokens: TokenSet) -> dict[str, Any]:
    contacts = [_normalize_contact(c) for c in _as_list(info.get("contacts"))]
    holder = _dict(info.get("holderContact"))
    if not holder:
        holder = next(
            (c for c in _as_list(info.get("contacts")) if _is_holder(c)), {}
        )
    preferred = _dict(info.get("preferredContact"))
    if not preferred:
        preferred = next(
            (c for c in _as_list(info.get("contacts")) if _bool(c.get("isPreferred"))),
            {},
        )
    email = _text(info.get("preferredEmail")) or _text(info.get("email"))
    return {
        "client_id": _text(info.get("clientId")),
        "client_code": _text(info.get("clientCode")) or _text(tokens.user.get("clientCode")),
        "doc_number": _text(info.get("docNumber")) or tokens.nif,
        "doc_type": _text(info.get("docType")),
        "name": _text(info.get("name")),
        "surname": _text(info.get("surname")),
        "surname2": _text(info.get("surname2")),
        "email": email,
        "phone": _phone(info.get("preferredPhone") or info.get("phone")),
        "segment": _text(info.get("segment")),
        "status": _text(info.get("status")) or _text(info.get("state")),
        "gdpr": info.get("gdprPreferences"),
        "contacts": contacts,
        "holder_contact": _normalize_contact(holder) if holder else None,
        "preferred_contact": _normalize_contact(preferred) if preferred else None,
    }


def _normalize_contact(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "contact_id": _text(raw.get("contactId")),
        "doc_number": _text(raw.get("docNumber")),
        "name": _text(raw.get("name")),
        "surname": _text(raw.get("surname")),
        "surname2": _text(raw.get("surname2")),
        "email": _text(raw.get("preferredEmail")) or _text(raw.get("email")),
        "phone": _phone(raw.get("preferredPhone") or raw.get("phone")),
        "is_preferred": _bool(raw.get("isPreferred")),
        "is_holder": _is_holder(raw),
        "representation_type": _text(raw.get("representationTypeId")),
        "channel": _text(raw.get("channel")),
    }


def _is_holder(raw: dict[str, Any]) -> bool:
    return _bool(raw.get("isHolder")) or _text(raw.get("representationTypeId")) in {
        "01",
        "HOLDER",
        "holder",
    }


def _normalize_authorization(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("authorization", "authorizations", "permissions"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


# ---------------------------------------------------------------------- supplies


def get_supplies(client: OmegaClient, *, raw: bool = False) -> dict[str, Any]:
    contracts = _contracts_raw(client)
    supplies = [
        _normalize_supply(item, raw_output=raw)
        for item in contracts
        if isinstance(item, dict)
    ]
    return {"supplies": supplies, "count": len(supplies)}


def _contracts_raw(client: OmegaClient) -> list[dict[str, Any]]:
    data = client.request("GET", "contractAccountsNew")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        raise ApiChangedError("Naturgy's supplies response was not a JSON object")
    for key in ("serviceAccountList", "serviceAccountListList", "contracts", "supplies", "accounts"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    raise ApiChangedError("Naturgy's supplies response did not contain a list")


def _normalize_supply(raw: dict[str, Any], *, raw_output: bool = False) -> dict[str, Any]:
    billing_accounts = _as_list(
        raw.get("billingAccountList")
        or raw.get("billingAccount")
        or raw.get("billingAccounts")
    )
    products: list[dict[str, Any]] = []
    normalized_accounts: list[dict[str, Any]] = []
    for account in billing_accounts:
        if not isinstance(account, dict):
            continue
        normalized_accounts.append(_normalize_billing_account(account))
        for product in _as_list(account.get("products")):
            if isinstance(product, dict):
                products.append(_normalize_product(product, account))
    if not billing_accounts:
        for product in _as_list(raw.get("products")):
            if isinstance(product, dict):
                products.append(_normalize_product(product, {}))

    address = raw.get("supplyAddress") or raw.get("address")
    return {
        "service_account_id": _text(raw.get("serviceAccountId")),
        "client_id": _text(raw.get("clientId")),
        "address": _address_text(address),
        "address_details": address if isinstance(address, dict) else None,
        "billing_accounts": normalized_accounts,
        "products": products,
        "ref_nums": [_text(a.get("refNum")) for a in billing_accounts if a.get("refNum")],
        "pay_day": _first(
            *[_text(a.get("payDay")) for a in billing_accounts if a.get("payDay")],
            default=None,
        ),
        **({"source": raw} if raw_output else {}),
    }


def _normalize_billing_account(raw: dict[str, Any]) -> dict[str, Any]:
    iban = _text(raw.get("iban")) or _text(_dict(raw.get("bankAccount")).get("iban"))
    return {
        "billing_account_id": _text(raw.get("billingAccountId")),
        "ref_num": _text(raw.get("refNum")),
        "is_direct_payment": _bool(raw.get("isDirectPayment")),
        "iban_masked": _mask_iban(iban),
        "pay_day": _text(raw.get("payDay")),
        "bill_delivery_method": _text(raw.get("billDeliveryMethod")),
    }


def _normalize_product(raw: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    product_type = _dict(raw.get("productType"))
    tech = _dict(raw.get("techData"))
    family = _text(product_type.get("productTypeFamily")) or _text(tech.get("productTypeFamily"))
    status = (
        _text(product_type.get("productStatus"))
        or _text(raw.get("contractStatus"))
        or _text(raw.get("status"))
    )
    active_flag = raw.get("isActive")
    if active_flag is not None:
        is_active = _bool(active_flag)
    else:
        is_active = status in ACTIVE_PRODUCT_STATUSES if status else None
    powers = {
        key: _num(tech.get(key))
        for key in ("powerPlain", "powerValley", "powerP3", "powerP4", "powerP5", "powerP6")
        if tech.get(key) is not None
    }
    for key in ("p1", "p2", "p3", "p4", "p5", "p6"):
        if tech.get(key) is not None:
            powers[key] = _num(tech.get(key))
    return {
        "contract_id": _text(raw.get("contractId")),
        "contract_number": _text(raw.get("contractNumber")),
        "asset_id": _text(product_type.get("Assetid")) or _text(product_type.get("assetId")),
        "product_type_id": _text(product_type.get("productTypeId")),
        "product_family": family,
        "product_name": _text(product_type.get("productTypeName")),
        "product_code": _text(product_type.get("productCode")),
        "sva_family": _text(product_type.get("svaFamily")),
        "status": status,
        "is_active": is_active,
        "rate": _text(tech.get("rate")),
        "cups": _text(tech.get("cups")),
        "powers": powers or None,
        "start_date": _text(raw.get("startDate")),
        "end_date": _text(raw.get("endDate")),
        "drop_out_date": _text(raw.get("dropOutDate")),
        "billing_account_id": _text(account.get("billingAccountId")),
        "ref_num": _text(account.get("refNum")),
    }


def select_supplies(
    supplies: list[dict[str, Any]], selector: str | None
) -> list[dict[str, Any]]:
    if not selector:
        return supplies
    needle = selector.strip().casefold()
    matches: list[dict[str, Any]] = []
    for supply in supplies:
        haystack = [
            supply.get("service_account_id"),
            supply.get("client_id"),
            supply.get("address"),
            *[a.get("ref_num") for a in supply.get("billing_accounts", [])],
            *[p.get("contract_number") for p in supply.get("products", [])],
            *[p.get("cups") for p in supply.get("products", [])],
        ]
        values = [str(value).casefold() for value in haystack if value]
        if any(
            needle == value or value.endswith(needle) or value.startswith(needle)
            for value in values
        ):
            matches.append(supply)
    if not matches:
        raise UsageError(f"no supply matches {selector!r}")
    return matches


# ---------------------------------------------------------------------- invoices


def get_invoices(
    client: OmegaClient,
    supplies: list[dict[str, Any]] | None = None,
    *,
    raw: bool = False,
) -> dict[str, Any]:
    if supplies is None:
        supplies = get_supplies(client)["supplies"]
    invoices_by_id: dict[str, dict[str, Any]] = {}
    contingency = False
    for supply in supplies:
        families = {
            p.get("product_family") for p in supply.get("products", []) if p.get("product_family")
        }
        for account in supply.get("billing_accounts", []):
            products = [
                p
                for p in supply.get("products", [])
                if p.get("billing_account_id") == account.get("billing_account_id")
                or p.get("ref_num") == account.get("ref_num")
            ]
            if not products:
                products = list(supply.get("products", []))
            numbers = _unique(p.get("contract_number") for p in products)
            cups = _unique(p.get("cups") for p in products)
            ref_num = account.get("ref_num") or account.get("billing_account_id")
            if not ref_num:
                continue
            data = client.request(
                "GET",
                "invoices",
                params={
                    "contractAccountId": ref_num,
                    "contractNumbers": ",".join(numbers),
                    "cups": ",".join(cups),
                },
            )
            if isinstance(data, dict) and _bool(data.get("isContingencyMode")):
                contingency = True
            for item in _extract_invoices(data):
                if not isinstance(item, dict):
                    continue
                invoice = _normalize_invoice(
                    item,
                    service_account_id=supply.get("service_account_id"),
                    billing_account_id=account.get("billing_account_id"),
                    ref_num=ref_num,
                    raw_output=raw,
                )
                invoice_id = invoice.get("invoice_id")
                if not invoice_id:
                    continue
                if not _keep_invoice(invoice, families):
                    continue
                previous = invoices_by_id.get(invoice_id)
                if previous is None or (
                    invoice.get("pending_amount") or 0
                ) > (previous.get("pending_amount") or 0):
                    invoices_by_id[invoice_id] = invoice
    invoices = sorted(
        invoices_by_id.values(),
        key=lambda item: _date_key(item.get("billing_date") or item.get("issued_date")),
        reverse=True,
    )
    return {
        "invoices": invoices,
        "count": len(invoices),
        "is_contingency_mode": contingency,
    }


def _extract_invoices(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("InvoicesPDS", "invoicesPDS", "invoices", "Invoices", "invoiceList"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _keep_invoice(invoice: dict[str, Any], families: set[str | None]) -> bool:
    invoice_type = invoice.get("type")
    relations = invoice.get("relations") or []
    relation_families = {
        relation.get("product_family_id")
        for relation in relations
        if relation.get("product_family_id")
    }
    pending = invoice.get("pending_amount") or 0
    if invoice_type == "NC":
        return not relation_families or bool(relation_families & families)
    if invoice_type == "NI":
        return not relation_families or bool(relation_families & families) or pending > 0
    return True


def _normalize_invoice(
    raw: dict[str, Any],
    *,
    service_account_id: str | None,
    billing_account_id: str | None,
    ref_num: str | None,
    raw_output: bool = False,
) -> dict[str, Any]:
    relations = [
        _normalize_relation(item) for item in _as_list(raw.get("productInvoiceRelationList"))
    ]
    relation = relations[0] if relations else {}
    pdf_id = _text(raw.get("pdfDocumentId")) or _text(raw.get("pdfDocumentID"))
    amount = _num(raw.get("amount"))
    pending = _num(raw.get("pendingAmount"))
    result: dict[str, Any] = {
        "invoice_id": _text(raw.get("invoiceId")),
        "type": _text(raw.get("type")),
        "status": _text(raw.get("status")),
        "issued_date": _text(raw.get("issuedDate")),
        "billing_date": _text(raw.get("billingDate")),
        "period_start": _text(raw.get("periodFrom"))
        or _text(raw.get("periodStart"))
        or _text(raw.get("startDate")),
        "period_end": _text(raw.get("periodEnd")) or _text(raw.get("endDate")),
        "amount": amount,
        "pending_amount": pending,
        "currency": _text(raw.get("currency")) or "EUR",
        "is_pending": (pending or 0) > 0,
        "pdf_document_id": pdf_id,
        "has_pdf": bool(pdf_id),
        "interactive_invoice": _text(raw.get("interactiveInvoice")),
        "document_type": _text(raw.get("documentType")),
        "contract_number": relation.get("contract_number")
        or _text(raw.get("contractNumber")),
        "product_family": relation.get("product_family_id"),
        "service_account_id": service_account_id,
        "billing_account_id": billing_account_id,
        "ref_num": ref_num,
        "relations": relations,
    }
    if raw_output:
        result["source"] = raw
    return result


def _normalize_relation(raw: Any) -> dict[str, Any]:
    item = _dict(raw)
    return {
        "contract_id": _text(item.get("contractId")),
        "contract_number": _text(item.get("contractNumber")),
        "product_family_id": _text(item.get("productFamilyId")),
        "billing_account_id": _text(item.get("billingAccountId")),
        "invoice_atr_id": _text(item.get("invoice")) or _text(item.get("invoiceAtrId")),
        "is_solar_invoice": _bool(item.get("isSolarInvoice")),
    }


def find_invoice(invoices: list[dict[str, Any]], target: str) -> dict[str, Any]:
    if target == "latest":
        with_pdf = [item for item in invoices if item.get("pdf_document_id")]
        if not with_pdf:
            raise UsageError("there are no invoices with a PDF yet")
        return with_pdf[0]
    for invoice in invoices:
        if invoice.get("invoice_id") == target:
            return invoice
    raise UsageError(f"unknown invoice id {target!r}; use `naturgy invoices list`")


def download_invoices(
    client: OmegaClient,
    tokens: TokenSet,
    target: str,
    directory: Path,
    supplies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    invoices = get_invoices(client, supplies)["invoices"]
    if target == "all":
        selected = [item for item in invoices if item.get("pdf_document_id")]
        if not selected:
            raise UsageError("there are no invoices with a PDF yet")
    else:
        selected = [find_invoice(invoices, target)]

    directory = directory.expanduser()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UsageError(f"cannot create the output directory at {directory}") from exc
    if not directory.is_dir():
        raise UsageError(f"{directory} is not a directory")

    saved: list[dict[str, Any]] = []
    try:
        for invoice in selected:
            name = invoice_filename(invoice)
            path = directory / name
            if path.exists():
                saved.append(
                    {"invoice_id": invoice["invoice_id"], "file": str(path), "status": "skipped"}
                )
                continue
            payload = invoice_pdf(client, tokens, invoice)
            _write_private(path, payload)
            saved.append(
                {
                    "invoice_id": invoice["invoice_id"],
                    "file": str(path),
                    "bytes": len(payload),
                    "status": "downloaded",
                }
            )
    except (UpstreamUnavailableError, ApiChangedError, BlockedError) as exc:
        exc.extra.setdefault("saved", saved)
        raise
    return {"directory": str(directory), "invoices": saved, "count": len(saved)}


def invoice_pdf(client: OmegaClient, tokens: TokenSet, invoice: dict[str, Any]) -> bytes:
    document_id = invoice.get("pdf_document_id")
    if not document_id:
        raise UsageError(f"invoice {invoice.get('invoice_id')} has no PDF yet")
    info = client.get_client_info()
    doc_number = (
        _text(info.get("docNumber"))
        or _text(tokens.user.get("clientCode"))
        or tokens.nif
    )
    if not doc_number:
        raise UsageError("the client document number is missing from the session")
    data = client.request(
        "GET",
        "pdfDocument",
        params={
            "pdfDocumentType": "invoice",
            "docNumber": doc_number,
            "documentId": document_id,
        },
    )
    value: Any = None
    if isinstance(data, dict):
        for key in ("pdfDocument", "pdf", "document", "file"):
            if data.get(key):
                value = data[key]
                break
    elif isinstance(data, str):
        value = data
    if not isinstance(value, str) or not value.strip():
        raise ApiChangedError("Naturgy's PDF response did not contain the document")
    payload = _decode_pdf(value)
    if not payload.lstrip().startswith(b"%PDF"):
        raise ApiChangedError("Naturgy returned a document that is not a PDF")
    return payload


def _decode_pdf(value: str) -> bytes:
    text = value.strip()
    if text.startswith("data:"):
        text = text.split(",", 1)[1] if "," in text else ""
    if text.startswith(("http://", "https://")):
        return _fetch_pdf_url(text)
    try:
        return base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ApiChangedError("Naturgy's PDF was not valid base64") from exc


def _fetch_pdf_url(url: str) -> bytes:
    parsed = httpx.URL(url)
    allowed = ("naturgy.es", "naturgy.com", "cloudhub.io", "amazonaws.com")
    host = parsed.host or ""
    if parsed.scheme != "https" or not any(host == d or host.endswith("." + d) for d in allowed):
        raise ApiChangedError("Naturgy returned an unexpected PDF URL")
    try:
        response = httpx.get(url, timeout=config.TIMEOUT_SECONDS, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpstreamUnavailableError("could not download the invoice PDF") from exc
    return response.content


def invoice_filename(invoice: dict[str, Any]) -> str:
    invoice_id = str(invoice.get("invoice_id") or "")
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", invoice_id).strip("._")[:80]
    if not safe_id:
        raise UsageError("the invoice id cannot be used as a file name")
    raw_date = _date_key(invoice.get("billing_date") or invoice.get("issued_date"))
    month = raw_date[:7]
    if len(month) == 7 and month[4] == "-":
        return f"factura_{month}_{safe_id}.pdf"
    return f"factura_{safe_id}.pdf"


def _date_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


def _write_private(path: Path, payload: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return
        except OSError:
            if path.exists():
                return
            os.replace(temporary, path)
            os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def get_invoice_detail(client: OmegaClient, invoice_id: str) -> dict[str, Any]:
    data = client.request("GET", "invoiceDetail", params={"invoiceNum": invoice_id})
    if not isinstance(data, dict):
        raise ApiChangedError("Naturgy's invoice-detail response was not a JSON object")
    detail = data.get("detalleFactura") or data.get("invoice") or data
    return {"invoice_id": invoice_id, "detail": detail}


# ---------------------------------------------------------------------- consumption


def get_consumption(
    client: OmegaClient,
    supplies: list[dict[str, Any]],
    *,
    period: str = "month",
    start: date | None = None,
    end: date | None = None,
    families: set[str] | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    if period not in ("day", "month"):
        raise UsageError("--period must be 'day' or 'month'")
    end = end or date.today()
    if start is None:
        if period == "month":
            start = _months_before(end, 12)
        else:
            start = end - timedelta(days=60)
    if start > end:
        raise UsageError("--from must be before --to")

    selected_families = families or CONSUMPTION_FAMILIES
    products: list[dict[str, Any]] = []
    for supply in supplies:
        for product in supply.get("products", []):
            family = product.get("product_family")
            if family not in selected_families or family not in CONSUMPTION_FAMILIES:
                continue
            if not product.get("contract_number") or not product.get("cups"):
                continue
            data = client.request(
                "GET",
                "consumptions",
                params={
                    "contractAccountId": "",
                    "contractNumber": product["contract_number"],
                    "period": period,
                    "startDate": start.strftime("%d/%m/%Y"),
                    "endDate": end.strftime("%d/%m/%Y"),
                    "productType": family,
                    "cups": product["cups"],
                },
            )
            entry: dict[str, Any] = {
                "service_account_id": supply.get("service_account_id"),
                "contract_number": product.get("contract_number"),
                "product_family": family,
                "cups": product.get("cups"),
                "series": _consumption_series(data),
            }
            if raw:
                entry["source"] = data
            products.append(entry)

    return {
        "period": period,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "products": products,
        "count": len(products),
    }


def _consumption_series(data: Any) -> list[dict[str, Any]]:
    values: Any = None
    if isinstance(data, dict):
        for key in ("consumptionValues", "consumptionValueList", "values"):
            if isinstance(data.get(key), list):
                values = data[key]
                break
        if values is None and isinstance(data.get("consumptions"), dict):
            values = _first(data["consumptions"], "consumptionValues", "values")
    elif isinstance(data, list):
        values = data
    series: list[dict[str, Any]] = []
    for item in _flatten(values):
        if not isinstance(item, dict):
            continue
        series.append(
            {
                "date": _text(item.get("date")) or _text(item.get("readingDate")),
                "total": _num(item.get("valueTotal")),
                "plain": _num(item.get("valuePlain")),
                "point": _num(item.get("valuePoint")),
                "valley": _num(item.get("valueValley")),
                "p1": _num(item.get("P1") or item.get("p1")),
                "p2": _num(item.get("P2") or item.get("p2")),
                "p3": _num(item.get("P3") or item.get("p3")),
                "p4": _num(item.get("P4") or item.get("p4")),
                "p5": _num(item.get("P5") or item.get("p5")),
                "p6": _num(item.get("P6") or item.get("p6")),
                "previous_year_total": _num(item.get("previousYearValue")),
            }
        )
    series.sort(key=lambda item: item.get("date") or "")
    return series


# ---------------------------------------------------------------------- readings


def get_readings(
    client: OmegaClient,
    supplies: list[dict[str, Any]],
    *,
    raw: bool = False,
) -> dict[str, Any]:
    numbers = _unique(
        product.get("contract_number")
        for supply in supplies
        for product in supply.get("products", [])
    )
    if not numbers:
        raise UsageError("there are no supplies to read")
    data = client.request(
        "GET", "readings", params={"contractNumbers": ",".join(numbers)}
    )
    raw_items = _extract_readings(data)
    readings = [
        _normalize_reading(item, raw_output=raw)
        for item in raw_items
        if isinstance(item, dict)
    ]
    return {"readings": readings, "count": len(readings)}


def _extract_readings(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("readingList", "readings", "lecturas"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _normalize_reading(raw: dict[str, Any], *, raw_output: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "contract_number": _text(raw.get("contractNumber")),
        "product_family": _text(raw.get("productTypeFamily")),
        "invoice_id": _text(raw.get("invoiceId")),
        "last_active_date": _text(raw.get("lastActiveDate")),
        "cups": _text(_dict(raw.get("agentInfo")).get("cups")),
        "items": _reading_items(raw),
    }
    if raw_output:
        result["source"] = raw
    return result


def _reading_items(raw: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    # Older shape: readingItem -> readingDetailList -> values.
    for reading in _as_list(raw.get("readingItem")):
        reading = _dict(reading)
        reading_date = _text(reading.get("readingItemDate"))
        reading_type = _text(reading.get("typeItem"))
        counter = reading.get("counterItem")
        for detail in _as_list(reading.get("readingDetailList")):
            _append_reading_detail(items, detail, reading_date, reading_type, counter)
    # Current shape: readingDetailList at the top level.
    for detail in _flatten(raw.get("readingDetailList")):
        _append_reading_detail(items, detail, _text(raw.get("readingDate")), None, None)
    return items


def _append_reading_detail(
    items: list[dict[str, Any]],
    detail: Any,
    fallback_date: str | None,
    reading_type: str | None,
    counter: Any,
) -> None:
    detail = _dict(detail)
    if not detail:
        return
    magnitude = _text(detail.get("magnitude"))
    values = _as_list(detail.get("values"))
    if values:
        for value in values:
            value = _dict(value)
            items.append(
                {
                    "date": _text(value.get("readingDate")) or fallback_date,
                    "type": reading_type or _text(value.get("type")),
                    "counter": counter or value.get("counter"),
                    "magnitude": magnitude,
                    "value": _num(
                        value.get("value")
                        or value.get("readingValue")
                        or value.get("counter")
                    ),
                }
            )
        return
    items.append(
        {
            "date": _text(detail.get("readingDate")) or fallback_date,
            "type": reading_type or _text(detail.get("type")),
            "counter": counter or detail.get("counter"),
            "magnitude": magnitude,
            "value": _num(
                detail.get("value")
                or detail.get("readingValue")
                or detail.get("counter")
            ),
        }
    )


# ---------------------------------------------------------------------- debt


def get_debt(
    client: OmegaClient,
    supplies: list[dict[str, Any]],
    *,
    raw: bool = False,
) -> dict[str, Any]:
    invoices = get_invoices(client, supplies)["invoices"]
    accounts: list[dict[str, Any]] = []
    total = 0.0
    for supply in supplies:
        for account in supply.get("billing_accounts", []):
            ref_num = account.get("ref_num")
            if not ref_num:
                continue
            account_invoices = [
                item
                for item in invoices
                if item.get("service_account_id") == supply.get("service_account_id")
                and (
                    item.get("billing_account_id") == account.get("billing_account_id")
                    or item.get("ref_num") == ref_num
                )
            ]
            invoices_pending = sum(
                (item.get("pending_amount") or 0)
                for item in account_invoices
                if item.get("pending_amount")
            )
            data = client.request(
                "GET", "itemsDebt", params={"cuentaContrato": ref_num}
            )
            items = _extract_debt_items(data)
            other_pending = sum((item.get("pending_amount") or 0) for item in items)
            account_total = invoices_pending + other_pending
            total += account_total
            entry: dict[str, Any] = {
                "service_account_id": supply.get("service_account_id"),
                "billing_account_id": account.get("billing_account_id"),
                "ref_num": ref_num,
                "invoices_pending": round(invoices_pending, 2),
                "other_debt_pending": round(other_pending, 2),
                "total_pending": round(account_total, 2),
                "items": items,
            }
            if raw:
                entry["source"] = data
            accounts.append(entry)
    return {"accounts": accounts, "total_pending": round(total, 2)}


def _extract_debt_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        value = data.get("itemsDebt")
        if isinstance(value, dict):
            value = value.get("itemsDebt")
        raw_items = value if isinstance(value, list) else []
    else:
        raw_items = []
    items: list[dict[str, Any]] = []
    for item in raw_items:
        item = _dict(item)
        if not item:
            continue
        items.append(
            {
                "receipt": _text(item.get("opbel")),
                "pending_amount": _num(item.get("pendingAmount")),
                "issued_date": _text(item.get("issuedDate")),
                "status": _text(item.get("status")),
                "number_due": [
                    {
                        "number_due": _text(due.get("opupk")),
                        "compensation": _text(due.get("opupz")),
                    }
                    for due in _as_list(item.get("items"))
                    if isinstance(due, dict)
                ],
            }
        )
    return items


# ---------------------------------------------------------------------- cases


def get_cases(
    client: OmegaClient, *, raw: bool = False
) -> dict[str, Any]:
    data = client.request("GET", "cases")
    raw_items = _extract_cases(data)
    cases = [
        _normalize_case(item, raw_output=raw)
        for item in raw_items
        if isinstance(item, dict)
    ]
    return {"cases": cases, "count": len(cases)}


def _extract_cases(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("cases", "caseList", "gestiones"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _normalize_case(raw: dict[str, Any], *, raw_output: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "case_id": _text(raw.get("caseId")),
        "case_code": _text(raw.get("caseCode")),
        "type": _text(raw.get("typeId")) or _text(raw.get("managementType")),
        "type_label": _text(raw.get("type")),
        "state": _text(raw.get("state")) or _text(raw.get("status")),
        "step": _text(raw.get("step")),
        "sub_step": _text(raw.get("subStep")),
        "channel": _text(raw.get("channel")),
        "contract_id": _text(raw.get("contractId")),
        "service_account_id": _text(raw.get("serviceAccountId")),
        "billing_account_id": _text(raw.get("billingAccountId")),
        "invoice_id": _text(raw.get("invoiceId")),
        "start_date": _text(raw.get("startDate")),
        "end_date": _text(raw.get("endDate")),
        "created_at": _text(raw.get("createdDate"))
        or _text(raw.get("createdAt"))
        or _text(raw.get("fechaAlta")),
        "updated_at": _text(raw.get("updatedDate")) or _text(raw.get("updatedAt")),
        "closed_at": _text(raw.get("closedDate")) or _text(raw.get("closedAt")),
        "note": _text(raw.get("comments")) or _text(raw.get("note")),
        "has_files": _bool(raw.get("hasFiles")),
        "is_sva": _bool(raw.get("isSva")),
        "documents": {
            "id_documentum": _text(raw.get("idDocumentum")),
            "id_s3": _text(raw.get("idS3")),
            "path_s3": _text(raw.get("pathS3")),
        },
    }
    if raw_output:
        result["source"] = raw
    return result


# ---------------------------------------------------------------------- battery


def validate_battery_history(page: int, limit: int) -> None:
    if page < 1:
        raise UsageError("--page must be 1 or greater")
    if limit < 1 or limit > BATTERY_HISTORY_MAX_LIMIT:
        raise UsageError(f"--limit must be between 1 and {BATTERY_HISTORY_MAX_LIMIT}")


def validate_battery_year(year: int | None) -> int:
    selected = date.today().year if year is None else year
    if selected < 1900 or selected > 2100:
        raise UsageError("--year must be a four-digit year")
    return selected


def get_battery_status(
    client: OmegaClient, *, raw: bool = False
) -> dict[str, Any]:
    data = client.request("GET", "balanceBattery")
    if not isinstance(data, dict):
        raise ApiChangedError("Naturgy's battery response was not a JSON object")
    known = {"nif", "name", "current_total", "is_automatic"}
    breakdown: list[dict[str, Any]] = []
    for code, value in data.items():
        if code in known or not isinstance(value, dict) or "total" not in value:
            continue
        breakdown.append(
            {
                "code": str(code),
                "total": _num(value.get("total")),
                "date_enabled": _text(value.get("date_enabled")),
                "date_disabled": _text(value.get("date_disabled")),
            }
        )
    date_enabled = _first(*(item["date_enabled"] for item in breakdown), default=None)
    date_disabled = _first(*(item["date_disabled"] for item in breakdown), default=None)
    balance = _num(data.get("current_total"))
    if balance is None:
        totals = [item["total"] for item in breakdown if item["total"] is not None]
        balance = round(sum(totals), 2) if totals else None
    result: dict[str, Any] = {
        "balance": balance,
        "currency": "EUR",
        "is_active": _battery_active(date_enabled, date_disabled),
        "is_automatic": _bool(data.get("is_automatic")),
        "date_enabled": date_enabled,
        "date_disabled": date_disabled,
        "breakdown": breakdown,
    }
    if raw:
        result["source"] = data
    return result


def get_battery_history(
    client: OmegaClient,
    *,
    page: int = 1,
    limit: int = 20,
    supply: str | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    validate_battery_history(page, limit)
    # /historyBattery paginates with request headers, not query parameters.
    data = client.request(
        "GET",
        "historyBattery",
        extra_headers={"page": str(page), "limit": str(limit)},
    )
    if not isinstance(data, dict):
        raise ApiChangedError("Naturgy's battery history response was not a JSON object")
    movements = [
        _normalize_battery_movement(item)
        for item in _as_list(data.get("data"))
        if isinstance(item, dict)
    ]
    if supply:
        movements = _filter_battery_items(
            movements, supply, ("contract", "supply", "address"), required=False
        )
    result: dict[str, Any] = {
        "movements": movements,
        "count": len(movements),
        "page": _int(data.get("currentPage")) or page,
        "limit": limit,
        "total_pages": _int(data.get("totalPages")) or 0,
        "total_items": _int(data.get("totalItems")) or 0,
    }
    if raw:
        result["source"] = data
    return result


def get_battery_evolution(
    client: OmegaClient, year: int | None = None, *, raw: bool = False
) -> dict[str, Any]:
    selected = validate_battery_year(year)
    data = client.request(
        "GET", "evolutionBalanceBattery", params={"year": selected}
    )
    if not isinstance(data, dict):
        raise ApiChangedError("Naturgy's battery evolution response was not a JSON object")
    months: list[dict[str, Any]] = []
    for key, value in data.items():
        text = str(key)
        if not text.isdigit():
            continue
        entries = [
            {"origin": _text(item.get("origin")), "total": _num(item.get("total"))}
            for item in _as_list(value)
            if isinstance(item, dict)
        ]
        months.append({"month": f"{int(text):02d}", "entries": entries})
    months.sort(key=lambda item: item["month"])
    result: dict[str, Any] = {"year": selected, "months": months, "count": len(months)}
    if raw:
        result["source"] = data
    return result


def get_battery_assignments(
    client: OmegaClient, *, supply: str | None = None, raw: bool = False
) -> dict[str, Any]:
    data = client.request("GET", "assignmentBattery")
    if not isinstance(data, dict):
        raise ApiChangedError("Naturgy's battery assignment response was not a JSON object")
    reserves = [
        _normalize_battery_assignment(item)
        for item in _as_list(data.get("reserves"))
        if isinstance(item, dict)
    ]
    if supply:
        reserves = _filter_battery_items(
            reserves, supply, ("contract", "supply", "address"), required=True
        )
    result: dict[str, Any] = {
        "reserves": reserves,
        "count": len(reserves),
        "total": _num(data.get("total")),
    }
    if raw:
        result["source"] = data
    return result


def _battery_active(
    date_enabled: str | None, date_disabled: str | None
) -> bool | None:
    if not date_enabled:
        return None
    if not date_disabled:
        return True
    return _date_key(date_disabled) > date.today().isoformat()


def _normalize_battery_movement(raw: dict[str, Any]) -> dict[str, Any]:
    concept = _text(raw.get("concept"))
    return {
        "id": _text(raw.get("id")),
        "concept": concept,
        "kind": _battery_kind(concept),
        "amount": _num(raw.get("quantity")),
        "invoice_id": _text(raw.get("factnum")),
        "contract": _text(raw.get("vkont")),
        "supply": _text(raw.get("supply")),
        "ally_id": _text(raw.get("allyId")),
        "is_self_consumption": _bool(raw.get("is_self_consumption")),
        "created_at": _text(raw.get("created_at")),
        "accounting_date": _text(raw.get("accounting_date")),
        "address": _text(raw.get("address")),
    }


def _battery_kind(concept: str | None) -> str | None:
    text = (concept or "").casefold()
    if "caduc" in text:
        return "expired"
    if "rectific" in text:
        return "adjustment"
    if "gasto" in text or "descarg" in text or "consum" in text:
        return "discharge"
    if "nuevo saldo" in text or "carg" in text or "abono" in text:
        return "charge"
    return None


def _normalize_battery_assignment(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _text(raw.get("id")),
        "contract": _text(raw.get("vkont")),
        "supply": _text(raw.get("supply")),
        "assigned": _num(raw.get("quantity")),
        "enabled": _bool(raw.get("enabled")),
        "supply_active": _bool(raw.get("supply_active")),
        "created_at": _text(raw.get("created_at")),
        "updated_at": _text(raw.get("updated_at")),
        "address": _text(raw.get("address")),
    }


def _filter_battery_items(
    items: list[dict[str, Any]],
    selector: str,
    keys: tuple[str, ...],
    *,
    required: bool,
) -> list[dict[str, Any]]:
    needle = selector.strip().casefold()
    matches = [
        item
        for item in items
        if any(needle in str(item.get(key) or "").casefold() for key in keys)
    ]
    if required and not matches:
        raise UsageError(f"no battery item matches {selector!r}")
    return matches


# ---------------------------------------------------------------------- claims


@dataclass
class ClaimDraft:
    payload: dict[str, Any]
    invoice_id: str
    invoice_month: str | None
    invoice_amount: float | None
    reason_code: str
    reason_label: str
    message: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    contract_id: str | None = None
    service_account_id: str | None = None

    def preview(self) -> str:
        amount = "importe desconocido" if self.invoice_amount is None else f"{self.invoice_amount:.2f} EUR"
        month = f"{self.invoice_month}, " if self.invoice_month else ""
        files = (
            "\nAdjuntos: " + ", ".join(a["file_name"] for a in self.attachments)
            if self.attachments
            else ""
        )
        return (
            f"Reclamación (revisión de factura) para Naturgy sobre la factura "
            f"{self.invoice_id} ({month}{amount}).\n"
            f"Motivo: {self.reason_label}.{files}\n"
            "Texto que se enviará:\n"
            "----------------------------------------\n"
            f"{self.message}\n"
            "----------------------------------------"
        )


def resolve_reason(value: str | None) -> tuple[str, str]:
    if not value:
        return CLAIM_REASONS[CLAIM_DEFAULT_REASON]
    key = value.strip().casefold()
    if key in CLAIM_REASONS:
        return CLAIM_REASONS[key]
    for code, label in CLAIM_REASONS.values():
        if code == key:
            return code, label
    choices = ", ".join(sorted(CLAIM_REASONS))
    raise UsageError(f"unknown claim reason {value!r}; choose one of: {choices}")


def claim_text(message: str) -> str:
    text = message.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise UsageError("the claim message cannot be empty")
    if len(text) > config.CLAIM_MAX_LENGTH:
        raise UsageError(
            f"the claim message can be at most {config.CLAIM_MAX_LENGTH} characters"
        )
    for char in text:
        if ord(char) < 32 and char not in ("\n", "\t"):
            raise UsageError("the claim message contains a control character")
    return text


def prepare_claim(
    client: OmegaClient,
    tokens: TokenSet,
    target: str,
    message: str,
    *,
    reason: str | None = None,
    attachments: list[Path] | None = None,
    supplies: list[dict[str, Any]] | None = None,
) -> ClaimDraft:
    text = claim_text(message)
    reason_code, reason_label = resolve_reason(reason)
    raw_contracts = _contracts_raw(client)
    if supplies is None:
        supplies = [
            _normalize_supply(item, raw_output=False)
            for item in raw_contracts
            if isinstance(item, dict)
        ]
    invoices = get_invoices(client, supplies)["invoices"]
    invoice = find_invoice(invoices, target)
    if reason_code == "08":
        if len(supplies) != 1:
            raise UsageError(
                "the 'billed-invoice' reason is contract-level; pass --supply to pick one"
            )
        selected_id = supplies[0].get("service_account_id")
        raw_supply = next(
            (
                item
                for item in raw_contracts
                if _text(item.get("serviceAccountId")) == selected_id
            ),
            raw_contracts[0] if raw_contracts else {},
        )
        raw_product = _first_product(raw_supply)
        if raw_product is None:
            raise UsageError("the selected supply has no products")
        raw_account = _account_for_product(raw_supply, raw_product)
        relation = {}
    else:
        raw_supply, raw_account, raw_product, relation = _find_raw_context(
            raw_contracts, invoice
        )
        if raw_supply is None or raw_product is None:
            raise UsageError(
                "could not match the invoice to a product; run `naturgy supplies` and pass --supply"
            )

    product_type = _dict(raw_product.get("productType"))
    product_code = _text(product_type.get("productCode"))
    contract_id = _text(raw_product.get("contractId")) or relation.get("contract_id")
    asset_id = _text(product_type.get("Assetid")) or _text(product_type.get("assetId"))
    billing_account_id = (
        _text(raw_product.get("billingAccountId"))
        or _text(raw_account.get("billingAccountId"))
        or invoice.get("billing_account_id")
    )
    invoice_atr_id = relation.get("invoice_atr_id") or "999999999"
    service_account_id = _text(raw_supply.get("serviceAccountId"))
    solar_family = SOLAR_FAMILY_BY_PRODUCT_CODE.get(product_code or "", "0")
    invoice_id = "" if reason_code == "08" else str(invoice.get("invoice_id") or "")
    issued_date = "" if reason_code == "08" else str(invoice.get("issued_date") or "")

    info = client.get_client_info()
    contact_id = _preferred_contact_id(info)
    validate_attachments(attachments or [])
    files = [_attachment_payload(path) for path in attachments or []]

    payload: dict[str, Any] = {
        "managementType": CLAIM_MANAGEMENT_TYPE,
        "comments": text,
        "reasonSelected": reason_code,
        "serviceAccountId": service_account_id,
        "contractId": contract_id,
        "billingAccountId": billing_account_id,
        "invoiceAtrId": invoice_atr_id,
        "invoiceId": invoice_id,
        "issuedDate": issued_date,
        "isSolarFamily": solar_family,
        "product": {
            "family": _text(product_type.get("productTypeFamily")),
            "contractId": contract_id,
            "assetId": asset_id,
            "invoiceAtrId": invoice_atr_id,
            "productName": _text(product_type.get("productTypeName")),
            "billingAccountId": billing_account_id,
            "productTypeFamily": _text(product_type.get("productTypeFamily")),
            "svaFamily": _text(product_type.get("svaFamily")),
        },
        "files": [
            {key: value for key, value in item.items() if key != "_raw"} for item in files
        ],
    }
    if contact_id:
        payload["preferredContactId"] = contact_id

    month = str(invoice.get("billing_date") or invoice.get("issued_date") or "")[:7] or None
    return ClaimDraft(
        payload=payload,
        invoice_id=str(invoice.get("invoice_id") or ""),
        invoice_month=month,
        invoice_amount=invoice.get("amount"),
        reason_code=reason_code,
        reason_label=reason_label,
        message=text,
        attachments=[
            {"file_name": f["fileNameWithExtension"], "bytes": len(f["_raw"])} for f in files
        ],
        contract_id=contract_id,
        service_account_id=service_account_id,
    )


def claim_body(draft: ClaimDraft) -> dict[str, Any]:
    """The body for --dry-run, with attachment bytes redacted."""
    body = {key: value for key, value in draft.payload.items()}
    body["files"] = [
        {
            "file": f"<base64 {item['bytes']} bytes>",
            "fileNameWithExtension": item["file_name"],
        }
        for item in draft.attachments
    ]
    return body


def send_claim(
    client: OmegaClient, draft: ClaimDraft, *, force: bool = False
) -> dict[str, Any]:
    check = _check_open_claim(client, draft, force=force)
    try:
        data = client.request("POST", "cases", json=draft.payload)
    except (UpstreamUnavailableError, BlockedError) as exc:
        raise OutcomeUnknownError() from exc
    if not isinstance(data, dict):
        raise OutcomeUnknownError()
    case_id = _text(data.get("caseId")) or _text(data.get("case_id"))
    case_code = _text(data.get("caseCode")) or _text(data.get("case_code"))
    if not case_id and not case_code:
        # A 0000 result without a ticket is not a confirmed filing.
        raise OutcomeUnknownError()
    return {
        "case_id": case_id,
        "case_code": case_code,
        "invoice_id": draft.invoice_id or None,
        "reason": draft.reason_code,
        "open_claim_check": check,
        "sent_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def _check_open_claim(
    client: OmegaClient, draft: ClaimDraft, *, force: bool = False
) -> str:
    try:
        cases = get_cases(client)["cases"]
    except (UpstreamUnavailableError, BlockedError) as exc:
        if force:
            return "skipped"
        raise ClaimCheckFailedError(
            extra={
                "sent": False,
                "hint": "use --force to send the claim without this check",
                "cause": exc.error_type,
            }
        ) from exc
    for case in cases:
        if case.get("type") != CLAIM_MANAGEMENT_TYPE:
            continue
        state = (case.get("state") or "").strip()
        if state in CLAIM_CLOSED_STATES or "cerrad" in state.casefold():
            continue
        same_invoice = case.get("invoice_id") and case.get("invoice_id") == draft.invoice_id
        same_contract = (
            draft.contract_id
            and case.get("contract_id") == draft.contract_id
            and case.get("billing_account_id") == draft.payload.get("billingAccountId")
        )
        if same_invoice or same_contract:
            raise ClaimAlreadyOpenError()
    return "ok"


def _find_raw_context(
    raw_contracts: list[dict[str, Any]], invoice: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    relation = (invoice.get("relations") or [{}])[0]
    contract_number = relation.get("contract_number") or invoice.get("contract_number")
    contract_id = relation.get("contract_id")
    service_account_id = invoice.get("service_account_id")
    candidates = raw_contracts
    if service_account_id:
        candidates = [
            item
            for item in raw_contracts
            if _text(item.get("serviceAccountId")) == service_account_id
        ] or raw_contracts
    for supply in candidates:
        for account in _as_list(
            supply.get("billingAccountList") or supply.get("billingAccount") or []
        ):
            if not isinstance(account, dict):
                continue
            for product in _as_list(account.get("products")):
                if not isinstance(product, dict):
                    continue
                if contract_id and _text(product.get("contractId")) == contract_id:
                    return supply, account, product, relation
                if contract_number and _text(product.get("contractNumber")) == contract_number:
                    return supply, account, product, relation
    return {}, {}, {}, relation


def _first_product(supply: dict[str, Any]) -> dict[str, Any] | None:
    fallback: dict[str, Any] | None = None
    for account in _as_list(
        supply.get("billingAccountList") or supply.get("billingAccount") or []
    ):
        if not isinstance(account, dict):
            continue
        for product in _as_list(account.get("products")):
            if not isinstance(product, dict):
                continue
            if fallback is None:
                fallback = product
            family = _text(_dict(product.get("productType")).get("productTypeFamily"))
            if family in CONSUMPTION_FAMILIES:
                return product
    return fallback


def _account_for_product(
    supply: dict[str, Any], product: dict[str, Any]
) -> dict[str, Any]:
    contract_id = _text(product.get("contractId"))
    for account in _as_list(
        supply.get("billingAccountList") or supply.get("billingAccount") or []
    ):
        if not isinstance(account, dict):
            continue
        for candidate in _as_list(account.get("products")):
            if isinstance(candidate, dict) and _text(candidate.get("contractId")) == contract_id:
                return account
    return {}


def _preferred_contact_id(info: dict[str, Any]) -> str | None:
    preferred = _dict(info.get("preferredContact"))
    contact_id = _text(preferred.get("contactId"))
    if contact_id:
        return contact_id
    for contact in _as_list(info.get("contacts")):
        contact = _dict(contact)
        if _bool(contact.get("isPreferred")):
            return _text(contact.get("contactId"))
    for contact in _as_list(info.get("contacts")):
        contact = _dict(contact)
        if _text(contact.get("contactId")):
            return _text(contact.get("contactId"))
    return None


def _attachment_payload(path: Path) -> dict[str, Any]:
    path = path.expanduser()
    if not path.is_file():
        raise UsageError(f"attachment not found: {path}")
    if path.suffix.casefold() not in config.CLAIM_ATTACHMENT_SUFFIXES:
        raise UsageError(
            "attachments must be PNG, JPEG or PDF: " + ", ".join(config.CLAIM_ATTACHMENT_SUFFIXES)
        )
    size = path.stat().st_size
    if size > config.CLAIM_MAX_ATTACHMENT_BYTES:
        raise UsageError(f"{path.name} is larger than 2 MB")
    raw = path.read_bytes()
    return {
        "file": base64.b64encode(raw).decode("ascii"),
        "fileNameWithExtension": path.name,
        "_raw": raw,
    }


def validate_attachments(paths: list[Path]) -> None:
    total = 0
    for path in paths:
        expanded = path.expanduser()
        if not expanded.is_file():
            raise UsageError(f"attachment not found: {expanded}")
        if expanded.suffix.casefold() not in config.CLAIM_ATTACHMENT_SUFFIXES:
            raise UsageError(
                "attachments must be PNG, JPEG or PDF: "
                + ", ".join(config.CLAIM_ATTACHMENT_SUFFIXES)
            )
        size = expanded.stat().st_size
        if size > config.CLAIM_MAX_ATTACHMENT_BYTES:
            raise UsageError(f"{expanded.name} is larger than 2 MB")
        total += size
    if total > config.CLAIM_MAX_TOTAL_ATTACHMENT_BYTES:
        raise UsageError("the attachments total more than 10 MB")


def attachment_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ---------------------------------------------------------------------- helpers


def _first(*values: Any, default: Any = None) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return default


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _flatten(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    result: list[Any] = []
    for item in value:
        if isinstance(item, list):
            result.extend(item)
        else:
            result.append(item)
    return result


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    comma = text.rfind(",")
    dot = text.rfind(".")
    if comma >= 0 and dot >= 0:
        # Whichever separator appears last is the decimal one; the other groups thousands.
        if comma > dot:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif comma >= 0:
        text = text.replace(",", "") if text.count(",") > 1 else text.replace(",", ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value).strip().casefold() in {"true", "1", "si", "sí", "yes", "y"}


def _unique(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _address_text(value: Any) -> str | None:
    if isinstance(value, str):
        return _text(value)
    if not isinstance(value, dict):
        return None
    if value.get("fullAddress"):
        return _text(value["fullAddress"])
    parts = [
        value.get("wayType"),
        value.get("way"),
        value.get("number"),
        value.get("portal"),
        value.get("stairs"),
        value.get("floor"),
        value.get("door"),
        value.get("zipCode"),
        value.get("city"),
    ]
    text = " ".join(str(part) for part in parts if part not in (None, ""))
    return text or None


def _phone(value: Any) -> str | None:
    if isinstance(value, dict):
        prefix = _text(value.get("prefix"))
        number = _text(value.get("number"))
        if number:
            return f"{prefix} {number}".strip() if prefix else number
        return None
    return _text(value)


def _mask_iban(value: str | None) -> str | None:
    if not value:
        return None
    clean = re.sub(r"\s+", "", value)
    if len(clean) <= 8:
        return "****"
    return f"{clean[:4]} **** **** {clean[-4:]}"


def _months_before(day: date, months: int) -> date:
    month_index = day.year * 12 + (day.month - 1) - months
    year, month = divmod(month_index, 12)
    return date(year, month + 1, 1)


def parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise UsageError("dates must be YYYY-MM-DD or DD/MM/YYYY")
