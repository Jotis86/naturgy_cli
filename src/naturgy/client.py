"""HTTP client for Naturgy's Omega API.

The web app at omegav2.naturgy.es talks to api.naturgy.com with a set of `x-*` headers, not
`Authorization: Bearer`. It also sits behind a Radware-style WAF, so a browser-like User-Agent
is mandatory and an empty 200 is reported as `blocked` instead of as a parse error.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Self

import httpx

from . import config
from .errors import (
    ApiChangedError,
    AuthError,
    BlockedError,
    ClaimAlreadyOpenError,
    UpstreamUnavailableError,
    UsageError,
)
from .models import TokenSet

_MISSING = object()


class OmegaClient:
    def __init__(
        self,
        tokens: TokenSet | None = None,
        *,
        base_url: str | None = None,
        language: str | None = None,
        pacing: float | None = None,
        timeout: float | None = None,
    ) -> None:
        self.tokens = tokens
        self.language = language or config.DEFAULT_LANGUAGE
        self.origin = tokens.origin if tokens else config.DEFAULT_ORIGIN
        self._base_url = (base_url or config.BASE_URL).rstrip("/")
        self._pacing = config.PACING_SECONDS if pacing is None else pacing
        self._timeout = config.TIMEOUT_SECONDS if timeout is None else timeout
        self._last_request = 0.0
        self._pace_lock = threading.Lock()
        self._client_info: dict[str, Any] | None = None
        self._http = httpx.Client(
            base_url=self._base_url + "/",
            timeout=self._timeout,
            follow_redirects=True,
            headers=self._base_headers(),
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ requests

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = _MISSING,
        auth: bool = True,
        expect_json: bool = True,
    ) -> Any:
        self._pace()
        headers = self._auth_headers() if auth else {}
        kwargs: dict[str, Any] = {"params": params, "headers": headers}
        if json is not _MISSING:
            kwargs["json"] = json
        try:
            response = self._http.request(method, path.lstrip("/"), **kwargs)
        except httpx.TimeoutException as exc:
            raise UpstreamUnavailableError("the request to Naturgy timed out") from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError("could not reach Naturgy") from exc

        status = response.status_code
        if status == 401:
            raise AuthError(
                "Naturgy rejected the session; run `naturgy auth login`",
                http_status=status,
                code="http_401",
            )
        if status in (403, 429):
            raise BlockedError(http_status=status)
        if status >= 500:
            raise UpstreamUnavailableError(http_status=status)
        if status == 404:
            raise ApiChangedError(
                "Naturgy returned 404 for a known endpoint; the API may have changed",
                http_status=status,
            )

        if not expect_json:
            if 400 <= status:
                self._raise_for_status(status, response)
            return response

        if status == 204 or not response.content:
            if status == 204:
                return None
            raise BlockedError(
                "Naturgy returned an empty response (bot protection); wait before retrying",
                http_status=status,
            )

        try:
            data = response.json()
        except ValueError as exc:
            text = response.text[:200].lstrip().lower()
            if text.startswith("<") or "captcha" in text or "access denied" in text:
                raise BlockedError(http_status=status) from exc
            raise ApiChangedError(
                "Naturgy returned a non-JSON response; the API may have changed",
                http_status=status,
            ) from exc

        if 400 <= status:
            self._raise_for_status(status, response, data)
        return self._check_payload(data, http_status=status)

    def _raise_for_status(
        self, status: int, response: httpx.Response, data: Any = None
    ) -> None:
        if data is None:
            try:
                data = response.json()
            except ValueError:
                data = None
        if isinstance(data, dict):
            result = data.get("result")
            if isinstance(result, dict) and result.get("codResult") is not None:
                self._raise_for_result(result.get("codResult"), result.get("msgResult"))
            error = data.get("error")
            if isinstance(error, str) and "token" in error.lower():
                raise AuthError(
                    "Naturgy rejected the session; run `naturgy auth login`",
                    http_status=status,
                )
        if status == 400:
            raise UsageError("Naturgy rejected the request parameters", http_status=status)
        raise ApiChangedError(
            f"Naturgy returned HTTP {status}; the API may have changed", http_status=status
        )

    def _check_payload(self, data: Any, *, http_status: int) -> Any:
        if not isinstance(data, dict):
            return data

        error = data.get("error")
        if isinstance(error, str) and error.strip():
            if "token" in error.lower():
                raise AuthError(
                    "Naturgy rejected the session; run `naturgy auth login`",
                    http_status=http_status,
                )
            raise ApiChangedError(
                "Naturgy returned an unexpected error payload; the API may have changed",
                http_status=http_status,
            )

        result = data.get("result")
        if isinstance(result, dict) and result.get("codResult") is not None:
            self._raise_for_result(result.get("codResult"), result.get("msgResult"))
        return data

    def _raise_for_result(self, code: Any, message: Any) -> None:
        text = str(code) if code is not None else ""
        if text in ("", "0000"):
            return
        if text in ("CG12", "CG13", "CG14"):
            raise AuthError(
                "the session expired or was revoked; run `naturgy auth login`", code=text
            )
        if text == "CG09":
            raise AuthError("Naturgy rejected the NIF or password", code=text)
        if text == "CG10":
            raise AuthError(
                "the password has expired; change it in the Naturgy web app", code=text
            )
        if text == "CG08":
            raise BlockedError(
                "too many sign-in attempts; wait before retrying", code=text
            )
        if text == "0004" and "reclamaci" in str(message or "").lower():
            raise ClaimAlreadyOpenError(code=text)
        if text == "1024":
            raise UsageError("Naturgy rejected the bank-details operation", code=text)
        raise UpstreamUnavailableError(
            f"Naturgy returned an error (code {text})", code=text
        )

    def _pace(self) -> None:
        with self._pace_lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self._pacing:
                time.sleep(self._pacing - elapsed)
            self._last_request = time.monotonic()

    # ------------------------------------------------------------------ auth

    def login(self, nif: str, password: str) -> TokenSet:
        data = self.request(
            "POST",
            "authentication",
            json={"user": nif, "password": password},
            auth=False,
        )
        if not isinstance(data, dict):
            raise ApiChangedError("Naturgy's sign-in response was not a JSON object")
        auth = data.get("authResponse") if isinstance(data.get("authResponse"), dict) else data
        user = auth.get("user") if isinstance(auth.get("user"), dict) else data.get("user")
        token = auth.get("token") or data.get("token")
        id_token = auth.get("idToken") or auth.get("id_token") or data.get("idToken")
        refresh_token = (
            auth.get("refreshToken") or auth.get("refresh_token") or data.get("refreshToken")
        )
        if not isinstance(user, dict) or not isinstance(token, str) or not token.strip():
            raise ApiChangedError("Naturgy's sign-in response did not contain a session")
        tokens = TokenSet(
            token=token.strip(),
            id_token=id_token.strip() if isinstance(id_token, str) else None,
            refresh_token=refresh_token.strip() if isinstance(refresh_token, str) else None,
            user=user,
            nif=nif,
            trading_company=config.DEFAULT_TRADING_COMPANY,
            origin=self.origin,
        )
        self.tokens = tokens
        self._client_info = None
        return tokens

    def auth_headers(self) -> dict[str, str]:
        return self._auth_headers()

    def _auth_headers(self) -> dict[str, str]:
        if not self.tokens:
            raise AuthError()
        user = self.tokens.user
        info = self._client_info or {}
        role = _str(user.get("roleTypeId")) or "newcoClient"
        login_code = (
            _str(user.get("clientCode"))
            or _str(user.get("docNumber"))
            or _str(self.tokens.nif)
        )
        selected_doc = (
            _str(info.get("docNumber"))
            or _str(info.get("clientCode"))
            or login_code
        )
        client_id = _str(info.get("clientId")) or _str(user.get("clientId"))
        headers: dict[str, str] = {
            "x-access-token": self.tokens.token,
            "x-user-role": role,
            "x-trading-company": self.tokens.trading_company,
            "x-language": self.language,
            "x-origin": self.tokens.origin or self.origin,
        }
        if self.tokens.id_token:
            headers["x-id-token"] = self.tokens.id_token
        if selected_doc:
            headers["x-client-code"] = selected_doc
            headers["nif"] = selected_doc
        if login_code:
            headers["x-client-doc-number"] = login_code
        if client_id:
            headers["x-client-id"] = client_id
        if role.endswith("Agent"):
            user_id = _str(user.get("userId"))
            if user_id:
                headers["x-user-id"] = user_id
        return headers

    # ------------------------------------------------------------------ read helpers

    def get_client_info(self) -> dict[str, Any]:
        if self._client_info is not None:
            return self._client_info
        data = self.request("GET", "clients", params={"loginDate": "false"})
        if not isinstance(data, dict):
            raise ApiChangedError("Naturgy's client response was not a JSON object")
        client = data.get("client")
        if not isinstance(client, dict):
            client = data.get("clientInfo")
        if not isinstance(client, dict):
            raise ApiChangedError("Naturgy's client response did not contain a client")
        self._client_info = client
        return client

    def authorization(self) -> Any:
        return self.request("GET", "authorization")

    def logout(self) -> None:
        if not self.tokens:
            return
        self.request(
            "POST",
            "logout",
            json={"accessToken": self.tokens.token},
            expect_json=False,
        )

    def _base_headers(self) -> dict[str, str]:
        # Do not send Origin/Referer: the WAF returns an empty 200 when curl/httpx presents
        # cross-origin browser headers with a non-browser TLS fingerprint. The API does not need them.
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "User-Agent": config.USER_AGENT,
        }


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
