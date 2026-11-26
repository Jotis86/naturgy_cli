from __future__ import annotations

from datetime import date

import pytest

from naturgy import api
from naturgy.errors import UsageError


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, method, path, *, params=None, json=None, extra_headers=None):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "json": json,
                "extra_headers": extra_headers,
            }
        )
        value = self.responses.get((method, path))
        if value is None:
            value = self.responses.get(path)
        if value is None:
            raise AssertionError(f"unexpected request: {method} {path}")
        if isinstance(value, Exception):
            raise value
        return value


def test_battery_status_normalizes_balance_and_dates():
    client = FakeClient(
        {
            ("GET", "balanceBattery"): {
                "nif": "00000000T",
                "name": "Holder",
                "current_total": 12.5,
                "is_automatic": True,
                "01": {
                    "total": 12.5,
                    "date_enabled": "2025-01-01",
                    "date_disabled": None,
                },
            }
        }
    )

    result = api.get_battery_status(client)

    assert result["balance"] == 12.5
    assert result["currency"] == "EUR"
    assert result["is_active"] is True
    assert result["is_automatic"] is True
    assert result["date_enabled"] == "2025-01-01"
    assert result["date_disabled"] is None
    assert result["breakdown"] == [
        {
            "code": "01",
            "total": 12.5,
            "date_enabled": "2025-01-01",
            "date_disabled": None,
        }
    ]
    assert "nif" not in result
    assert "name" not in result


def test_battery_status_is_inactive_after_the_disable_date():
    client = FakeClient(
        {
            ("GET", "balanceBattery"): {
                "current_total": 0,
                "is_automatic": False,
                "01": {
                    "total": 0,
                    "date_enabled": "2000-01-01",
                    "date_disabled": "2000-02-01",
                },
            }
        }
    )

    result = api.get_battery_status(client)

    assert result["is_active"] is False


def test_battery_status_falls_back_to_the_breakdown_total():
    client = FakeClient(
        {
            ("GET", "balanceBattery"): {
                "is_automatic": False,
                "01": {
                    "total": 3.5,
                    "date_enabled": "2025-01-01",
                    "date_disabled": None,
                },
            }
        }
    )

    result = api.get_battery_status(client)

    assert result["balance"] == 3.5


def test_battery_status_raw_includes_the_source():
    payload = {"current_total": 1.0, "is_automatic": True}
    client = FakeClient({("GET", "balanceBattery"): payload})

    result = api.get_battery_status(client, raw=True)

    assert result["source"] is payload
    assert result["breakdown"] == []


def test_battery_history_normalizes_and_paginates():
    payload = {
        "totalItems": 28,
        "totalPages": 3,
        "currentPage": 2,
        "data": [
            {
                "id": "MOV1",
                "nif": "00000000T",
                "vkont": "CT1",
                "supply": "SA1",
                "factnum": "INV1",
                "quantity": "1,23",
                "concept": "Nuevo saldo",
                "is_self_consumption": True,
                "created_at": "2026-01-02",
                "accounting_date": "2026-01-03",
                "address": "Calle Ejemplo 1",
            },
            {
                "id": "MOV2",
                "vkont": "CT1",
                "quantity": "-0,50",
                "concept": "Gasto saldo",
            },
        ],
    }
    client = FakeClient({("GET", "historyBattery"): payload})

    result = api.get_battery_history(client, page=2, limit=10)

    assert result["count"] == 2
    assert result["page"] == 2
    assert result["limit"] == 10
    assert result["total_pages"] == 3
    assert result["total_items"] == 28
    first = result["movements"][0]
    assert first["id"] == "MOV1"
    assert first["kind"] == "charge"
    assert first["amount"] == 1.23
    assert first["invoice_id"] == "INV1"
    assert first["contract"] == "CT1"
    assert first["is_self_consumption"] is True
    assert result["movements"][1]["kind"] == "discharge"
    assert result["movements"][1]["amount"] == -0.5
    assert "nif" not in first
    call = client.calls[0]
    assert call["extra_headers"] == {"page": "2", "limit": "10"}


def test_battery_history_supply_filter_is_client_side():
    payload = {
        "totalItems": 2,
        "totalPages": 1,
        "currentPage": 1,
        "data": [
            {"id": "1", "vkont": "CT1", "quantity": "1"},
            {"id": "2", "vkont": "CT2", "quantity": "2"},
        ],
    }
    client = FakeClient({("GET", "historyBattery"): payload})

    result = api.get_battery_history(client, supply="CT1")
    assert [item["id"] for item in result["movements"]] == ["1"]

    empty = api.get_battery_history(client, supply="missing")
    assert empty["movements"] == []


@pytest.mark.parametrize(
    ("concept", "kind"),
    [
        ("Nuevo saldo", "charge"),
        ("Carga", "charge"),
        ("Gasto saldo", "discharge"),
        ("Descarga", "discharge"),
        ("Caducado", "expired"),
        ("Factura rectificativa", "adjustment"),
        (None, None),
        ("otro", None),
    ],
)
def test_battery_kind_maps_the_api_labels(concept, kind):
    assert api._battery_kind(concept) == kind


@pytest.mark.parametrize(
    ("page", "limit"),
    [(0, 20), (-1, 20), (1, 0), (1, 101)],
)
def test_battery_history_rejects_invalid_pagination(page, limit):
    with pytest.raises(UsageError):
        api.get_battery_history(FakeClient({}), page=page, limit=limit)


def test_battery_evolution_sorts_months_and_normalizes_totals():
    payload = {
        "10": [{"origin": "battery", "total": "1,50"}],
        "05": [{"origin": "battery", "total": "2"}],
    }
    client = FakeClient({("GET", "evolutionBalanceBattery"): payload})

    result = api.get_battery_evolution(client, 2026)

    assert result["year"] == 2026
    assert [month["month"] for month in result["months"]] == ["05", "10"]
    assert result["months"][0]["entries"][0]["total"] == 2.0
    assert result["months"][1]["entries"][0]["total"] == 1.5
    assert client.calls[0]["params"] == {"year": 2026}


def test_battery_evolution_defaults_to_the_current_year():
    client = FakeClient({("GET", "evolutionBalanceBattery"): {}})

    result = api.get_battery_evolution(client)

    assert result["year"] == date.today().year
    assert result["months"] == []


def test_battery_evolution_rejects_an_invalid_year():
    with pytest.raises(UsageError):
        api.get_battery_evolution(FakeClient({}), 1800)


def test_battery_assignments_normalizes_and_filters():
    payload = {
        "total": 10.0,
        "reserves": [
            {
                "id": "R1",
                "vkont": "CT1",
                "supply": "SA1",
                "quantity": "10,00",
                "enabled": True,
                "supply_active": True,
                "created_at": "2025-01-01",
                "updated_at": "2025-02-01",
                "address": "Calle Ejemplo 1",
            },
            {
                "id": "R2",
                "vkont": "CT2",
                "quantity": "0",
                "enabled": False,
                "supply_active": False,
            },
        ],
    }
    client = FakeClient({("GET", "assignmentBattery"): payload})

    result = api.get_battery_assignments(client)

    assert result["count"] == 2
    assert result["total"] == 10.0
    assert result["reserves"][0]["assigned"] == 10.0
    assert result["reserves"][0]["enabled"] is True
    assert result["reserves"][1]["enabled"] is False

    filtered = api.get_battery_assignments(client, supply="CT2")
    assert [item["id"] for item in filtered["reserves"]] == ["R2"]

    with pytest.raises(UsageError):
        api.get_battery_assignments(client, supply="missing")


def test_battery_assignments_raw_includes_the_source():
    payload = {"total": 0.0, "reserves": []}
    client = FakeClient({("GET", "assignmentBattery"): payload})

    result = api.get_battery_assignments(client, raw=True)

    assert result["source"] is payload
