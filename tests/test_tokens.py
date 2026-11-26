from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from naturgy.errors import AuthError
from naturgy.models import TokenSet
from naturgy.tokens import FileTokenStore, jwt_exp, token_status


def make_jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return b".".join([header, body, b"signature"]).decode()


def test_save_and_load_round_trip(tmp_path):
    store = FileTokenStore(tmp_path / "tokens.json")
    tokens = TokenSet(
        token="access",
        id_token=make_jwt({"exp": 4102444800}),
        refresh_token="refresh",
        user={"clientCode": "00000000T", "roleTypeId": "newcoClient"},
        nif="00000000T",
    )
    store.save(tokens)
    assert store.path.stat().st_mode & 0o777 == 0o600
    loaded = store.load()
    assert loaded is not None
    assert loaded.token == "access"
    assert loaded.nif == "00000000T"
    assert loaded.user["clientCode"] == "00000000T"


def test_browser_export_is_accepted(tmp_path):
    path = tmp_path / "browser.json"
    path.write_text(
        json.dumps(
            {
                "token": "access",
                "idToken": "id",
                "refreshToken": "refresh",
                "userId": "U1",
                "clientCode": "00000000T",
                "clientId": "C1",
                "roleTypeId": "newcoClient",
            }
        ),
        encoding="utf-8",
    )
    tokens = FileTokenStore(path).load()
    assert tokens is not None
    assert tokens.token == "access"
    assert tokens.user["clientId"] == "C1"
    assert tokens.user["roleTypeId"] == "newcoClient"


def test_browser_export_maps_client_id_to_account_id(tmp_path):
    path = tmp_path / "browser.json"
    path.write_text(
        json.dumps(
            {
                "token": "access",
                "idToken": "id",
                "refreshToken": "refresh",
                "userId": "U1",
                "clientCode": "00000000T",
                "clientId": "ACC1",
                "roleTypeId": "newcoClient",
            }
        ),
        encoding="utf-8",
    )
    tokens = FileTokenStore(path).load()
    assert tokens is not None
    assert tokens.account_id == "ACC1"


def test_missing_token_file_returns_none(tmp_path):
    assert FileTokenStore(tmp_path / "missing.json").load() is None


def test_empty_token_file_is_auth_error(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("", encoding="utf-8")
    with pytest.raises(AuthError):
        FileTokenStore(path).load()


def test_jwt_exp_and_status():
    expires = datetime.now(UTC) + timedelta(hours=1)
    token = make_jwt({"exp": int(expires.timestamp())})
    parsed = jwt_exp(token)
    assert parsed is not None
    assert abs((parsed - expires).total_seconds()) < 2
    status = token_status(TokenSet(token=token, user={"clientCode": "x"}))
    assert status["access_token"]["expired"] is False
    assert status["refresh_token"]["usable"] is False


def test_expired_status():
    token = make_jwt({"exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())})
    status = token_status(TokenSet(token=token))
    assert status["access_token"]["expired"] is True


def test_opaque_token_has_no_expiry():
    status = token_status(TokenSet(token="opaque-token"))
    assert status["access_token"]["expires_at"] is None
    assert status["access_token"]["expired"] is None


def test_delete(tmp_path):
    store = FileTokenStore(tmp_path / "tokens.json")
    store.save(TokenSet(token="x"))
    assert store.delete() is True
    assert store.delete() is False
