from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from naturgy import auth
from naturgy.errors import (
    AuthError,
    LoginError,
    UpstreamUnavailableError,
    UsageError,
)
from naturgy.models import TokenSet
from naturgy.tokens import FileTokenStore


def make_jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return b".".join([header, body, b"signature"]).decode()


class FakeClient:
    def __init__(
        self,
        *,
        login_error: Exception | None = None,
        identity_error: Exception | None = None,
        info_error: Exception | None = None,
        logout_error: Exception | None = None,
    ) -> None:
        self.login_error = login_error
        self.identity_error = identity_error
        self.info_error = info_error
        self.logout_error = logout_error
        self.login_args: tuple[str, str] | None = None
        self.closed = False

    def login(self, nif: str, password: str) -> TokenSet:
        self.login_args = (nif, password)
        if self.login_error:
            raise self.login_error
        return TokenSet(token="access", id_token="id", user={"clientCode": nif})

    def fetch_identity(self) -> dict[str, str]:
        if self.identity_error:
            raise self.identity_error
        return {"account_id": "ACC1", "contact_id": "CON1"}

    def ensure_identity(self) -> bool:
        return False

    def get_client_info(self) -> dict:
        if self.info_error:
            raise self.info_error
        return {
            "clientId": "C1",
            "clientCode": "00000000T",
            "docNumber": "00000000T",
            "preferredContact": {"contactId": "CON1"},
        }

    def logout(self) -> None:
        if self.logout_error:
            raise self.logout_error

    def close(self) -> None:
        self.closed = True


def install_fake(monkeypatch, fake: FakeClient) -> None:
    monkeypatch.setattr(auth, "OmegaClient", lambda *args, **kwargs: fake)


def test_login_saves_session_identity_and_profile(tmp_path, monkeypatch):
    store = FileTokenStore(tmp_path / "tokens.json")
    fake = FakeClient()
    install_fake(monkeypatch, fake)

    payload = auth.login(store, " 12.345.678-z ", " secret ")

    assert payload["signed_in"] is True
    assert payload["nif"] == "12345678Z"
    assert fake.login_args is not None
    assert fake.login_args[0] == "12345678Z"
    assert fake.closed is True
    tokens = store.load()
    assert tokens is not None
    assert tokens.nif == "12345678Z"
    assert tokens.account_id == "ACC1"
    assert tokens.contact_id == "CON1"
    assert tokens.user["clientId"] == "C1"


def test_login_failure_does_not_write_a_session(tmp_path, monkeypatch):
    store = FileTokenStore(tmp_path / "tokens.json")
    fake = FakeClient(login_error=LoginError("bad", code="PS01"))
    install_fake(monkeypatch, fake)

    with pytest.raises(LoginError):
        auth.login(store, "12345678Z", "wrong")

    assert not store.path.exists()
    assert fake.closed is True


def test_rejected_identity_deletes_the_new_session(tmp_path, monkeypatch):
    store = FileTokenStore(tmp_path / "tokens.json")
    fake = FakeClient(identity_error=AuthError())
    install_fake(monkeypatch, fake)

    with pytest.raises(AuthError):
        auth.login(store, "12345678Z", "secret")

    assert not store.path.exists()


def test_identity_outage_keeps_the_session_with_a_warning(tmp_path, monkeypatch):
    store = FileTokenStore(tmp_path / "tokens.json")
    fake = FakeClient(identity_error=UpstreamUnavailableError())
    install_fake(monkeypatch, fake)

    payload = auth.login(store, "12345678Z", "secret")

    assert payload["signed_in"] is True
    assert "warning" in payload
    assert store.load() is not None


def test_import_browser_export(tmp_path):
    store = FileTokenStore(tmp_path / "tokens.json")
    path = tmp_path / "session-user.json"
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

    payload = auth.import_tokens(store, path)

    assert payload["signed_in"] is True
    tokens = store.load()
    assert tokens is not None
    assert tokens.nif == "00000000T"
    assert tokens.account_id == "C1"


def test_import_rejects_invalid_json(tmp_path):
    store = FileTokenStore(tmp_path / "tokens.json")
    path = tmp_path / "session-user.json"
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(UsageError):
        auth.import_tokens(store, path)


def test_status_without_a_session_is_an_auth_error(tmp_path):
    with pytest.raises(AuthError):
        auth.status(FileTokenStore(tmp_path / "tokens.json"))


def test_status_reports_an_expired_token(tmp_path):
    store = FileTokenStore(tmp_path / "tokens.json")
    expired = make_jwt({"exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())})
    store.save(TokenSet(token=expired))

    payload = auth.status(store)

    assert payload["access_token"]["expired"] is True
    assert payload["tokens_file"] == str(store.path)


def test_check_reports_a_valid_remote_session(tmp_path, monkeypatch):
    store = FileTokenStore(tmp_path / "tokens.json")
    store.save(TokenSet(token="opaque"))
    fake = FakeClient()
    install_fake(monkeypatch, fake)

    payload, valid = auth.check(store)

    assert valid is True
    assert payload["remote"] == {
        "valid": True,
        "client_code": "00000000T",
        "client_id": "C1",
        "account_id": None,
        "contact_id": None,
    }
    assert fake.closed is True


def test_check_reports_a_rejected_remote_session(tmp_path, monkeypatch):
    store = FileTokenStore(tmp_path / "tokens.json")
    store.save(TokenSet(token="opaque"))
    fake = FakeClient(info_error=AuthError())
    install_fake(monkeypatch, fake)

    payload, valid = auth.check(store)

    assert valid is False
    assert payload["remote"]["valid"] is False
    assert fake.closed is True


def test_logout_revokes_and_removes_the_local_session(tmp_path, monkeypatch):
    store = FileTokenStore(tmp_path / "tokens.json")
    store.save(TokenSet(token="opaque"))
    fake = FakeClient()
    install_fake(monkeypatch, fake)

    payload = auth.logout(store)

    assert payload["logged_out"] is True
    assert payload["remote_revoked"] is True
    assert not store.path.exists()
    assert fake.closed is True


def test_logout_removes_the_session_even_if_the_remote_call_fails(tmp_path, monkeypatch):
    store = FileTokenStore(tmp_path / "tokens.json")
    store.save(TokenSet(token="opaque"))
    fake = FakeClient(logout_error=UpstreamUnavailableError())
    install_fake(monkeypatch, fake)

    payload = auth.logout(store)

    assert payload["logged_out"] is True
    assert payload["remote_revoked"] is False
    assert not store.path.exists()
    assert fake.closed is True
