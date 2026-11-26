from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from naturgy import api
from naturgy.cli import app
from naturgy.models import TokenSet
from naturgy.tokens import FileTokenStore

runner = CliRunner()


CLIENT = {
    "clientId": "C1",
    "clientCode": "00000000T",
    "docNumber": "00000000T",
    "name": "Holder",
    "preferredContact": {"contactId": "CON1"},
    "contacts": [{"contactId": "CON1", "isPreferred": True, "isHolder": True}],
}

SUPPLY = {
    "serviceAccountId": "SA1",
    "clientId": "C1",
    "supplyAddress": {"fullAddress": "Calle Ejemplo 1"},
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
                    },
                }
            ],
        }
    ],
}

INVOICE = {
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


class Router:
    """Small request router for httpx.MockTransport."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, object]] = {}
        self.calls: list[dict[str, object]] = []

    def add(
        self, method: str, path: str, payload: object = None, *, status: int = 200
    ) -> None:
        self.routes[(method, path.strip("/"))] = (status, payload)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.strip("/")
        self.calls.append(
            {
                "method": request.method,
                "path": path,
                "params": dict(request.url.params),
                "headers": dict(request.headers),
            }
        )
        route = self.routes.get((request.method, path))
        if route is None:
            return httpx.Response(
                404,
                json={"error": f"unexpected {request.method} {path}"},
                request=request,
            )
        status, payload = route
        if callable(payload):
            payload = payload(request)
        return httpx.Response(status, json=payload, request=request)


def make_router() -> Router:
    router = Router()
    router.add("GET", "clients", {"client": CLIENT})
    router.add(
        "GET",
        "authorization",
        {"authorization": [{"accountid": "ACC1", "contactid": "CON1"}]},
    )
    router.add("GET", "contractAccountsNew", {"serviceAccountList": [SUPPLY]})
    router.add(
        "GET",
        "invoices",
        {"InvoicesPDS": [INVOICE], "isContingencyMode": False},
    )
    return router


def make_jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return b".".join([header, body, b"signature"]).decode()


@pytest.fixture
def token_file(tmp_path) -> Path:
    path = tmp_path / "tokens.json"
    FileTokenStore(path).save(
        TokenSet(
            token="access",
            user={
                "clientCode": "00000000T",
                "clientId": "C1",
                "roleTypeId": "newcoClient",
            },
            nif="00000000T",
            account_id="ACC1",
            contact_id="CON1",
        )
    )
    return path


@pytest.fixture
def use_transport(monkeypatch):
    def install(router: Router) -> Router:
        transport = httpx.MockTransport(router)
        real_client = api.OmegaClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            kwargs.setdefault("pacing", 0)
            kwargs.setdefault("base_url", "https://api.test")
            return real_client(*args, **kwargs)

        monkeypatch.setattr(api, "OmegaClient", factory)
        return router

    return install


def invoke(token_file: Path, *args: str):
    return runner.invoke(app, ["--tokens", str(token_file), *args])


def test_cli_me_returns_json(token_file, use_transport):
    use_transport(make_router())

    result = invoke(token_file, "me")

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["account"]["client_code"] == "00000000T"
    assert payload["authorization"][0]["accountid"] == "ACC1"


def test_cli_supplies_returns_normalized_products(token_file, use_transport):
    use_transport(make_router())

    result = invoke(token_file, "supplies")

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    product = payload["supplies"][0]["products"][0]
    assert product["contract_number"] == "CN1"
    assert product["cups"] == "ES0000000001"


def test_cli_invoices_list_returns_json(token_file, use_transport):
    use_transport(make_router())

    result = invoke(token_file, "invoices", "list")

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["invoices"][0]["invoice_id"] == "INV1"


def test_cli_battery_status_and_history(token_file, use_transport):
    router = make_router()
    router.add(
        "GET",
        "balanceBattery",
        {
            "current_total": 12.5,
            "is_automatic": True,
            "01": {
                "total": 12.5,
                "date_enabled": "2025-01-01",
                "date_disabled": None,
            },
        },
    )
    router.add(
        "GET",
        "historyBattery",
        {
            "totalItems": 4,
            "totalPages": 2,
            "currentPage": 2,
            "data": [
                {"id": "M1", "vkont": "CT1", "quantity": "1,23", "concept": "Nuevo saldo"},
                {"id": "M2", "vkont": "CT2", "quantity": "-0,50", "concept": "Gasto saldo"},
            ],
        },
    )
    use_transport(router)

    status = invoke(token_file, "battery", "status")
    assert status.exit_code == 0, status.stderr
    assert json.loads(status.stdout)["balance"] == 12.5

    history = invoke(token_file, "battery", "history", "--page", "2", "--limit", "2")
    assert history.exit_code == 0, history.stderr
    payload = json.loads(history.stdout)
    assert payload["count"] == 2
    assert payload["page"] == 2
    assert payload["movements"][0]["kind"] == "charge"
    assert payload["movements"][1]["kind"] == "discharge"

    call = next(call for call in router.calls if call["path"] == "historyBattery")
    assert call["headers"]["page"] == "2"
    assert call["headers"]["limit"] == "2"


def test_cli_battery_supply_filter_and_other_views(token_file, use_transport):
    router = make_router()
    router.add(
        "GET",
        "historyBattery",
        {
            "totalItems": 2,
            "totalPages": 1,
            "currentPage": 1,
            "data": [
                {"id": "M1", "vkont": "CT1", "quantity": "1"},
                {"id": "M2", "vkont": "CT2", "quantity": "2"},
            ],
        },
    )
    router.add(
        "GET",
        "evolutionBalanceBattery",
        {"05": [{"origin": "battery", "total": "1,50"}]},
    )
    router.add(
        "GET",
        "assignmentBattery",
        {
            "total": 10.0,
            "reserves": [
                {
                    "id": "R1",
                    "vkont": "CT1",
                    "quantity": "10,00",
                    "enabled": True,
                    "supply_active": True,
                }
            ],
        },
    )
    use_transport(router)

    history = invoke(token_file, "battery", "history", "--supply", "CT1")
    assert history.exit_code == 0, history.stderr
    assert [item["id"] for item in json.loads(history.stdout)["movements"]] == ["M1"]

    evolution = invoke(token_file, "battery", "evolution", "--year", "2026")
    assert evolution.exit_code == 0, evolution.stderr
    assert json.loads(evolution.stdout)["months"][0]["month"] == "05"

    assignments = invoke(token_file, "battery", "assignments")
    assert assignments.exit_code == 0, assignments.stderr
    assert json.loads(assignments.stdout)["reserves"][0]["assigned"] == 10.0


def test_cli_invoice_download_and_skip_existing(tmp_path, token_file, use_transport):
    router = make_router()
    router.add(
        "GET",
        "pdfDocument",
        {"pdfDocument": base64.b64encode(b"%PDF-1.4\nfake\n").decode()},
    )
    use_transport(router)
    output_dir = tmp_path / "pdfs"

    first = invoke(token_file, "invoices", "download", "--dir", str(output_dir))

    assert first.exit_code == 0, first.stderr
    payload = json.loads(first.stdout)
    assert payload["invoices"][0]["status"] == "downloaded"
    path = Path(payload["invoices"][0]["file"])
    assert path.read_bytes().startswith(b"%PDF")
    assert path.stat().st_mode & 0o777 == 0o600

    second = invoke(token_file, "invoices", "download", "--dir", str(output_dir))

    assert second.exit_code == 0, second.stderr
    assert json.loads(second.stdout)["invoices"][0]["status"] == "skipped"
    assert sum(1 for call in router.calls if call["path"] == "pdfDocument") == 1


def test_cli_claim_dry_run_never_posts(token_file, use_transport):
    router = make_router()
    use_transport(router)

    result = invoke(
        token_file,
        "invoices",
        "claim",
        "INV1",
        "-m",
        "No reconozco el cargo",
        "--dry-run",
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["payload"]["invoiceId"] == "INV1"
    assert not any(call["path"] == "cases" for call in router.calls)


def test_cli_claim_without_confirmation_fails_before_the_api(token_file, use_transport):
    router = use_transport(make_router())

    result = invoke(
        token_file,
        "invoices",
        "claim",
        "INV1",
        "-m",
        "No reconozco el cargo",
    )

    assert result.exit_code == 2
    assert "not sent" in result.stderr
    assert router.calls == []


def test_cli_claim_fails_closed_when_the_open_check_fails(token_file, use_transport):
    router = make_router()
    router.add("GET", "cases", {"error": "unavailable"}, status=503)
    use_transport(router)

    result = invoke(
        token_file,
        "invoices",
        "claim",
        "INV1",
        "-m",
        "No reconozco el cargo",
        "--yes",
    )

    assert result.exit_code == 5
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "claim_check_failed"
    assert error["sent"] is False
    assert not any(call["method"] == "POST" for call in router.calls)


def test_cli_claim_success_returns_the_case(token_file, use_transport):
    router = make_router()
    router.add("GET", "cases", {"cases": []})
    router.add("POST", "cases", {"caseId": "CASE1", "caseCode": "CODE1"})
    use_transport(router)

    result = invoke(
        token_file,
        "invoices",
        "claim",
        "INV1",
        "-m",
        "No reconozco el cargo",
        "--yes",
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["case_id"] == "CASE1"
    assert payload["open_claim_check"] == "ok"
    assert any(call["method"] == "POST" for call in router.calls)


@pytest.mark.parametrize(
    ("status", "exit_code", "error_type"),
    [(401, 3, "auth_expired"), (403, 5, "blocked"), (404, 7, "api_changed")],
)
def test_cli_maps_http_errors_to_exit_codes(
    status, exit_code, error_type, token_file, use_transport
):
    router = Router()
    router.add("GET", "clients", {"error": "nope"}, status=status)
    use_transport(router)

    result = invoke(token_file, "me")

    assert result.exit_code == exit_code
    assert json.loads(result.stderr)["error"]["type"] == error_type


def test_cli_auth_status_expired_exits_3(tmp_path):
    path = tmp_path / "tokens.json"
    expired = make_jwt({"exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())})
    FileTokenStore(path).save(TokenSet(token=expired))

    result = runner.invoke(app, ["--tokens", str(path), "auth", "status"])

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["access_token"]["expired"] is True
