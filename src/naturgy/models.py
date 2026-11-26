"""Token-set model and the small helpers shared by the CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .errors import AuthError


class TokenSet(BaseModel):
    """The session the CLI keeps on disk. Never contains the password."""

    model_config = ConfigDict(extra="ignore")

    token: str
    id_token: str | None = None
    refresh_token: str | None = None
    user: dict[str, Any] = Field(default_factory=dict)
    nif: str | None = None
    account_id: str | None = None
    contact_id: str | None = None
    trading_company: str = "newco"
    origin: str = "AC"
    saved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_export(cls, data: Any) -> TokenSet:
        """Accept our own file, the browser's sessionStorage `user` JSON, or an
        `authResponse` wrapper. The browser export is what a user can copy without
        typing the password into the CLI."""
        if not isinstance(data, dict):
            raise AuthError("the token file is not a JSON object")

        if isinstance(data.get("authResponse"), dict):
            data = data["authResponse"]

        user = data.get("user") or data.get("authUser") or {}
        if isinstance(user, str):
            try:
                user = json.loads(user)
            except json.JSONDecodeError as exc:
                raise AuthError("the token file's user field is not valid JSON") from exc
        if not isinstance(user, dict):
            user = {}

        token = _first_str(data, "token", "accessToken", "access_token")
        id_token = _first_str(data, "idToken", "id_token")
        refresh_token = _first_str(data, "refreshToken", "refresh_token")

        if not token:
            raise AuthError("the token file does not contain an access token")

        # A raw sessionStorage export keeps the user fields at the top level.
        if not user:
            user = {
                key: data[key]
                for key in (
                    "userId",
                    "clientId",
                    "clientCode",
                    "roleTypeId",
                    "docNumber",
                    "uName",
                    "cOrigin",
                )
                if data.get(key) not in (None, "")
            }

        saved_at = _parse_datetime(data.get("savedAt") or data.get("saved_at")) or datetime.now(UTC)
        account_id = (
            _first_str(data, "accountId", "account_id")
            or _first_str(user, "accountId")
            or _first_str(user, "clientId")
        )
        contact_id = _first_str(data, "contactId", "contact_id") or _first_str(user, "contactId")
        return cls(
            token=token,
            id_token=id_token,
            refresh_token=refresh_token,
            user=user,
            nif=_first_str(data, "nif", "docNumber", "documentNumber"),
            account_id=account_id,
            contact_id=contact_id,
            trading_company=_first_str(data, "tradingCompany", "trading_company") or "newco",
            origin=_first_str(data, "origin") or "AC",
            saved_at=saved_at,
        )

    def summary(self) -> dict[str, Any]:
        """What is safe to print: identities and expiry, never token values."""
        user = self.user
        return {
            "client_code": user.get("clientCode") or self.nif,
            "client_id": user.get("clientId"),
            "account_id": self.account_id or user.get("clientId"),
            "contact_id": self.contact_id or user.get("contactId"),
            "user_id": user.get("userId"),
            "role_type_id": user.get("roleTypeId"),
            "nif": self.nif,
            "trading_company": self.trading_company,
            "origin": self.origin,
            "saved_at": self.saved_at.isoformat(),
        }


def _first_str(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
