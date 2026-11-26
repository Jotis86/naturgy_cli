"""Typed errors and the CLI's exit codes.

Errors are rendered to stderr as {"error": {...}} and never echo values the server sent back
(credentials, tokens, query values such as the NIF) nor raw response headers.
"""

from __future__ import annotations

from typing import Any

EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_UPSTREAM = 5
EXIT_API_CHANGED = 7


class NaturgyError(Exception):
    error_type = "internal"
    exit_code = EXIT_INTERNAL

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        code: str | int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.code = str(code) if code is not None else None
        self.extra = extra or {}

    def to_payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "type": self.error_type,
            "message": self.message,
            "exit_code": self.exit_code,
        }
        if self.http_status is not None:
            error["http_status"] = self.http_status
        if self.code is not None:
            error["code"] = self.code
        error.update(self.extra)
        return {"error": error}


class UsageError(NaturgyError):
    error_type = "usage"
    exit_code = EXIT_USAGE


class CancelledError(NaturgyError):
    error_type = "cancelled"
    exit_code = EXIT_USAGE


class AuthError(NaturgyError):
    error_type = "auth_expired"
    exit_code = EXIT_AUTH

    def __init__(self, message: str = "not signed in: run `naturgy auth login`", **kwargs: Any) -> None:
        super().__init__(message, **kwargs)


class LoginError(NaturgyError):
    error_type = "login_failed"
    exit_code = EXIT_AUTH

    def __init__(
        self, message: str = "Naturgy rejected the sign-in; check the NIF/NIE and password", **kwargs: Any
    ) -> None:
        super().__init__(message, **kwargs)


class AccessDeniedError(NaturgyError):
    error_type = "access_denied"
    exit_code = EXIT_AUTH

    def __init__(self, message: str = "this account or supply was refused, not the session", **kwargs: Any) -> None:
        super().__init__(message, **kwargs)


class BlockedError(NaturgyError):
    error_type = "blocked"
    exit_code = EXIT_UPSTREAM

    def __init__(
        self,
        message: str = "Naturgy blocked the request (bot protection or rate limit); wait before retrying",
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)


class UpstreamUnavailableError(NaturgyError):
    error_type = "upstream_unavailable"
    exit_code = EXIT_UPSTREAM

    def __init__(
        self,
        message: str = "Naturgy is unavailable, timed out or returned a server error; try again later",
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)


class ClaimAlreadyOpenError(NaturgyError):
    error_type = "claim_already_open"
    exit_code = EXIT_UPSTREAM

    def __init__(
        self,
        message: str = "this invoice's account already has an open claim; nothing was sent",
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)


class ClaimCheckFailedError(NaturgyError):
    error_type = "claim_check_failed"
    exit_code = EXIT_UPSTREAM

    def __init__(
        self,
        message: str = "could not check whether a claim is already open; nothing was sent",
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)


class OutcomeUnknownError(NaturgyError):
    error_type = "outcome_unknown"
    exit_code = EXIT_UPSTREAM

    def __init__(
        self,
        message: str = "the claim may or may not have been filed; check `naturgy cases` before retrying",
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)


class ApiChangedError(NaturgyError):
    error_type = "api_changed"
    exit_code = EXIT_API_CHANGED

    def __init__(
        self,
        message: str = "unexpected response from Naturgy; the API may have changed",
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)


class InternalError(NaturgyError):
    error_type = "internal"
    exit_code = EXIT_INTERNAL

    def __init__(self, message: str = "unexpected internal error", **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
