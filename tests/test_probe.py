from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_probe():
    spec = importlib.util.spec_from_file_location("probe", ROOT / "scripts" / "probe.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_shape_never_includes_values():
    probe = load_probe()
    value = {
        "name": "Ana Ejemplo",
        "nif": "00000000T",
        "amount": 1234.56,
        "date": "2026-10-01",
        "invoiceId": "INV-SECRET",
        "nested": [{"email": "ana@example.com"}],
    }
    dumped = json.dumps(probe.shape(value), ensure_ascii=False)
    for secret in ("Ana", "00000000T", "1234.56", "INV-SECRET", "ana@example.com"):
        assert secret not in dumped
    assert '"amount": "number"' in dumped
    assert '"date": "string(iso-date)"' in dumped


def test_shape_arrays_keep_count_and_samples():
    probe = load_probe()
    shaped = probe.shape([1, 2, 3, 4, 5])
    assert shaped["__type__"] == "array"
    assert shaped["__count__"] == 5
    assert len(shaped["__sample__"]) == 3


def test_shape_scalars():
    probe = load_probe()
    assert probe.shape(None) == "null"
    assert probe.shape(True) == "boolean"
    assert probe.shape(1) == "number"
    assert probe.shape("2026-10-01") == "string(iso-date)"
