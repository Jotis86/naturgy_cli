from __future__ import annotations

import httpx
import pytest

from naturgy.client import OmegaClient
from naturgy.errors import (
    ApiChangedError,
    AuthError,
    BlockedError,
    ClaimAlreadyOpenError,
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
    assert headers["x-client-id"] == "C1"
    assert headers["x-language"] == "es"


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


def test_cg09_is_auth_error_with_code():
    client = make_client(
        lambda request: response(
            request, json={"result": {"codResult": "CG09", "msgResult": "no"}}
        )
    )
    with pytest.raises(AuthError) as info:
        client.request("POST", "authentication", auth=False)
    assert info.value.code == "CG09"


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


def test_login_without_session_is_api_changed():
    client = make_client(lambda request: response(request, json={"foo": "bar"}))
    with pytest.raises(ApiChangedError):
        client.login("00000000T", "secret")
