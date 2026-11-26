"""Opt-in read-only checks against the real account.

    NATURGY_LIVE=1 uv run --no-sync pytest -m live

They use the normal token file (or $NATURGY_TOKENS_FILE) and never send a claim.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from naturgy import api
from naturgy.config import default_tokens_path
from naturgy.tokens import FileTokenStore

pytestmark = pytest.mark.live


def live_store() -> FileTokenStore:
    if os.environ.get("NATURGY_LIVE") != "1":
        pytest.skip("set NATURGY_LIVE=1 to run read-only live checks")
    path = Path(os.environ.get("NATURGY_TOKENS_FILE") or default_tokens_path())
    store = FileTokenStore(path)
    if not path.exists():
        pytest.skip(f"no token file at {path}; run `naturgy auth login` first")
    return store


def test_me_live():
    payload = api.run_with_client(
        live_store(), lambda client, tokens: api.get_me(client, tokens)
    )
    assert payload["account"]["client_code"]


def test_supplies_live():
    payload = api.run_with_client(live_store(), lambda client, _: api.get_supplies(client))
    assert isinstance(payload["supplies"], list)


def test_invoices_live():
    def operation(client, _):
        supplies = api.get_supplies(client)["supplies"]
        return api.get_invoices(client, supplies)

    payload = api.run_with_client(live_store(), operation)
    assert isinstance(payload["invoices"], list)
