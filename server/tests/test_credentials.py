import json
from unittest.mock import patch

import pytest

from server.sources import DriveSource


def test_json_credentials_take_precedence(monkeypatch):
    info = {"type": "service_account", "private_key": "line one\nline two\n"}
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(info))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/unused.json")
    with patch("google.oauth2.service_account.Credentials.from_service_account_info") as load:
        assert DriveSource.credentials() is load.return_value
        load.assert_called_once_with(info, scopes=["https://www.googleapis.com/auth/drive.readonly"])


@pytest.mark.parametrize("raw", ["private-secret-not-json", "[]", "null"])
def test_invalid_json_does_not_leak_secret_or_fall_back(monkeypatch, raw):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", raw)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/unused.json")
    with pytest.raises(ValueError, match="must be valid service-account JSON") as error:
        DriveSource.credentials()
    assert raw not in str(error.value)


def test_file_credentials_fallback(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/credentials.json")
    with patch("google.oauth2.service_account.Credentials.from_service_account_file") as load:
        assert DriveSource.credentials() is load.return_value
        load.assert_called_once_with("/credentials.json", scopes=["https://www.googleapis.com/auth/drive.readonly"])


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    with pytest.raises(ValueError, match="Set GOOGLE_SERVICE_ACCOUNT_JSON"):
        DriveSource.credentials()
