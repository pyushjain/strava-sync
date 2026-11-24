"""The original converter's session/distance join, limited to relevant records."""

import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path

MAX_DATABASE_BYTES = 1024 * 1024 * 1024


def parse_export(path: Path):
    """Yield raw sessions; extract only the DB to a controlled temporary path."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "health_connect_export.db"
        with zipfile.ZipFile(path) as archive:
            matches = [n for n in archive.namelist()
                       if Path(n).name == "health_connect_export.db"]
            if len(matches) != 1:
                raise ValueError("Export must contain exactly one health_connect_export.db")
            if archive.getinfo(matches[0]).file_size > MAX_DATABASE_BYTES:
                raise ValueError("Export database exceeds the 1 GiB extraction limit")
            with archive.open(matches[0]) as src, db.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            apps = {r["row_id"]: dict(r) for r in con.execute(
                "SELECT row_id, app_name, package_name FROM application_info_table"
            )}
            for session in con.execute(
                "SELECT * FROM exercise_session_record_table ORDER BY start_time"
            ):
                row = dict(session)
                app = apps.get(row["app_info_id"], {})
                row["app_package"] = app.get("package_name")
                row["source_app"] = app.get("app_name") or app.get("package_name")
                # Select one latest exact match, rather than multiplying sessions.
                distance = con.execute(
                    "SELECT distance, last_modified_time FROM distance_record_table "
                    "WHERE app_info_id IS ? AND start_time = ? AND end_time = ? "
                    "ORDER BY last_modified_time DESC, row_id DESC LIMIT 1",
                    (row["app_info_id"], row["start_time"], row["end_time"]),
                ).fetchone()
                row["distance_m"] = distance["distance"] if distance else None
                if distance:
                    row["last_modified_time"] = max(
                        row["last_modified_time"], distance["last_modified_time"]
                    )
                yield row
        finally:
            con.close()
