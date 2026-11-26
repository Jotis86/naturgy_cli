"""The on-disk token set: atomic writes, mode 0600, and a lock for concurrent commands."""

from __future__ import annotations

import base64
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import AuthError, UsageError
from .models import TokenSet


class FileTokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> TokenSet | None:
        if not self.path.exists():
            return None
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AuthError(f"cannot read the token file at {self.path}") from exc
        if not text.strip():
            raise AuthError(f"the token file at {self.path} is empty")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AuthError(f"the token file at {self.path} is not valid JSON") from exc
        return TokenSet.from_export(data)

    def save(self, tokens: TokenSet) -> None:
        self._ensure_parent()
        payload = tokens.model_dump(mode="json")
        fd, temporary = tempfile.mkstemp(prefix=".naturgy-tokens-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def delete(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True

    def _ensure_parent(self) -> None:
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise UsageError(f"cannot create the token folder at {parent}") from exc

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Serialize sign-in and token writes. A process that waited uses the file the other one saved."""
        self._ensure_parent()
        lock_path = self.path.with_name(self.path.name + ".lock")
        try:
            import fcntl
        except ImportError:  # pragma: no cover - macOS/Linux are the supported platforms
            yield
            return
        try:
            handle = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115 - closed by `with handle`
        except OSError as exc:
            raise UsageError(f"cannot create the token lock at {lock_path}") from exc
        with handle:
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def jwt_exp(token: str | None) -> datetime | None:
    """The `exp` claim, without verifying the signature. Tokens are opaque to us."""
    if not token or token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(exp), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def token_status(tokens: TokenSet, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    expires_at = jwt_exp(tokens.id_token) or jwt_exp(tokens.token)
    if expires_at is None:
        access = {
            "present": True,
            "expires_at": None,
            "expired": None,
            "seconds_remaining": None,
            "note": "the token is not a readable JWT; use `naturgy auth status --check`",
        }
    else:
        seconds = int((expires_at - now).total_seconds())
        access = {
            "present": True,
            "expires_at": expires_at.isoformat(),
            "expired": seconds <= 0,
            "seconds_remaining": max(0, seconds),
        }
    return {
        **tokens.summary(),
        "access_token": access,
        "refresh_token": {
            "present": bool(tokens.refresh_token),
            "usable": False,
            "note": "Naturgy's Omega API exposes no refresh endpoint; sign in again when the token expires",
        },
    }
