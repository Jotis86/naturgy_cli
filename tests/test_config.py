from __future__ import annotations

from naturgy import config


def test_env_wins(monkeypatch, tmp_path):
    target = tmp_path / "custom.json"
    monkeypatch.setenv(config.TOKENS_FILE_ENV, str(target))
    assert config.default_tokens_path() == target


def test_checkout_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv(config.TOKENS_FILE_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "src" / "naturgy").mkdir(parents=True)
    assert config.default_tokens_path() == tmp_path / ".local-secrets" / "tokens.json"


def test_existing_installed_session_beats_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv(config.TOKENS_FILE_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    installed = tmp_path / "xdg" / "naturgy-cli" / "tokens.json"
    installed.parent.mkdir(parents=True)
    installed.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "src" / "naturgy").mkdir(parents=True)
    assert config.default_tokens_path() == installed


def test_existing_local_session_wins(monkeypatch, tmp_path):
    monkeypatch.delenv(config.TOKENS_FILE_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    local = tmp_path / ".local-secrets" / "tokens.json"
    local.parent.mkdir(parents=True)
    local.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert config.default_tokens_path() == local
