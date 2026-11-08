import pytest

from tradequery.settings import CredentialsError, load_credentials, read_env_file


def test_env_file_is_parsed(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "﻿# comment\nMT5_LOGIN=12345\nMT5_INVESTOR_PASSWORD=\"pa ss=word\"\n\nMT5_SERVER = Srv-Live \n",
        encoding="utf-8",
    )
    assert read_env_file(path) == {
        "MT5_LOGIN": "12345",
        "MT5_INVESTOR_PASSWORD": "pa ss=word",
        "MT5_SERVER": "Srv-Live",
    }
    assert read_env_file(tmp_path / "missing.env") == {}


def test_credentials_load_and_environment_overrides_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("MT5_LOGIN=1\nMT5_INVESTOR_PASSWORD=pw\nMT5_SERVER=s\n")
    for key in ("MT5_LOGIN", "MT5_INVESTOR_PASSWORD", "MT5_SERVER", "MT5_PATH"):
        monkeypatch.delenv(key, raising=False)
    creds = load_credentials(path)
    assert (creds.login, creds.password, creds.server, creds.path) == (1, "pw", "s", None)
    monkeypatch.setenv("MT5_LOGIN", "99")
    assert load_credentials(path).login == 99


def test_missing_or_bad_credentials_are_explained(tmp_path, monkeypatch):
    for key in ("MT5_LOGIN", "MT5_INVESTOR_PASSWORD", "MT5_SERVER", "MT5_PATH"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text("MT5_LOGIN=abc\nMT5_SERVER=s\n")
    with pytest.raises(CredentialsError, match="MT5_INVESTOR_PASSWORD"):
        load_credentials(path)
    path.write_text("MT5_LOGIN=abc\nMT5_INVESTOR_PASSWORD=pw\nMT5_SERVER=s\n")
    with pytest.raises(CredentialsError, match="digits"):
        load_credentials(path)

