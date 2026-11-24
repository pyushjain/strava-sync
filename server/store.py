"""Supabase PostgreSQL connections, schema setup, and activity upserts."""

import os
from datetime import datetime, timezone
from importlib.resources import files

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row


def load_environment():
    """CLI entry points load .env from the working directory; shell values win."""
    load_dotenv(".env", override=False)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def supabase_url() -> str:
    url = os.getenv("SUPABASE_DB_URL", "").strip()
    if not url:
        raise ValueError("SUPABASE_DB_URL is required. Set it in .env or your shell environment.")
    return url


def iso_dict_row(cursor):
    make_row = dict_row(cursor)

    def convert(values):
        return {key: value.astimezone(timezone.utc).isoformat(timespec="milliseconds")
                if isinstance(value, datetime) else value
                for key, value in make_row(values).items()}

    return convert


def connect(*, readonly: bool = False) -> psycopg.Connection:
    con = psycopg.connect(supabase_url(), autocommit=True,
                          row_factory=iso_dict_row, connect_timeout=15,
                          prepare_threshold=None)
    try:
        con.execute("SET search_path TO fitness, pg_catalog")
        con.execute("SET TIME ZONE 'UTC'")
        con.execute("SET statement_timeout = '60s'")
        con.autocommit = False
        if readonly:
            con.read_only = True
            con.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        return con
    except Exception:
        con.close()
        raise


def begin_ingest(con: psycopg.Connection):
    # PostgreSQL starts the transaction on this statement. Hold the lock until
    # commit/rollback to serialize checksum checks, activity writes, and counts.
    con.execute("SELECT pg_advisory_xact_lock(734620193)")


def initialize_schema():
    sql = files("server").joinpath("schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(supabase_url(), connect_timeout=15, prepare_threshold=None) as con:
        con.execute(sql)


def upsert(con: psycopg.Connection, activity: dict) -> str:
    old = con.execute(
        "SELECT last_modified_utc FROM activities WHERE source = %s AND source_id = %s",
        (activity["source"], activity["source_id"]),
    ).fetchone()
    if old and datetime.fromisoformat(activity["last_modified_utc"]) <= datetime.fromisoformat(old["last_modified_utc"]):
        return "skipped"
    values = dict(activity)
    values["exercise_type_code"] = values.pop("type_code")
    columns = list(values)
    updates = ", ".join(f"{c}=excluded.{c}" for c in columns if c not in ("source", "source_id"))
    con.execute(
        f"INSERT INTO activities ({', '.join(columns)}) VALUES ({', '.join('%s' for _ in columns)}) "
        f"ON CONFLICT(source, source_id) DO UPDATE SET {updates} "
        "WHERE excluded.last_modified_utc > activities.last_modified_utc",
        list(values.values()),
    )
    return "updated" if old else "new"


def main():
    load_environment()
    try:
        supabase_url()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    try:
        initialize_schema()
    except Exception as exc:
        raise SystemExit(f"Schema setup failed ({type(exc).__name__}); check database connection and privileges.") from None
    print("Strava Sync database is ready (Supabase schema: fitness).")


if __name__ == "__main__":
    main()
