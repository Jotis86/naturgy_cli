from __future__ import annotations

import json
import re

import httpx
import pytest

from naturgy.client import OmegaClient
from naturgy.errors import (
    ApiChangedError,
    AuthError,
    BlockedError,
    ClaimAlreadyOpenError,
    LoginError,
    UpstreamUnavailableError,
)
from naturgy.models import TokenSet


def make_client(handler, *, tokens: TokenSet | None = None) -> OmegaClient:
    client = OmegaClient(tokens or TokenSet(token="access"), pacing=0, base_url="https://api.test")
    client._http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.test/",
    )
    return client


def response(request: httpx.Request, status: int = 200, **kwargs) -> httpx.Response:
    return httpx.Response(status, request=request, **kwargs)


def test_auth_headers_match_the_web_app():
    tokens = TokenSet(
        token="access",
        id_token="id",
        user={"clientCode": "00000000T", "clientId": "C1", "roleTypeId": "newcoClient"},
        nif="00000000T",
        account_id="ACC1",
        contact_id="CON1",
    )
    client = make_client(lambda request: response(request, json={}), tokens=tokens)
    client._client_info = {"docNumber": "00000000T", "clientId": "C1"}
    headers = client.auth_headers()
    assert headers["x-access-token"] == "access"
    assert headers["x-id-token"] == "id"
    assert headers["x-user-role"] == "newcoClient"
    assert headers["x-trading-company"] == "newco"
    assert headers["x-client-code"] == "00000000T"
    assert headers["x-client-doc-number"] == "00000000T"
    assert headers["nif"] == "00000000T"
    assert headers["x-client-id"] == "ACC1"
    assert headers["x-contact-id"] == "CON1"
    assert headers["x-language"] == "es"


def test_traceparent_header_is_valid():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return response(request, json={})

    client = make_client(handler)
    client.request("GET", "clients")
    assert re.fullmatch(
        r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", captured["traceparent"]
    )


def test_fetch_identity_sets_account_and_contact():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/authorization")
        return response(
            request,
            json={
                "authorization": [
                    {"accountid": "ACC1", "contactid": "CON1", "docnumber": "00000000T"}
                ]
            },
        )

    client = make_client(handler)
    result = client.fetch_identity()
    assert result == {"account_id": "ACC1", "contact_id": "CON1"}
    assert client.tokens
    assert client.tokens.account_id == "ACC1"
    assert client.tokens.contact_id == "CON1"
    assert client.auth_headers()["x-client-id"] == "ACC1"
    assert client.auth_headers()["x-contact-id"] == "CON1"


def test_ensure_identity_skips_request_when_present():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not request /authorization")

    client = make_client(
        handler,
        tokens=TokenSet(token="x", account_id="ACC1", contact_id="CON1"),
    )
    assert client.ensure_identity() is False


def test_base_headers_omit_origin_and_referer():
    # The WAF answered an empty 200 when Origin/Referer were present with httpx's fingerprint.
    client = OmegaClient(TokenSet(token="x"), pacing=0, base_url="https://api.test")
    try:
        assert "origin" not in client._http.headers
        assert "referer" not in client._http.headers
        assert client._http.headers["user-agent"].startswith("Mozilla/5.0")
    finally:
        client.close()


def test_empty_200_is_blocked():
    client = make_client(lambda request: response(request, content=b""))
    with pytest.raises(BlockedError):
        client.request("GET", "clients")


def test_invalid_token_json_is_auth_error():
    client = make_client(lambda request: response(request, json={"error": "Invalid token."}))
    with pytest.raises(AuthError):
        client.request("GET", "clients")


def test_cg09_is_login_error_with_code():
    client = make_client(
        lambda request: response(
            request, json={"result": {"codResult": "CG09", "msgResult": "no"}}
        )
    )
    with pytest.raises(LoginError) as info:
        client.request("POST", "authentication", auth=False)
    assert info.value.code == "CG09"


def test_ps01_is_unknown_user_login_error():
    client = make_client(
        lambda request: response(request, json={"result": {"codResult": "PS01"}})
    )
    with pytest.raises(LoginError) as info:
        client.request("POST", "authentication", auth=False)
    assert info.value.code == "PS01"
    assert "does not recognize" in info.value.message


def test_ps04_is_login_error():
    client = make_client(
        lambda request: response(request, json={"result": {"codResult": "PS04"}})
    )
    with pytest.raises(LoginError) as info:
        client.request("POST", "authentication", auth=False)
    assert info.value.code == "PS04"


def test_cg14_is_auth_error():
    client = make_client(
        lambda request: response(request, json={"result": {"codResult": "CG14"}})
    )
    with pytest.raises(AuthError):
        client.request("GET", "clients")


def test_open_claim_code():
    client = make_client(
        lambda request: response(
            request,
            json={
                "result": {
                    "codResult": "0004",
                    "msgResult": "Existen reclamaciones abiertas para esta factura",
                }
            },
        )
    )
    with pytest.raises(ClaimAlreadyOpenError):
        client.request("POST", "cases", json={})


def test_500_is_upstream():
    client = make_client(lambda request: response(request, status=500, text="boom"))
    with pytest.raises(UpstreamUnavailableError):
        client.request("GET", "clients")


def test_login_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/authentication")
        return response(
            request,
            json={
                "authResponse": {
                    "user": {"clientCode": "00000000T", "roleTypeId": "newcoClient"},
                    "token": "access",
                    "idToken": "id",
                    "refreshToken": "refresh",
                }
            },
        )

    client = make_client(handler)
    tokens = client.login("00000000T", "secret")
    assert tokens.token == "access"
    assert tokens.id_token == "id"
    assert tokens.refresh_token == "refresh"
    assert tokens.user["clientCode"] == "00000000T"


def test_login_normalizes_user_and_password_like_the_web_app():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return response(
            request,
            json={
                "authResponse": {
                    "user": {"clientCode": "12345678Z"},
                    "token": "access",
                }
            },
        )

    client = make_client(handler)
    tokens = client.login(" 12.345.678-z ", " pass word ")
    assert captured["user"] == "12345678Z"
    assert captured["password"] == "password"
    assert tokens.nif == "12345678Z"


def test_login_400_is_login_error_with_safe_keys():
    client = make_client(
        lambda request: response(
            request, status=400, json={"message": "bad request", "field": "password"}
        )
    )
    with pytest.raises(LoginError) as info:
        client.login("12345678Z", "wrong")
    assert info.value.http_status == 400
    assert info.value.extra["response_keys"] == ["field", "message"]


def test_login_without_session_is_api_changed():
    client = make_client(lambda request: response(request, json={"foo": "bar"}))
    with pytest.raises(ApiChangedError):
        client.login("00000000T", "secret")
