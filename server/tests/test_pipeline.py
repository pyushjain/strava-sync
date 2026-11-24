import hashlib
import shutil
import zipfile
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from server.analytics import Analytics, SOURCE_LIMITATION
from server.ingest import ingest_file
from server.normalize import normalize
from server.parse import parse_export
from server.sources import ExportFile
from server.store import connect, upsert


def test_sample_activity(ingested):
    archive, result = ingested
    assert result["rows_seen"] == result["rows_new"] == 1
    assert len(list(archive.glob("*.zip"))) == 1
    activity = Analytics().get_activity("21078195405")["activity"]
    assert activity["distance_m"] == pytest.approx(6068.2)
    assert activity["duration_s"] == 2951
    assert activity["start_local"] == "2026-11-22T07:13:39+05:30"
    assert activity["source_id"] == "21078195405"
    assert activity["exercise_type_code"] == 33
    assert activity["strava_url"] == "https://www.strava.com/activities/21078195405"


def test_checksum_skip(ingested, sample_zip):
    archive, _ = ingested
    result = ingest_file(ExportFile(sample_zip, "renamed.zip"), archive)
    assert result["status"] == "skipped"
    assert result["rows_new"] == result["rows_updated"] == 0
    assert Analytics().get_data_status()["total_activities"] == 1


def test_new_snapshot_same_records(ingested, sample_zip, tmp_path):
    archive, _ = ingested
    snapshot = tmp_path / "next-day.zip"
    shutil.copyfile(sample_zip, snapshot)
    with zipfile.ZipFile(snapshot, "a") as z:
        z.comment = b"Different ZIP, same full snapshot"
    result = ingest_file(ExportFile(snapshot, snapshot.name), archive)
    assert result["status"] == "success"
    assert result["rows_seen"] == 1
    assert result["rows_new"] == result["rows_updated"] == 0


def test_newer_only_upsert(ingested, sample_zip):
    row = normalize(next(parse_export(sample_zip)))
    con = connect()
    try:
        with con:
            assert upsert(con, row) == "skipped"
            row["last_modified_utc"] = "2090-01-01T00:00:00.000+00:00"
            row["distance_m"] = 7000
            assert upsert(con, row) == "updated"
            row["last_modified_utc"] = "2020-01-01T00:00:00.000+00:00"
            row["distance_m"] = 100
            assert upsert(con, row) == "skipped"
    finally:
        con.close()
    assert Analytics().get_activity(row["source_id"])["activity"]["distance_m"] == 7000


def test_fallback_identity(sample_zip):
    row = next(parse_export(sample_zip))
    row["client_record_id"] = "another-app-id"
    identity = f"{row['app_package']}|{row['start_time']}|{row['end_time']}|{row['exercise_type']}"
    assert normalize(row)["source_id"] == hashlib.sha1(identity.encode()).hexdigest()


def test_weekly_summary(ingested):
    analytics = Analytics()
    week = analytics.get_weekly_summary("2026-11-27")
    assert week["data_available"] is True
    assert week["count"] == 1
    assert week["total_km"] == pytest.approx(6.0682)
    assert week["total_time_s"] == 2951
    assert week["average_pace_min_per_km"] == pytest.approx(8.105, abs=0.001)
    assert week["activities"][0]["pace_formatted"] == "8:06/km"
    assert week["days"][0]["status"] == "no activity recorded"
    assert analytics.get_weekly_summary("2026-11-20")["data_available"] is False
    assert SOURCE_LIMITATION in week["limitations"]


def test_status(ingested):
    status = Analytics().get_data_status()
    assert status["total_activities"] == 1
    assert status["dates_with_data"] == ["2026-11-22"]
    assert status["newest_activity_date"] == "2026-11-22"
    assert status["last_ingest_utc"] is not None
    assert "no activity recorded" in status["coverage"]


def test_comparisons(ingested, sample_zip):
    assert Analytics().compare_weeks("2026-11-27")["prior_week_deltas"]["count"]["absolute"] is None
    row = normalize(next(parse_export(sample_zip)))
    row["source_id"] = "123"
    for key in ("start_utc", "end_utc"):
        row[key] = (datetime.fromisoformat(row[key]) - timedelta(weeks=1)).isoformat()
    con = connect()
    try:
        with con:
            upsert(con, row)
    finally:
        con.close()
    comparison = Analytics().compare_weeks("2026-11-27")
    assert comparison["prior_week_deltas"]["total_km"] == {"absolute": 0, "percent": 0}
    assert comparison["prior_weeks_average"]["count"] == 0.25
    assert comparison["prior_average_deltas"]["count"] == {"absolute": 0.75, "percent": 300}


def test_week_boundaries(ingested, sample_zip):
    template = normalize(next(parse_export(sample_zip)))
    con = connect()
    try:
        with con:
            for index, instant in enumerate(("2026-11-20T18:29:59.999+00:00", "2026-11-20T18:30:00+00:00",
                                              "2026-11-27T18:29:59.999+00:00", "2026-11-27T18:30:00+00:00")):
                upsert(con, {**template, "source_id": str(index), "start_utc": instant})
    finally:
        con.close()
    ids = {a["source_id"] for a in Analytics().get_weekly_summary("2026-11-27")["activities"]}
    assert ids == {"1", "2", "21078195405"}


def test_staleness(ingested):
    con = connect()
    try:
        with con:
            old = (datetime.now(timezone.utc) - timedelta(hours=37)).isoformat()
            con.execute("UPDATE ingest_runs SET completed_at=%s", (old,))
    finally:
        con.close()
    assert any("Staleness warning" in s for s in Analytics().get_data_status()["limitations"])


def test_empty_database(cloud):
    empty = Analytics().get_data_status()
    assert empty["data_available"] is False
    assert empty["last_ingest_utc"] is None


def test_bad_file_recorded(tmp_path, cloud):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    result = ingest_file(ExportFile(bad, bad.name), tmp_path / "archive")
    assert result["status"] == "failed"
    assert Analytics().get_data_status()["total_activities"] == 0


def test_readonly_connection(ingested):
    con = connect(readonly=True)
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            con.execute("DELETE FROM activities")
    finally:
        con.close()


def test_input_validation(ingested):
    analytics = Analytics()
    with pytest.raises(ValueError):
        analytics.get_weekly_summary("2026-11-22")
    with pytest.raises(ValueError):
        analytics.compare_weeks(prior_weeks=0)
    with pytest.raises(ValueError):
        analytics.list_activities("2026-11-23", "2026-11-22")
