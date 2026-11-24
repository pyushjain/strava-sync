import hashlib
import math
from datetime import datetime, timezone


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")


def normalize(row: dict) -> dict:
    client_id = row.get("client_record_id") or ""
    if client_id.startswith("strava-"):
        source, source_id = "strava", client_id.rsplit("-", 1)[-1]
        if not source_id.isdigit():
            raise ValueError("Strava client_record_id must end with a numeric activity ID")
    else:
        if not row.get("app_package"):
            raise ValueError("Non-Strava session requires an application package for identity")
        identity = "|".join(str(row[k]) for k in
                            ("app_package", "start_time", "end_time", "exercise_type"))
        source, source_id = "health_connect", hashlib.sha1(identity.encode()).hexdigest()
    duration = (row["end_time"] - row["start_time"]) / 1000
    distance = row["distance_m"]
    if not math.isfinite(duration) or duration < 0 or (
        distance is not None and (not math.isfinite(distance) or distance < 0)
    ):
        raise ValueError("Duration and distance must be finite and non-negative")
    return {
        "source": source,
        "source_id": source_id,
        "source_app": row["source_app"],
        "type_code": row["exercise_type"],
        "start_utc": ms_to_iso(row["start_time"]),
        "end_utc": ms_to_iso(row["end_time"]),
        "tz_offset_s": row["start_zone_offset"] if row["start_zone_offset"] is not None else 19800,
        "duration_s": duration,
        "distance_m": row["distance_m"],
        "last_modified_utc": ms_to_iso(row["last_modified_time"]),
    }
