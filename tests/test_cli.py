from __future__ import annotations

from typer.testing import CliRunner

from naturgy.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip()


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("me", "supplies", "consumption", "invoices", "claim", "auth"):
        assert command in result.output


def test_missing_token_is_auth_error(tmp_path):
    result = runner.invoke(
        app, ["--tokens", str(tmp_path / "missing.json"), "me"]
    )
    assert result.exit_code == 3
    combined = result.output + (result.stderr or "")
    assert "auth_expired" in combined


def test_auth_status_missing_token_exits_3(tmp_path):
    result = runner.invoke(
        app, ["--tokens", str(tmp_path / "missing.json"), "auth", "status"]
    )
    assert result.exit_code == 3
    combined = result.output + (result.stderr or "")
    assert "auth_expired" in combined


def test_unknown_supply_is_usage(tmp_path):
    # No session: the command fails at auth first, which is exit 3, not a traceback.
    result = runner.invoke(
        app, ["--tokens", str(tmp_path / "missing.json"), "supplies", "--supply", "x"]
    )
    assert result.exit_code == 3
