import os
import subprocess
import sys

import psycopg
import pytest

from server.analytics import Analytics
from server.ingest import ingest_file, ingest_source
from server.parse import parse_export
from server.sources import ExportFile
from server.store import connect, initialize_schema, load_environment


def test_schema_setup_preserves_data(ingested):
    before = Analytics().get_data_status()
    initialize_schema()
    assert Analytics().get_data_status() == before


def test_failed_batch_rolls_back(cloud, sample_zip, tmp_path, monkeypatch):
    row = next(parse_export(sample_zip))

    def broken_export(path):
        yield row
        raise ValueError("bad record after valid record")

    monkeypatch.setattr("server.ingest.parse_export", broken_export)
    result = ingest_file(ExportFile(sample_zip, sample_zip.name), tmp_path / "archive")
    assert result["status"] == "failed"
    assert result["rows_new"] == 0
    assert Analytics().get_data_status()["total_activities"] == 0
    with connect(readonly=True) as con:
        assert con.execute("SELECT status FROM ingest_runs").fetchone()["status"] == "failed"


def test_source_versions_are_loaded_from_supabase(cloud, sample_zip, tmp_path):
    ingest_file(ExportFile(sample_zip, "drive-file-id", "drive-version"), tmp_path / "archive")

    class AlreadySeenSource:
        def files(self, seen_versions, download_dir):
            assert seen_versions == {"drive-version"}
            return iter([])

    assert ingest_source(AlreadySeenSource(), tmp_path / "archive") == []


def test_missing_configuration_is_an_error():
    with pytest.raises(ValueError, match="SUPABASE_DB_URL is required"):
        Analytics().get_data_status()
    with pytest.raises(ValueError, match="SUPABASE_DB_URL is required"):
        connect()


def test_connection_failure_is_not_empty_data(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://localhost/fitness_test?host=/nonexistent-fitness-socket")
    with pytest.raises(psycopg.OperationalError):
        Analytics().get_data_status()


def test_dotenv_loading_preserves_shell_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SUPABASE_DB_URL=from-file\nMCP_PORT=9000\n")
    monkeypatch.delenv("SUPABASE_DB_URL")
    monkeypatch.setenv("MCP_PORT", "8001")
    load_environment()
    assert os.environ["SUPABASE_DB_URL"] == "from-file"
    assert os.environ["MCP_PORT"] == "8001"


def test_cli_loads_connection_from_dotenv(cloud, tmp_path):
    (tmp_path / ".env").write_text(f"SUPABASE_DB_URL='{cloud}'\n")
    env = dict(os.environ)
    env.pop("SUPABASE_DB_URL", None)
    result = subprocess.run([sys.executable, "-m", "server.store"], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert "Strava Sync database is ready" in result.stdout
