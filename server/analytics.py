"""Saturday–Friday summaries, using Asia/Kolkata calendar dates."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .store import connect

LOCAL_TZ = ZoneInfo("Asia/Kolkata")
SOURCE_LIMITATION = (
    "Health Connect export provides start/end, elapsed duration, and distance when present; "
    "pace/speed are whole-activity averages. No heart rate, heart-rate zones, laps, splits, "
    "GPS route, elevation, cadence, power, or moving-time measurements are available. "
    "Interval quality, within-run pacing, and physiological intensity cannot be assessed "
    "from these averages. Missing metrics are unavailable, not zero."
)
METRICS = ("count", "total_km", "total_time_s", "average_pace_min_per_km", "average_speed_kmh")


def local_start(activity: dict) -> datetime:
    return datetime.fromisoformat(activity["start_utc"]).astimezone(LOCAL_TZ)


def metrics(activities: list[dict]) -> dict:
    distance = sum(a["distance_m"] or 0 for a in activities) / 1000
    duration = sum(a["duration_s"] for a in activities)
    # Rates use only records with measured distance, so missing distance is not zero.
    measured = [a for a in activities if a["distance_m"] is not None
                and a["distance_m"] > 0 and a["duration_s"] > 0]
    measured_time = sum(a["duration_s"] for a in measured)
    measured_distance = sum(a["distance_m"] for a in measured) / 1000
    return {
        "count": len(activities), "total_km": distance, "total_time_s": duration,
        "average_pace_min_per_km": measured_time / 60 / measured_distance if measured_distance > 0 else None,
        "average_speed_kmh": measured_distance / (measured_time / 3600) if measured_time > 0 else None,
    }


def activity_view(activity: dict) -> dict:
    start = local_start(activity)
    km = activity["distance_m"] / 1000 if activity["distance_m"] is not None else None
    pace = activity["duration_s"] / 60 / km if km and km > 0 and activity["duration_s"] > 0 else None
    pace_seconds = round(pace * 60) if pace is not None else None
    return {
        **activity, "start_local": start.isoformat(), "weekday": start.strftime("%A"),
        "km": km, "pace_min_per_km": pace,
        "pace_formatted": f"{pace_seconds // 60}:{pace_seconds % 60:02d}/km" if pace_seconds is not None else None,
        "strava_url": f"https://www.strava.com/activities/{activity['source_id']}"
        if activity["source"] == "strava" else None,
    }


def week_end(value: str | None) -> date:
    if value is None:
        today = datetime.now(LOCAL_TZ).date()
        return today - timedelta(days=(today.weekday() - 4) % 7)
    ending = date.fromisoformat(value)
    if ending.weekday() != 4:
        raise ValueError("week_ending must be a Friday (YYYY-MM-DD)")
    return ending


class Analytics:
    def _snapshot(self) -> tuple[list[dict], str | None]:
        con = connect(readonly=True)
        try:
            rows = [dict(r) for r in con.execute("SELECT * FROM activities ORDER BY start_utc, source_id")]
            last = con.execute(
                "SELECT MAX(completed_at) AS last_ingest FROM ingest_runs WHERE status = 'success'"
            ).fetchone()["last_ingest"]
            return rows, last
        finally:
            con.close()

    @staticmethod
    def _envelope(rows: list[dict], last: str | None) -> dict:
        limitations = [SOURCE_LIMITATION,
                      'A day with no recorded activity means "no activity recorded"; coverage is not proof of inactivity.']
        if last is None:
            limitations.append("No successful ingest is available yet.")
        elif datetime.now(timezone.utc) - datetime.fromisoformat(last) > timedelta(hours=36):
            limitations.append("Staleness warning: the last successful ingest is older than 36 hours.")
        if any(a["distance_m"] is None for a in rows):
            limitations.append("Some activities have no distance: total km includes known distances only; pace and speed exclude those activities.")
        if any(a["duration_s"] == 0 or a["distance_m"] == 0 for a in rows):
            limitations.append("Activities with zero duration or zero distance are excluded from pace and speed calculations.")
        limitations.append("Totals include all recorded exercise types, not a running-only filter; inspect exercise_type_code before describing them as runs.")
        return {"data_available": bool(rows), "last_ingest_utc": last, "limitations": limitations}

    @staticmethod
    def _between(rows: list[dict], start: date, end: date) -> list[dict]:
        return [a for a in rows if start <= local_start(a).date() <= end]

    def get_data_status(self) -> dict:
        rows, last = self._snapshot()
        dates = sorted({local_start(a).date().isoformat() for a in rows})
        return {
            **self._envelope(rows, last), "last_successful_ingest_utc": last,
            "newest_activity_date": dates[-1] if dates else None,
            "total_activities": len(rows), "dates_with_data": dates,
            "coverage": (
                f"{len(rows)} activities recorded on {len(dates)} dates"
                + (f", from {dates[0]} through {dates[-1]}. " if dates else ". ")
                + 'Other dates have no activity recorded. Exports do not establish complete daily coverage.'
            ),
        }

    def _weekly(self, rows: list[dict], last: str | None, ending: date) -> dict:
        start = ending - timedelta(days=6)
        selected = self._between(rows, start, ending)
        dates = {local_start(a).date() for a in selected}
        return {
            **self._envelope(selected, last), "week_ending": ending.isoformat(),
            "start_date": start.isoformat(), "end_date": ending.isoformat(), "timezone": "Asia/Kolkata",
            **metrics(selected), "activities": [activity_view(a) for a in selected],
            "days": [{"date": (day := start + timedelta(days=i)).isoformat(),
                      "status": "activity recorded" if day in dates else "no activity recorded"}
                     for i in range(7)],
        }

    def get_weekly_summary(self, week_ending: str | None = None) -> dict:
        rows, last = self._snapshot()
        return self._weekly(rows, last, week_end(week_ending))

    def compare_weeks(self, week_ending: str | None = None, prior_weeks: int = 4) -> dict:
        if not 1 <= prior_weeks <= 52:
            raise ValueError("prior_weeks must be between 1 and 52")
        rows, last = self._snapshot()
        ending = week_end(week_ending)
        current = self._weekly(rows, last, ending)
        previous = [self._weekly(rows, last, ending - timedelta(weeks=i))
                    for i in range(1, prior_weeks + 1)]
        baseline_available = any(w["data_available"] for w in previous)
        average = {}
        for key in METRICS:
            values = [w[key] for w in previous if w[key] is not None]
            average[key] = sum(values) / len(values) if values and baseline_available else None

        def deltas(baseline: dict, available: bool) -> dict:
            result = {}
            for key in METRICS:
                now, before = current[key], baseline[key]
                absolute = now - before if current["data_available"] and available and now is not None and before is not None else None
                result[key] = {"absolute": absolute,
                               "percent": absolute / before * 100 if absolute is not None and before else None}
            return result

        relevant = self._between(rows, ending - timedelta(weeks=prior_weeks, days=6), ending)
        envelope = self._envelope(relevant, last)
        envelope["data_available"] = current["data_available"]
        return {
            **envelope, "current_week": current, "prior_week": previous[0],
            "prior_weeks": prior_weeks,
            "prior_weeks_average": {"data_available": baseline_available, **average},
            "prior_week_deltas": deltas(previous[0], previous[0]["data_available"]),
            "prior_average_deltas": deltas(average, baseline_available),
            "baseline_week_endings": [w["week_ending"] for w in previous],
            "averaging_note": "Count, distance and time average across all requested weeks, including weeks with no activity recorded. Pace and speed average the defined weekly rates. No recorded baseline yields null deltas; zero baselines yield null percentages.",
        }

    def list_activities(self, start_date: str, end_date: str) -> dict:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if end < start:
            raise ValueError("end_date must be on or after start_date")
        rows, last = self._snapshot()
        selected = self._between(rows, start, end)
        return {**self._envelope(selected, last), "start_date": start_date, "end_date": end_date,
                "activities": [activity_view(a) for a in selected]}

    def get_activity(self, source_id: str) -> dict:
        rows, last = self._snapshot()
        selected = [a for a in rows if a["source_id"] == source_id]
        return {**self._envelope(selected, last),
                "activity": activity_view(selected[0]) if selected else None}
