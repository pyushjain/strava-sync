"""Read-only FastMCP tools served over streamable HTTP."""

import os
from typing import Any
from urllib.parse import quote, urlsplit

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .analytics import Analytics
from .store import load_environment, supabase_url

SERVER_INSTRUCTIONS = """This connector provides read-only running/activity analytics
from ingested Health Connect exports, not the full Strava API or live Strava data.
Available: recorded activity start/end times, elapsed duration, distance when present,
derived whole-activity average pace/speed, activity counts, weekly totals and comparisons.
Unavailable: heart rate, heart-rate zones, laps, splits, GPS routes, elevation,
cadence, power, and moving-time measurements. Strava links/IDs do not make those
metrics accessible through this connector.
Call get_data_status before analyzing to check freshness and recorded-date coverage.
Read data_available, last_ingest_utc, and limitations in every response.
Base analysis on observed distance, elapsed duration, average pace, and recorded
frequency; distinguish observations from hypotheses. Whole-activity averages cannot
establish interval quality, within-run pacing, physiological intensity, recovery,
or fitness/VO2-max estimates. Do not invent unavailable measurements or treat them
as zero. If asked about them, explain the limitation and ask for additional data.
No activity recorded on a date is not proof of a rest day or inactivity.
Weeks run Saturday–Friday in Asia/Kolkata; week_ending must be a Friday.
Activities may include non-running exercise types. Inspect exercise_type_code;
weekly totals aggregate all recorded types and must not be labeled running-only.
"""


def create_server() -> FastMCP:
    secret = os.getenv("MCP_PATH_SECRET", "")
    path = "/mcp" + ("/" + quote(secret, safe="") if secret else "")
    allowed_hosts = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"]
    allowed_origins = ["http://127.0.0.1", "http://127.0.0.1:*", "http://localhost", "http://localhost:*",
                       "http://[::1]", "http://[::1]:*"]
    public_url = os.getenv("MCP_PUBLIC_URL", "").strip().rstrip("/")
    if public_url:
        if not secret.strip():
            raise ValueError("MCP_PATH_SECRET is required when MCP_PUBLIC_URL is configured")
        parsed = urlsplit(public_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("MCP_PUBLIC_URL must be an HTTP(S) origin with no path or credentials")
        allowed_hosts.append(parsed.netloc)
        allowed_origins.append(public_url)
    server = FastMCP(
        "Strava Sync", host=os.getenv("MCP_HOST", "127.0.0.1"),
        instructions=SERVER_INSTRUCTIONS,
        streamable_http_path=path, stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts, allowed_origins=allowed_origins,
        ),
    )
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=annotations)
    def get_data_status() -> dict[str, Any]:
        """Call first before analysis: return ingest freshness, recorded dates, and limitations.

        Health Connect exports only; no heart rate, laps/splits, GPS, elevation,
        cadence, or power. Missing dates are not proof of inactivity.
        """
        return Analytics().get_data_status()

    @server.tool(annotations=annotations)
    def get_weekly_summary(week_ending: str | None = None) -> dict[str, Any]:
        """Saturday–Friday summary in Asia/Kolkata; ending is a Friday YYYY-MM-DD.

        Counts, distance, elapsed duration, and average pace/speed only. No heart
        rate, laps/splits, or routes; averages cannot establish interval quality.
        """
        return Analytics().get_weekly_summary(week_ending)

    @server.tool(annotations=annotations)
    def compare_weeks(week_ending: str | None = None, prior_weeks: int = 4) -> dict[str, Any]:
        """Compare a week with the prior week and the average of prior_weeks weeks.

        Compare recorded volume and whole-activity averages, not physiological
        intensity or fitness. No heart rate, laps/splits, or routes are available.
        """
        return Analytics().compare_weeks(week_ending, prior_weeks)

    @server.tool(annotations=annotations)
    def list_activities(start_date: str, end_date: str) -> dict[str, Any]:
        """List activities by inclusive Asia/Kolkata start dates (YYYY-MM-DD).

        Start/end, elapsed duration, distance, derived average pace/speed only;
        no heart rate, laps/splits, GPS, elevation, cadence, or power.
        """
        return Analytics().list_activities(start_date, end_date)

    @server.tool(annotations=annotations)
    def get_activity(source_id: str) -> dict[str, Any]:
        """Retrieve one activity using its Strava activity ID or fallback SHA-1 ID.

        Returns the ingested export, not full Strava details: start/end, elapsed
        duration, distance, and derived average pace/speed. No heart rate,
        laps/splits, GPS, elevation, cadence, power, or moving-time measurements.
        """
        return Analytics().get_activity(source_id)

    return server


def create_app():
    return create_server().streamable_http_app()


def main():
    load_environment()
    try:
        supabase_url()
        app = create_app()
        port = int(os.getenv("MCP_PORT", "8000"))
        if not 1 <= port <= 65535:
            raise ValueError("MCP_PORT must be between 1 and 65535")
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    # Access logs are disabled because the URL can contain the path secret.
    uvicorn.run(app, host=os.getenv("MCP_HOST", "127.0.0.1"),
                port=port, access_log=False,
                log_level="warning")


if __name__ == "__main__":
    main()
