"""Sign-in, token status and the browser-token import.

Omega has no refresh endpoint (the web app stores a refreshToken but never exchanges it), so an
expired session means signing in again. The password is only used for the request and is never
stored or logged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import OmegaClient
from .errors import (
    AuthError,
    BlockedError,
    NaturgyError,
    UpstreamUnavailableError,
    UsageError,
)
from .models import TokenSet
from .tokens import FileTokenStore, token_status


def login(
    store: FileTokenStore,
    nif: str,
    password: str,
) -> dict[str, Any]:
    nif = nif.strip()
    if not nif:
        raise UsageError("the NIF/NIE cannot be empty")
    if not password:
        raise UsageError("the password cannot be empty")

    warning: str | None = None
    with store.lock():
        client = OmegaClient()
        try:
            tokens = client.login(nif, password)
            tokens.nif = nif
            store.save(tokens)
            try:
                info = client.get_client_info()
            except AuthError:
                store.delete()
                raise
            except (BlockedError, UpstreamUnavailableError):
                info = None
                warning = "signed in, but the client profile could not be read yet"
            if info:
                _merge_client_info(tokens, info)
                store.save(tokens)
        finally:
            client.close()

    return _summary(tokens, store, warning)


def status(store: FileTokenStore, now: datetime | None = None) -> dict[str, Any]:
    tokens = store.load()
    if tokens is None:
        raise AuthError(
            f"not signed in: no token file at {store.path}; run `naturgy auth login`",
            code="missing_token",
        )
    payload = token_status(tokens, now)
    payload["tokens_file"] = str(store.path)
    return payload


def check(store: FileTokenStore) -> tuple[dict[str, Any], bool]:
    """Offline status plus one read-only call that proves the session still works."""
    payload = status(store)
    tokens = store.load()
    if tokens is None:  # status() would already have raised
        raise AuthError()
    client = OmegaClient(tokens)
    try:
        info = client.get_client_info()
        payload["remote"] = {
            "valid": True,
            "client_code": info.get("clientCode"),
            "client_id": info.get("clientId"),
        }
        return payload, True
    except AuthError:
        payload["remote"] = {
            "valid": False,
            "message": "the session was rejected; run `naturgy auth login`",
        }
        return payload, False
    finally:
        client.close()


def logout(store: FileTokenStore) -> dict[str, Any]:
    tokens = store.load()
    if tokens is None:
        return {"logged_out": False, "tokens_file": str(store.path)}
    remote_revoked = False
    client = OmegaClient(tokens)
    try:
        client.logout()
        remote_revoked = True
    except NaturgyError:
        # The local session is removed either way; a failed logout must not keep it alive.
        pass
    finally:
        client.close()
    removed = store.delete()
    return {
        "logged_out": removed,
        "remote_revoked": remote_revoked,
        "tokens_file": str(store.path),
    }


def import_tokens(store: FileTokenStore, path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot read the token export at {path}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(f"the token export at {path} is not valid JSON") from exc
    tokens = TokenSet.from_export(data)
    if tokens.nif is None:
        tokens.nif = (
            tokens.user.get("clientCode")
            if isinstance(tokens.user.get("clientCode"), str)
            else tokens.user.get("docNumber")
            if isinstance(tokens.user.get("docNumber"), str)
            else None
        )
    with store.lock():
        store.save(tokens)
    return _summary(tokens, store, None)


def _merge_client_info(tokens: TokenSet, info: dict[str, Any]) -> None:
    user = dict(tokens.user)
    if info.get("clientId"):
        user["clientId"] = info["clientId"]
    if info.get("docNumber"):
        user["docNumber"] = info["docNumber"]
    if info.get("clientCode") and not user.get("clientCode"):
        user["clientCode"] = info["clientCode"]
    tokens.user = user


def _summary(tokens: TokenSet, store: FileTokenStore, warning: str | None) -> dict[str, Any]:
    payload = token_status(tokens)
    payload["signed_in"] = True
    payload["tokens_file"] = str(store.path)
    if warning:
        payload["warning"] = warning
    return payload


def now_utc() -> datetime:
    return datetime.now(UTC)
