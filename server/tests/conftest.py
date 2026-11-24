import os
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from server.ingest import ingest_file
from server.sources import ExportFile
from server.store import initialize_schema


@pytest.fixture(autouse=True)
def isolate_database_environment(monkeypatch):
    # Unit tests must never access the user's configured cloud database.
    # Empty overrides also prevent child CLI processes from loading real .env credentials.
    monkeypatch.setenv("SUPABASE_DB_URL", "")


@pytest.fixture(scope="session")
def postgres_url():
    configured = os.getenv("TEST_SUPABASE_DB_URL")
    if configured:
        # Tests truncate tables. Require an explicitly named test database.
        if not conninfo_to_dict(configured).get("dbname", "").endswith("_test"):
            pytest.fail("TEST_SUPABASE_DB_URL must point to a disposable database named *_test")
        yield configured
        return
    if not shutil.which("docker"):
        pytest.fail("Database tests require Docker or TEST_SUPABASE_DB_URL (see README)")
    name = f"fitness-test-{uuid.uuid4().hex[:12]}"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        subprocess.run(
            ["docker", "run", "--rm", "-d", "--name", name,
             "--network", "host",
             "--tmpfs", "/var/lib/postgresql/data",
             "-e", "POSTGRES_PASSWORD=fitness-test-only", "-e", "POSTGRES_DB=fitness_test",
             "postgres:17", "-p", str(port), "-c", "listen_addresses=127.0.0.1"],
            check=True, capture_output=True, text=True, timeout=120,
        )
        url = f"postgresql://postgres:fitness-test-only@127.0.0.1:{port}/fitness_test"
        for _ in range(60):
            try:
                with psycopg.connect(url, connect_timeout=2):
                    break
            except psycopg.OperationalError:
                time.sleep(0.5)
        else:
            pytest.fail("Disposable PostgreSQL did not become ready")
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


@pytest.fixture
def cloud(postgres_url, monkeypatch, isolate_database_environment):
    monkeypatch.setenv("SUPABASE_DB_URL", postgres_url)
    initialize_schema()
    with psycopg.connect(postgres_url) as con:
        con.execute("TRUNCATE fitness.activities, fitness.ingest_runs RESTART IDENTITY")
    return postgres_url


@pytest.fixture
def sample_zip(tmp_path):
    """Synthetic export with realistic joins; never depends on private user data."""
    db = tmp_path / "health_connect_export.db"
    start = int(datetime.fromisoformat("2026-11-22T07:13:39+05:30").timestamp() * 1000)
    end = start + 2951000
    with sqlite3.connect(db) as con:
        con.executescript("""
            CREATE TABLE application_info_table (row_id INTEGER, app_name TEXT, package_name TEXT);
            INSERT INTO application_info_table VALUES (1, 'Strava', 'com.strava');
            CREATE TABLE exercise_session_record_table (
                row_id INTEGER, app_info_id INTEGER, client_record_id TEXT,
                start_time INTEGER, end_time INTEGER, start_zone_offset INTEGER,
                exercise_type INTEGER, last_modified_time INTEGER);
            CREATE TABLE distance_record_table (
                row_id INTEGER, app_info_id INTEGER, start_time INTEGER,
                end_time INTEGER, distance REAL, last_modified_time INTEGER);
        """)
        con.execute("INSERT INTO exercise_session_record_table VALUES (1, 1, ?, ?, ?, 19800, 33, ?)",
                    ("strava-21078195405", start, end, end))
        con.execute("INSERT INTO distance_record_table VALUES (1, 1, ?, ?, 6068.2, ?)", (start, end, end))
    sample = tmp_path / "sample.zip"
    with zipfile.ZipFile(sample, "w") as archive:
        archive.write(db, db.name)
    return sample


@pytest.fixture
def ingested(tmp_path, sample_zip, cloud):
    archive = tmp_path / "archive"
    result = ingest_file(ExportFile(sample_zip, sample_zip.name), archive)
    assert result["status"] == "success"
    return archive, result
