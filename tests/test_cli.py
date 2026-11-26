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
    for command in ("me", "supplies", "consumption", "invoices", "battery", "claim", "auth"):
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


def test_invalid_period_fails_before_auth(tmp_path):
    result = runner.invoke(
        app,
        [
            "--tokens",
            str(tmp_path / "missing.json"),
            "consumption",
            "--period",
            "year",
        ],
    )
    assert result.exit_code == 2
    assert "usage" in result.output


def test_invalid_family_fails_before_auth(tmp_path):
    result = runner.invoke(
        app,
        [
            "--tokens",
            str(tmp_path / "missing.json"),
            "consumption",
            "--family",
            "agua",
        ],
    )
    assert result.exit_code == 2
    assert "unknown product family" in result.output


def test_inverted_dates_fail_before_auth(tmp_path):
    result = runner.invoke(
        app,
        [
            "--tokens",
            str(tmp_path / "missing.json"),
            "consumption",
            "--from",
            "2026-02-01",
            "--to",
            "2026-01-01",
        ],
    )
    assert result.exit_code == 2
    assert "--from must be before --to" in result.output


def test_claim_without_confirmation_fails_before_auth(tmp_path):
    result = runner.invoke(
        app,
        [
            "--tokens",
            str(tmp_path / "missing.json"),
            "invoices",
            "claim",
            "latest",
            "-m",
            "No reconozco el cargo",
        ],
    )
    assert result.exit_code == 2
    assert "not sent" in result.output


def test_invalid_claim_reason_fails_before_auth(tmp_path):
    result = runner.invoke(
        app,
        [
            "--tokens",
            str(tmp_path / "missing.json"),
            "invoices",
            "claim",
            "latest",
            "-m",
            "No reconozco el cargo",
            "--reason",
            "not-a-reason",
            "--yes",
        ],
    )
    assert result.exit_code == 2
    assert "unknown claim reason" in result.output


def test_missing_attachment_fails_before_auth(tmp_path):
    result = runner.invoke(
        app,
        [
            "--tokens",
            str(tmp_path / "missing.json"),
            "invoices",
            "claim",
            "latest",
            "-m",
            "No reconozco el cargo",
            "--attach",
            str(tmp_path / "missing.png"),
            "--dry-run",
        ],
    )
    assert result.exit_code == 2
    assert "attachment not found" in result.output


def test_battery_help_lists_commands():
    result = runner.invoke(app, ["battery", "--help"])
    assert result.exit_code == 0
    for command in ("status", "history", "evolution", "assignments"):
        assert command in result.output


def test_battery_invalid_page_fails_before_auth(tmp_path):
    result = runner.invoke(
        app,
        [
            "--tokens",
            str(tmp_path / "missing.json"),
            "battery",
            "history",
            "--page",
            "0",
        ],
    )
    assert result.exit_code == 2
    assert "--page" in result.output


def test_battery_invalid_limit_fails_before_auth(tmp_path):
    result = runner.invoke(
        app,
        [
            "--tokens",
            str(tmp_path / "missing.json"),
            "battery",
            "history",
            "--limit",
            "0",
        ],
    )
    assert result.exit_code == 2
    assert "--limit" in result.output


def test_battery_invalid_year_fails_before_auth(tmp_path):
    result = runner.invoke(
        app,
        [
            "--tokens",
            str(tmp_path / "missing.json"),
            "battery",
            "evolution",
            "--year",
            "1800",
        ],
    )
    assert result.exit_code == 2
    assert "--year" in result.output


def test_battery_status_without_a_session_exits_3(tmp_path):
    result = runner.invoke(
        app,
        ["--tokens", str(tmp_path / "missing.json"), "battery", "status"],
    )
    assert result.exit_code == 3
