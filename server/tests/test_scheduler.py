from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from server.scheduler import configuration, next_run


def test_daily_schedule_and_restart_after_scheduled_time():
    zone = ZoneInfo("Asia/Kolkata")
    before = datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc)
    expected = datetime(2026, 11, 24, 0, 30, tzinfo=timezone.utc)
    assert next_run(before, time(6), zone) == expected
    assert next_run(expected, time(6), zone) == datetime(2026, 11, 25, 0, 30, tzinfo=timezone.utc)


def test_nonexistent_dst_time_is_skipped():
    now = datetime(2026, 3, 8, 0, tzinfo=timezone.utc)
    assert next_run(now, time(2, 30), ZoneInfo("America/New_York")) == datetime(
        2026, 3, 9, 6, 30, tzinfo=timezone.utc
    )


def test_repeated_dst_time_does_not_run_twice():
    zone = ZoneInfo("America/New_York")
    first = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    assert next_run(datetime(2026, 11, 1, 0, tzinfo=timezone.utc), time(1, 30), zone) == first
    assert next_run(first, time(1, 30), zone) == datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)


def test_scheduler_accepts_json_credentials_without_file(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/unused")
    monkeypatch.setenv("DRIVE_FOLDER_ID", "test-folder")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type": "service_account"}')
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("DRIVE_INGEST_TIME", "06:00")
    monkeypatch.setenv("DRIVE_INGEST_TIMEZONE", "Asia/Kolkata")
    from unittest.mock import patch

    with patch("google.oauth2.service_account.Credentials.from_service_account_info") as load:
        raw, daily, zone = configuration()
        assert (raw, daily, zone.key) == ("06:00", time(6), "Asia/Kolkata")
        load.assert_called_once()


def test_scheduler_rejects_invalid_json_without_exposing_it(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/unused")
    monkeypatch.setenv("DRIVE_FOLDER_ID", "test-folder")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "private-invalid-json")
    monkeypatch.setenv("DRIVE_INGEST_TIME", "06:00")
    monkeypatch.setenv("DRIVE_INGEST_TIMEZONE", "Asia/Kolkata")
    with pytest.raises(SystemExit, match="GOOGLE_SERVICE_ACCOUNT_JSON must be valid") as error:
        configuration()
    assert "private-invalid-json" not in str(error.value)
