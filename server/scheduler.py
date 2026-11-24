"""Run Drive ingestion once per day at a configured local time."""

import logging
import os
import signal
import subprocess
import sys
import threading
import time as clock
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .store import load_environment, supabase_url
from .sources import DriveSource


def next_run(now: datetime, daily_time: time, zone: ZoneInfo) -> datetime:
    """Return the next future occurrence (UTC), skipping nonexistent DST times."""
    date = now.astimezone(zone).date()
    while True:
        candidate = datetime.combine(date, daily_time, tzinfo=zone)
        utc = candidate.astimezone(timezone.utc)
        if utc > now and utc.astimezone(zone).replace(tzinfo=None) == candidate.replace(tzinfo=None):
            return utc
        date += timedelta(days=1)


def configuration():
    """Validate the schedule and the same credentials used by Drive ingestion."""
    try:
        raw_time = os.getenv("DRIVE_INGEST_TIME", "06:00")
        daily_time = datetime.strptime(raw_time, "%H:%M").time()
        zone = ZoneInfo(os.getenv("DRIVE_INGEST_TIMEZONE", "Asia/Kolkata"))
        if int(os.getenv("DRIVE_INGEST_TIMEOUT_S", "1800")) <= 0:
            raise ValueError("DRIVE_INGEST_TIMEOUT_S must be a positive integer")
        supabase_url()
        if not os.getenv("DRIVE_FOLDER_ID", "").strip():
            raise ValueError("DRIVE_FOLDER_ID is required")
        DriveSource.credentials()
    except (ValueError, KeyError, OSError) as exc:
        raise SystemExit(f"Invalid scheduler configuration: {exc}") from None
    return raw_time, daily_time, zone


def wait_for_ingest(child, stopped: threading.Event, timeout: int) -> int:
    deadline = clock.monotonic() + timeout
    while child.poll() is None:
        if stopped.is_set() or clock.monotonic() >= deadline:
            if not stopped.is_set():
                logging.error("Drive ingestion exceeded %s seconds; terminating", timeout)
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            return 1
        try:
            return child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    return child.returncode


def main():
    load_environment()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raw_time, daily_time, zone = configuration()

    stopped = threading.Event()
    child = None

    def stop(signum, frame):
        stopped.set()
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logging.info("Daily Drive ingestion scheduled for %s %s", raw_time, zone.key)
    while not stopped.is_set():
        scheduled = next_run(datetime.now(timezone.utc), daily_time, zone)
        logging.info("Next ingestion: %s", scheduled.astimezone(zone).isoformat())
        # Recheck the clock periodically so clock corrections do not shift the schedule.
        while not stopped.is_set():
            remaining = (scheduled - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                break
            stopped.wait(min(remaining, 60))
        if stopped.is_set():
            break
        logging.info("Starting Drive ingestion")
        child = subprocess.Popen([sys.executable, "-m", "server.ingest", "--source", "drive"])
        code = wait_for_ingest(child, stopped, int(os.getenv("DRIVE_INGEST_TIMEOUT_S", "1800")))
        child = None
        if code:
            logging.error("Drive ingestion failed (exit %s); next attempt at next daily run", code)
        else:
            logging.info("Drive ingestion completed")


if __name__ == "__main__":
    main()
