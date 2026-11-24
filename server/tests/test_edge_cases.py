import sqlite3
import subprocess
import sys
import threading
import zipfile

import pytest

from server.analytics import metrics
from server.normalize import normalize
from server.parse import parse_export
from server.scheduler import wait_for_ingest


@pytest.mark.parametrize("distance", [float("nan"), float("inf"), -1])
def test_invalid_distance_rejected(sample_zip, distance):
    row = next(parse_export(sample_zip))
    row["distance_m"] = distance
    with pytest.raises(ValueError, match="finite and non-negative"):
        normalize(row)


def test_rates_exclude_missing_zero_distance_and_zero_duration():
    rows = [
        {"distance_m": 1000, "duration_s": 300},
        {"distance_m": None, "duration_s": 900},
        {"distance_m": 0, "duration_s": 900},
        {"distance_m": 1000, "duration_s": 0},
    ]
    result = metrics(rows)
    assert result["count"] == 4
    assert result["total_km"] == 2
    assert result["total_time_s"] == 2100
    assert result["average_pace_min_per_km"] == 5
    assert result["average_speed_kmh"] == 12


def test_latest_exact_distance_join(sample_zip, tmp_path):
    with zipfile.ZipFile(sample_zip) as archive:
        archive.extract("health_connect_export.db", tmp_path)
    db = tmp_path / "health_connect_export.db"
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO distance_record_table SELECT 2, app_info_id, start_time, end_time, 7000, last_modified_time + 1 FROM distance_record_table")
        con.execute("INSERT INTO distance_record_table SELECT 3, 99, start_time, end_time, 9999, last_modified_time + 2 FROM distance_record_table WHERE row_id = 1")
    updated = tmp_path / "updated.zip"
    with zipfile.ZipFile(updated, "w") as archive:
        archive.write(db, db.name)
    rows = list(parse_export(updated))
    assert len(rows) == 1
    assert rows[0]["distance_m"] == 7000


def test_oversized_database_rejected_before_extraction(sample_zip, monkeypatch):
    monkeypatch.setattr("server.parse.MAX_DATABASE_BYTES", 1)
    with pytest.raises(ValueError, match="extraction limit"):
        list(parse_export(sample_zip))


def test_stopped_scheduler_reaps_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    stopped = threading.Event()
    stopped.set()
    try:
        assert wait_for_ingest(child, stopped, 30) == 1
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
