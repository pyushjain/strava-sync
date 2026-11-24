import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from .normalize import normalize
from .parse import parse_export
from .sources import DriveSource, ExportFile, LocalFolderSource, Source
from .store import begin_ingest, connect, load_environment, supabase_url, utc_now


def ingest_file(export: ExportFile, archive: Path = Path("archive")) -> dict:
    from .store import upsert

    archive.mkdir(parents=True, exist_ok=True)
    # Snapshot the input before hashing/parsing (daily exporters may replace it).
    with tempfile.TemporaryDirectory(dir=archive) as tmp:
        snapshot = Path(tmp) / "export.zip"
        shutil.copyfile(export.path, snapshot)
        with snapshot.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        archived = archive / f"{checksum}.zip"
        snapshot.replace(archived)
    con = connect()
    result = {"file": export.file_id_or_name, "rows_seen": 0, "rows_new": 0,
              "rows_updated": 0, "status": "success", "error": None}
    try:
        # Serialize the checksum check and all activity writes across ingesters.
        begin_ingest(con)
        duplicate = con.execute(
            "SELECT 1 FROM ingest_runs WHERE checksum = %s AND status = 'success'",
            (checksum,),
        ).fetchone()
        run_id = con.execute(
            "INSERT INTO ingest_runs(started_at, file_id_or_name, checksum, source_version, status) "
            "VALUES (%s, %s, %s, %s, 'running') RETURNING id",
            (utc_now(), export.file_id_or_name, checksum, export.version),
        ).fetchone()["id"]
        con.execute("SAVEPOINT activities_batch")
        try:
            if duplicate:
                result["status"] = "skipped"
            else:
                for row in parse_export(archived):
                    result["rows_seen"] += 1
                    action = upsert(con, normalize(row))
                    if action in ("new", "updated"):
                        result[f"rows_{action}"] += 1
        except Exception as exc:
            con.execute("ROLLBACK TO activities_batch")
            result.update(status="failed", rows_new=0, rows_updated=0,
                          error=f"{type(exc).__name__}: export parsing or storage failed")
        con.execute("RELEASE activities_batch")
        con.execute(
            "UPDATE ingest_runs SET completed_at=%s, rows_seen=%s, rows_new=%s, rows_updated=%s, "
            "status=%s, error=%s WHERE id=%s",
            (utc_now(), result["rows_seen"], result["rows_new"], result["rows_updated"],
             result["status"], result["error"], run_id),
        )
        con.commit()
        return result
    finally:
        con.close()


def ingest_source(source: Source, archive: Path = Path("archive")) -> list[dict]:
    con = connect()
    try:
        seen = {r["source_version"] for r in con.execute(
            "SELECT source_version FROM ingest_runs WHERE status IN ('success', 'skipped') "
            "AND source_version IS NOT NULL"
        )}
    finally:
        con.close()
    with tempfile.TemporaryDirectory() as tmp:
        return [ingest_file(export, archive) for export in source.files(seen, Path(tmp))]


def main():
    load_environment()
    parser = argparse.ArgumentParser(description="Ingest full Health Connect ZIP snapshots")
    parser.add_argument("--source", choices=("local", "drive"), default="local")
    parser.add_argument("--path", type=Path, default=Path("./inbox"))
    parser.add_argument("--archive", type=Path, default=Path(os.getenv(
        "STRAVA_SYNC_ARCHIVE", os.getenv("FITNESS_ARCHIVE", "./archive")
    )))
    args = parser.parse_args()
    try:
        supabase_url()
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    source = LocalFolderSource(args.path) if args.source == "local" else DriveSource()
    try:
        results = ingest_source(source, args.archive)
    except Exception as exc:
        parser.exit(1, f"Ingest failed ({type(exc).__name__}); check source configuration and permissions.\n")
    print(json.dumps(results, indent=2))
    if any(r["status"] == "failed" for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
