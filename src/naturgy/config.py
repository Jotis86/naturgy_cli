"""Hosts, browser-ish headers and the token-file lookup."""

from __future__ import annotations

import os
from pathlib import Path

TOKENS_FILE_ENV = "NATURGY_TOKENS_FILE"

OMEGA_URL = os.environ.get("NATURGY_OMEGA_URL", "https://omega.naturgy.es")
BASE_URL = os.environ.get(
    "NATURGY_BASE_URL", "https://api.naturgy.com/ntgy-fua-e-fua/fua/api"
).rstrip("/")
NEWCO_URL = os.environ.get(
    "NATURGY_NEWCO_URL", "https://ntgy-newco-e-retdir.de-c1.cloudhub.io/api"
).rstrip("/")

DEFAULT_LANGUAGE = os.environ.get("NATURGY_LANGUAGE", "es")
DEFAULT_ORIGIN = os.environ.get("NATURGY_ORIGIN", "AC")
DEFAULT_TRADING_COMPANY = os.environ.get("NATURGY_TRADING_COMPANY", "newco")

# The web app sends its requests one at a time and a burst can trip the WAF. Keep a small gap.
PACING_SECONDS = float(os.environ.get("NATURGY_PACING", "0.6"))

TIMEOUT_SECONDS = float(os.environ.get("NATURGY_TIMEOUT", "45"))

USER_AGENT = os.environ.get(
    "NATURGY_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

CLAIM_MAX_LENGTH = 4000
CLAIM_MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024
CLAIM_MAX_TOTAL_ATTACHMENT_BYTES = 10 * 1024 * 1024
CLAIM_ATTACHMENT_SUFFIXES = (".png", ".jpg", ".jpeg", ".pdf")


def default_tokens_path() -> Path:
    """--tokens, then $NATURGY_TOKENS_FILE, then an existing session, then a checkout, then ~/.config.

    The existing-session check comes before the checkout fallback so that a global `naturgy auth
    login` (which writes ~/.config) is visible when the probe or tests run from a source checkout.
    """
    env = os.environ.get(TOKENS_FILE_ENV)
    if env:
        return Path(env).expanduser()

    cwd = Path.cwd()
    local = cwd / ".local-secrets" / "tokens.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    installed = base / "naturgy-cli" / "tokens.json"

    if local.is_file():
        return local
    if installed.is_file():
        return installed

    checkout = (cwd / "pyproject.toml").is_file() and (cwd / "src" / "naturgy").is_dir()
    return local if checkout else installed
