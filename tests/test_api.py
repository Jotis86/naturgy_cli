from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from naturgy import api
from naturgy.errors import (
    AuthError,
    ClaimAlreadyOpenError,
    ClaimCheckFailedError,
    UpstreamUnavailableError,
    UsageError,
)
from naturgy.models import TokenSet
from naturgy.tokens import FileTokenStore

RAW_SUPPLY = {
    "serviceAccountId": "SA1",
    "clientId": "C1",
    "supplyAddress": {"fullAddress": "Calle Falsa 1, 28001 Madrid", "zipCode": "28001"},
    "billingAccountList": [
        {
            "billingAccountId": "BA1",
            "refNum": "REF1",
            "isDirectPayment": True,
            "payDay": "5",
            "bankAccount": {"iban": "ES1234567890123456789012"},
            "products": [
                {
                    "contractId": "CT1",
                    "contractNumber": "CN1",
                    "billingAccountId": "BA1",
                    "productType": {
                        "productTypeFamily": "luz",
                        "productTypeName": "Tarifa Luz",
                        "productCode": "E0001",
                        "productStatus": "03",
                        "Assetid": "A1",
                    },
                    "techData": {
                        "cups": "ES0000000001",
                        "rate": "2.0TD",
                        "powerPlain": 4.6,
                        "powerValley": 4.6,
                    },
                }
            ],
        }
    ],
}

RAW_INVOICE = {
    "invoiceId": "INV1",
    "type": "NC",
    "status": "03",
    "issuedDate": "2026-10-01",
    "billingDate": "2026-10-01",
    "amount": 54.21,
    "pendingAmount": 0,
    "pdfDocumentId": "PDF1",
    "productInvoiceRelationList": [
        {
            "contractId": "CT1",
            "contractNumber": "CN1",
            "productFamilyId": "luz",
            "billingAccountId": "BA1",
            "invoice": "ATR1",
        }
    ],
}


def make_jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return b".".join([header, body, b"signature"]).decode()


class FakeClient:
    def __init__(self, responses, info=None):
        self.responses = responses
        self.info = info or {
            "docNumber": "00000000T",
            "preferredContact": {"contactId": "CONT1"},
        }
        self.calls = []

    def request(self, method, path, *, params=None, json=None, auth=True, expect_json=True):
        self.calls.append(
            {"method": method, "path": path, "params": params, "json": json}
        )
        value = self.responses.get((method, path))
        if value is None:
            value = self.responses.get(path)
        if value is None:
            raise AssertionError(f"unexpected request: {method} {path}")
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(params, json)
        return value

    def get_client_info(self):
        return self.info


def supplies():
    return [api._normalize_supply(RAW_SUPPLY)]


def invoice_responses(invoice=None):
    if isinstance(invoice, dict) and "InvoicesPDS" in invoice:
        payload = invoice
    else:
        payload = {"InvoicesPDS": [invoice or RAW_INVOICE], "isContingencyMode": False}
    return {
        "contractAccountsNew": {"serviceAccountList": [RAW_SUPPLY]},
        ("GET", "invoices"): payload,
    }


def test_normalize_supply_and_mask_iban():
    supply = supplies()[0]
    assert supply["service_account_id"] == "SA1"
    assert supply["address"] == "Calle Falsa 1, 28001 Madrid"
    product = supply["products"][0]
    assert product["contract_number"] == "CN1"
    assert product["product_family"] == "luz"
    assert product["cups"] == "ES0000000001"
    assert product["is_active"] is True
    assert supply["billing_accounts"][0]["iban_masked"] == "ES12 **** **** 9012"


def test_product_uses_is_active_and_contract_status():
    raw = {
        "contractId": "CT1",
        "contractNumber": "CN1",
        "contractStatus": "02",
        "isActive": False,
        "productType": {"productTypeFamily": "luz", "productStatus": "02"},
        "techData": {"p1": 4.6, "p2": 4.6, "cups": "ES1"},
    }
    product = api._normalize_product(raw, {"billingAccountId": "BA1"})
    assert product["status"] == "02"
    assert product["is_active"] is False
    assert product["powers"] == {"p1": 4.6, "p2": 4.6}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.234,56", 1234.56),
        ("1,234.56", 1234.56),
        ("1.234.567,89", 1234567.89),
        ("1,234,567.89", 1234567.89),
        ("1234,56", 1234.56),
        ("1234.56", 1234.56),
        ("-1.234,56", -1234.56),
        ("0", 0.0),
        ("", None),
        (None, None),
        (True, None),
        ("n/a", None),
    ],
)
def test_num_accepts_spanish_and_english_number_formats(raw, expected):
    assert api._num(raw) == expected


def test_num_keeps_the_legacy_meaning_of_a_single_separator():
    # A single separator is ambiguous; keep the old behavior and document it here.
    assert api._num("1.234") == 1.234
    assert api._num("1,234") == 1.234


def test_invoices_use_billing_ref_and_prefer_pending():
    duplicate = dict(RAW_INVOICE, pendingAmount=12.5)
    client = FakeClient(invoice_responses({"InvoicesPDS": [RAW_INVOICE, duplicate]}))
    result = api.get_invoices(client, supplies())
    assert result["count"] == 1
    assert result["invoices"][0]["pending_amount"] == 12.5
    call = next(call for call in client.calls if call["path"] == "invoices")
    assert call["params"]["contractAccountId"] == "REF1"
    assert call["params"]["contractNumbers"] == "CN1"
    assert call["params"]["cups"] == "ES0000000001"


def test_invoice_of_another_family_is_filtered():
    gas = dict(RAW_INVOICE, invoiceId="INV2")
    gas["productInvoiceRelationList"] = [
        dict(RAW_INVOICE["productInvoiceRelationList"][0], productFamilyId="gas")
    ]
    client = FakeClient(invoice_responses({"InvoicesPDS": [gas]}))
    result = api.get_invoices(client, supplies())
    assert result["count"] == 0


def test_invoice_uses_period_from():
    raw = dict(RAW_INVOICE, periodFrom="2026-09-01", periodEnd="2026-09-30")
    invoice = api._normalize_invoice(
        raw,
        service_account_id="SA1",
        billing_account_id="BA1",
        ref_num="REF1",
    )
    assert invoice["period_start"] == "2026-09-01"
    assert invoice["period_end"] == "2026-09-30"


def test_reading_with_null_detail_list_does_not_fail():
    reading = api._normalize_reading(
        {
            "agentInfo": {"cups": "ES1"},
            "invoiceId": "INV1",
            "productTypeFamily": "luz",
            "readingDetailList": None,
        }
    )
    assert reading["invoice_id"] == "INV1"
    assert reading["product_family"] == "luz"
    assert reading["cups"] == "ES1"
    assert reading["items"] == []


def test_invoice_filename_is_safe():
    assert (
        api.invoice_filename({"invoice_id": "AB/12 34", "billing_date": "2026-10-01"})
        == "factura_2026-10_AB_12_34.pdf"
    )
    assert (
        api.invoice_filename({"invoice_id": "../x", "issued_date": ""})
        == "factura_x.pdf"
    )
    assert (
        api.invoice_filename({"invoice_id": "X", "billing_date": "01/10/2026"})
        == "factura_2026-10_X.pdf"
    )


def test_decode_pdf():
    payload = b"%PDF-1.7 test"
    assert api._decode_pdf(base64.b64encode(payload).decode()) == payload
    assert api._decode_pdf("data:application/pdf;base64," + base64.b64encode(payload).decode()) == payload


def test_validate_attachments_rejects_an_unknown_suffix(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("not an attachment", encoding="utf-8")
    with pytest.raises(UsageError):
        api.validate_attachments([path])


def test_claim_text_validation():
    assert api.claim_text("  hola\nmundo  ") == "hola\nmundo"
    with pytest.raises(UsageError):
        api.claim_text("")
    with pytest.raises(UsageError):
        api.claim_text("x" * (api.config.CLAIM_MAX_LENGTH + 1))
    with pytest.raises(UsageError):
        api.claim_text("hola\x00mundo")


def test_prepare_claim_payload():
    client = FakeClient(invoice_responses())
    tokens = TokenSet(token="access", user={"clientCode": "00000000T"})
    draft = api.prepare_claim(
        client,
        tokens,
        "INV1",
        "No reconozco el cargo",
        reason="wrong-charge",
        supplies=supplies(),
    )
    payload = draft.payload
    assert payload["managementType"] == "claimInvoice"
    assert payload["reasonSelected"] == "03"
    assert payload["invoiceId"] == "INV1"
    assert payload["issuedDate"] == "2026-10-01"
    assert payload["serviceAccountId"] == "SA1"
    assert payload["contractId"] == "CT1"
    assert payload["billingAccountId"] == "BA1"
    assert payload["invoiceAtrId"] == "ATR1"
    assert payload["preferredContactId"] == "CONT1"
    assert payload["product"]["assetId"] == "A1"
    assert payload["product"]["productTypeFamily"] == "luz"
    assert payload["files"] == []
    assert draft.invoice_amount == 54.21


def test_send_claim_success():
    responses = invoice_responses()
    responses[("GET", "cases")] = {"cases": []}
    responses[("POST", "cases")] = {"caseId": "CASE1", "caseCode": "CODE1"}
    client = FakeClient(responses)
    tokens = TokenSet(token="access", user={"clientCode": "00000000T"})
    draft = api.prepare_claim(
        client, tokens, "INV1", "No reconozco el cargo", supplies=supplies()
    )
    result = api.send_claim(client, draft)
    assert result["case_id"] == "CASE1"
    assert result["case_code"] == "CODE1"
    assert result["invoice_id"] == "INV1"
    assert result["open_claim_check"] == "ok"


def test_send_claim_refuses_when_the_open_check_fails():
    responses = invoice_responses()
    responses[("GET", "cases")] = UpstreamUnavailableError("cases unavailable")
    client = FakeClient(responses)
    tokens = TokenSet(token="access", user={"clientCode": "00000000T"})
    draft = api.prepare_claim(
        client, tokens, "INV1", "No reconozco el cargo", supplies=supplies()
    )
    with pytest.raises(ClaimCheckFailedError) as info:
        api.send_claim(client, draft)
    assert info.value.extra["sent"] is False
    assert not any(call["method"] == "POST" for call in client.calls)


def test_send_claim_force_sends_when_the_open_check_fails():
    responses = invoice_responses()
    responses[("GET", "cases")] = UpstreamUnavailableError("cases unavailable")
    responses[("POST", "cases")] = {"caseId": "CASE1", "caseCode": "CODE1"}
    client = FakeClient(responses)
    tokens = TokenSet(token="access", user={"clientCode": "00000000T"})
    draft = api.prepare_claim(
        client, tokens, "INV1", "No reconozco el cargo", supplies=supplies()
    )
    result = api.send_claim(client, draft, force=True)
    assert result["case_id"] == "CASE1"
    assert result["open_claim_check"] == "skipped"
    assert any(call["method"] == "POST" for call in client.calls)


def test_send_claim_force_does_not_bypass_a_confirmed_open_claim():
    responses = invoice_responses()
    responses[("GET", "cases")] = {
        "cases": [
            {
                "typeId": "claimInvoice",
                "state": "Abierto",
                "invoiceId": "INV1",
                "billingAccountId": "BA1",
                "contractId": "CT1",
            }
        ]
    }
    client = FakeClient(responses)
    tokens = TokenSet(token="access", user={"clientCode": "00000000T"})
    draft = api.prepare_claim(
        client, tokens, "INV1", "No reconozco el cargo", supplies=supplies()
    )
    with pytest.raises(ClaimAlreadyOpenError):
        api.send_claim(client, draft, force=True)
    assert not any(call["method"] == "POST" for call in client.calls)


def test_send_claim_refuses_when_open():
    responses = invoice_responses()
    responses[("GET", "cases")] = {
        "cases": [
            {
                "typeId": "claimInvoice",
                "state": "Abierto",
                "invoiceId": "INV1",
                "billingAccountId": "BA1",
                "contractId": "CT1",
            }
        ]
    }
    client = FakeClient(responses)
    tokens = TokenSet(token="access", user={"clientCode": "00000000T"})
    draft = api.prepare_claim(
        client, tokens, "INV1", "No reconozco el cargo", supplies=supplies()
    )
    with pytest.raises(ClaimAlreadyOpenError):
        api.send_claim(client, draft)


def test_consumption_series():
    client = FakeClient(
        {
            "consumptions": {
                "consumptionValues": [
                    {
                        "date": "2026-09-01",
                        "valueTotal": "10.500",
                        "valuePlain": "4",
                        "valueValley": "2",
                        "P1": "1",
                    }
                ]
            }
        }
    )
    result = api.get_consumption(client, supplies(), period="month")
    product = result["products"][0]
    assert product["product_family"] == "luz"
    assert product["series"][0]["total"] == 10.5
    assert product["series"][0]["p1"] == 1.0
    call = client.calls[0]
    assert call["params"]["startDate"].count("/") == 2
    assert call["params"]["productType"] == "luz"


def test_select_supplies():
    assert len(api.select_supplies(supplies(), "REF1")) == 1
    assert len(api.select_supplies(supplies(), "CN1")) == 1
    assert len(api.select_supplies(supplies(), "ES0000")) == 1
    with pytest.raises(UsageError):
        api.select_supplies(supplies(), "does-not-exist")


def test_expired_session_fails_before_any_request(tmp_path):
    store = FileTokenStore(tmp_path / "tokens.json")
    expired = make_jwt({"exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())})
    store.save(TokenSet(token=expired))
    called = []
    with pytest.raises(AuthError):
        api.run_with_client(store, lambda client, tokens: called.append(True))
    assert called == []
