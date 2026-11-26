"""JSON-first CLI for your own Naturgy España account (Omega)."""

from importlib.metadata import PackageNotFoundError, version


def package_version() -> str | None:
    try:
        return version("naturgy-cli")
    except PackageNotFoundError:
        return None
