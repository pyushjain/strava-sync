# Strava Sync

Python 3.11+. Health Connect ZIP exports from Google Drive or a local folder are
ingested into **Supabase PostgreSQL** and queried through five read-only FastMCP
tools. Supabase is the only application database. Missing database configuration
or connection failures are errors when ingesting or querying data; the public
health endpoint works independently.

## Local setup

```sh
python -m venv .venv
. .venv/bin/activate
pip install -c requirements.lock -e '.[test,drive]'
```

Create `.env` from `.env.example`. In Supabase's **Connect** panel, copy the
PostgreSQL URI, preferably the **session pooler** on port 5432 for IPv4 connectivity.
Use your database password, not an API key; URL-encode special password characters.

```dotenv
SUPABASE_DB_URL='postgresql://postgres.PROJECT:PASSWORD@POOLER_HOST:5432/postgres?sslmode=require'
```

All CLI entry points load `.env` from the current working directory. Existing
environment variables take precedence. Create the schema once for a new project:

```sh
python -m server.store
```

Tables appear under the **fitness** schema in Supabase's Table Editor:

- `fitness.activities`: deduplicated activities and original modification times.
- `fitness.ingest_runs`: processed-file identities, checksums, counts, and outcomes.

Put local ZIPs in `inbox/`, then run:

```sh
python -m server.ingest --source local --path ./inbox
python -m server.mcp_server
```

Connect locally to `http://127.0.0.1:8000/mcp`. The server defaults to binding
`0.0.0.0`; `.env.example` sets `MCP_HOST=127.0.0.1` for local development.
`PORT` takes precedence over `MCP_PORT`, which defaults to 8000.
If `MCP_PATH_SECRET` is set, use `/mcp/<secret>` instead. MCP requests only query
stored data and never trigger ingestion.

## Production deployment: Render Free + GitHub Actions

```text
Google Drive ZIP → GitHub Actions (daily 06:00 Asia/Kolkata) → Supabase PostgreSQL
                                                                  ↑
                                                       Render Free MCP server
                                                                  ↑
                                                   ChatGPT / MCP clients
UptimeRobot Free → GET Render /healthz every 5 minutes
```

Render runs only the HTTP server. GitHub Actions runs ingestion as a short-lived
job. This setup requires no Render background worker, persistent Render disk,
production VM, Docker Compose host, Doppler CLI, or Cloud Scheduler.

### 1. Supabase and Google Drive

Initialize the Supabase schema once using `python -m server.store` with your
database connection configured. The workflow does not initialize or recreate it.
Use a TLS connection URI (`sslmode=require`, or `verify-full` with a trusted CA).
Keep `fitness` outside Supabase's exposed Data API schemas and configure backups
appropriate to your data.

Enable the Google Drive API for your Google Cloud project, create a service
account, and share the export folder with its email address as a **viewer**.
Keep Health Connect ZIP exports in that folder. Save the service-account JSON
and folder ID in the Doppler configuration described below.

### 2. Render deployment

1. Connect this GitHub repository to Render and create a **Web Service**.
2. Select branch **main**, the **Docker** runtime, and the existing root `Dockerfile`.
3. Select the **Free** instance plan. The image's default command runs
   `python -m server.mcp_server`; no scheduler or ingestion process is needed.
4. Set the service health check path to **`/healthz`**.
5. Configure these Render environment variables:

| Variable | Value |
| --- | --- |
| `SUPABASE_DB_URL` | Supabase PostgreSQL connection URI |
| `MCP_PUBLIC_URL` | `https://YOUR-SERVICE.onrender.com` (origin only, no path) |
| `MCP_PATH_SECRET` | A long, random URL-safe secret |

Generate a path secret locally with:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Render supplies `PORT` automatically (normally 10000). It overrides `MCP_PORT`;
the server binds to `0.0.0.0` unless `MCP_HOST` is explicitly configured.
Do not set `MCP_HOST` to loopback on Render. Drive credentials are only needed
in GitHub Actions, not in the Render web service.

The final MCP connector URL is:

```text
https://YOUR-SERVICE.onrender.com/mcp/YOUR_SECRET
```

Use **Streamable HTTP** and select **no authentication** in the connector UI.
Access is controlled by the secret path; no bearer header is required. Keep the
full connector URL private. The application disables HTTP access logs to avoid
logging the secret path; avoid recording it in any additional proxy logs.
`MCP_PUBLIC_URL` enables host/origin validation and requires `MCP_PATH_SECRET`.

### 3. UptimeRobot Free

Create a free **HTTP(s)** monitor with:

- URL: `https://YOUR-SERVICE.onrender.com/healthz`
- Method: **GET**
- Interval: **5 minutes**
- Authentication: **none**

The endpoint returns HTTP 200 and `{"status":"ok"}`. It makes no database query
or external API call and exposes no credentials or data. It works independently
of the secret MCP path and does not require Supabase credentials.

These external requests help avoid Render's 15-minute inactivity spin-down.
They cannot prevent Render-initiated restarts or guarantee uninterrupted uptime.
Health success indicates HTTP liveness, not database connectivity or data freshness.

### 4. GitHub Actions ingestion

Create a Doppler configuration dedicated to ingestion with these variables:

| Doppler variable | Purpose |
| --- | --- |
| `SUPABASE_DB_URL` | PostgreSQL connection URI |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Entire service-account JSON |
| `DRIVE_FOLDER_ID` | Google Drive export folder |

Paste the entire original JSON object, including the private key, into the JSON
variable. Do not base64-encode it or wrap it in another JSON string. Credentials
are parsed in memory; no credentials file is written.

Create a read-only Doppler service token scoped to that configuration. Under
**Settings → Secrets and variables → Actions → New repository secret**, add only
`DOPPLER_SERVICE_TOKEN`. The token selects the Doppler project/configuration;
no additional project or configuration variables are needed. Once configured,
remove the old three ingestion secrets from GitHub Actions.

The wrapper fetches configuration over HTTPS and passes only the three required
Doppler variables to the ingestion process. It removes the service token from
the child environment, rejects redirects, and fails if Doppler is unavailable
or required values are missing. It does not write secrets to files, command-line
arguments, GitHub environment/output files, or logs. Child output is suppressed
to prevent exception details or export identifiers from leaking into CI logs;
only a generic success/failure message is emitted. Do not enable shell tracing
or add environment/secret dumps to the workflow. Check `fitness.ingest_runs` in
Supabase for ingestion counts and outcomes.

`.github/workflows/daily-ingest.yml` schedules ingestion **daily at 6:00 AM IST**
using cron `0 6 * * *` with timezone `Asia/Kolkata`. Scheduled workflows run from
the default branch: ensure the repository's default branch is **main** and the
workflow is present there. GitHub Actions scheduling can experience delays;
06:00 IST is the scheduled time, not a guaranteed exact execution time.

For a manual run, open **Actions → Daily Drive ingestion → Run workflow**, select
**main**, and run it. Other branches are skipped by the job guard. Inspect each
scheduled or manual run's **ingest** job and **Ingest Google Drive export** logs
for outcomes. Failed ingestion fails the job. The job has a 30-minute timeout,
read-only repository permissions, and concurrency protection shared by manual
and scheduled runs.

The job uses Python 3.11 and installs with:

```sh
pip install -c requirements.lock '.[drive]'
python -m server.doppler_ingest
```

Archives go into a temporary runner directory via `STRAVA_SYNC_ARCHIVE` and are
discarded with the ephemeral runner. Original exports remain in Google Drive;
ingestion state and activities persist in Supabase. No archive upload, Render
disk, or additional storage service is required.

After configuring the services, manually run ingestion and call `get_data_status`
through the connector to verify data access and freshness. Check Render service
logs and UptimeRobot status separately. These steps must be verified in your
deployed environment.

## Local Docker Compose

With `.env` configured and the cloud schema created:

```sh
docker compose up --build
```

Compose uses Linux host networking for builds and containers. The MCP server
binds to `127.0.0.1:8000` on the host and mounts `inbox/` and `archive/`. There is
no application database volume. Its HTTP `/healthz` check verifies liveness only.
Logs rotate at 10 MB with three retained files per service.

Ingest a local ZIP explicitly while the server is running:

```sh
docker compose exec server python -m server.ingest --source local --path /app/inbox
```

With [just](https://just.systems), `just local`, `just local-refresh`,
`just local-ingest`, `just logs`, and `just stop` provide local shortcuts.
MCP startup does not ingest exports, so malformed ZIPs cannot prevent startup.

## Google Drive and optional self-hosted scheduler

For local Drive ingestion, install `.[drive]` using `requirements.lock` and set
`DRIVE_FOLDER_ID` and `GOOGLE_SERVICE_ACCOUNT_JSON` in your environment or `.env`.
`GOOGLE_APPLICATION_CREDENTIALS` can alternatively point to a local JSON file;
the JSON environment variable takes precedence.

```sh
python -m server.ingest --source drive
```

Each invocation downloads at most one ZIP: the newest **unseen version**, ordered
by Drive modification time. Supabase tracks file ID + modifiedTime + MD5.
Repeated invocations may process older unseen snapshots. Drive access is
read-only; failed ingests remain eligible for retry. Drive HTTP calls have a
60-second timeout; extracted export databases are limited to 1 GiB.

The scheduler remains available for alternative local/self-hosted deployments:

```dotenv
DRIVE_INGEST_TIME=06:00
DRIVE_INGEST_TIMEZONE=Asia/Kolkata
DRIVE_INGEST_TIMEOUT_S=1800
```

```sh
docker compose --profile drive up --build -d
docker compose logs -f drive-ingest
# Immediate ingestion with the optional worker's configuration:
docker compose --profile drive exec drive-ingest python -m server.ingest --source drive
# Or run the scheduler directly:
python -m server.scheduler
```

The scheduler waits until the next scheduled time, does not ingest on startup,
and does not catch up missed runs. Failed jobs retry at the next daily run.
It terminates jobs after the configured timeout and reaps active children on
shutdown. Nonexistent daylight-saving times are skipped; repeated times run once.
Apply `.env` changes with `docker compose --profile drive up -d`. Local archive
cleanup remains manual. Avoid running this worker against the production database
unless you intend additional ingestion alongside GitHub Actions.

## Data handling

- Downloads and extracted databases use temporary directories. Outside Actions,
  original ZIPs are retained in `archive/` by SHA-256 filename. Override with
  `STRAVA_SYNC_ARCHIVE` (legacy `FITNESS_ARCHIVE` is also accepted). Identical
  content shares an archive filename. Archives are not database backups.
- ZIP parsing uses Python's SQLite reader with `mode=ro` for the source
  `health_connect_export.db`; all application data lives exclusively in Supabase.
- Sessions join to distance by app ID and exact start/end milliseconds. Multiple
  matches use the newest distance row. Missing distance remains null.
- Activity identity is `(strava, trailing activity ID)` or
  `(health_connect, sha1(package|start_ms|end_ms|type_code))`. Exercise codes are
  retained as raw integers in `exercise_type_code`.
- Updates require a newer modification time, taking the maximum of session and
  matched distance. Full snapshots add/update records; disappearance does not
  delete history. File activity writes are atomic; failed runs retain their records.
- PostgreSQL advisory transaction locking serializes deduplication and activity
  writes across ingesters. MCP connections use read-only transactions. Schema
  initialization requires create/ownership privileges.

## Tools and analytics

All five tools have `readOnlyHint=true`:

- `get_data_status()`
- `get_weekly_summary(week_ending?)`
- `compare_weeks(week_ending?, prior_weeks=4)`
- `list_activities(start_date, end_date)`
- `get_activity(source_id)`

Weeks use Asia/Kolkata activity start dates, Saturday–Friday inclusive.
`week_ending` must be a Friday; the default is the most recent Friday, including
today when Friday. Missing source offsets default to +05:30.

Pace is elapsed time / distance; speed is distance / elapsed time. Missing/zero
distance or zero duration are excluded from rates. Totals include all exercise
types, not just running. Comparisons explain averaging and null-baseline rules.
Every response includes `data_available`, `last_ingest_utc`, and `limitations`.
Successful distinct snapshots establish freshness; checksum skips do not reset
it. More than 36 hours triggers a staleness warning.

Empty dates mean **no activity recorded**, not evidence of inactivity. Exports
do not provide heart rate, zones, laps, splits, GPS routes, elevation, cadence,
power, or moving time. Whole-activity averages cannot establish interval quality
or physiological fitness.

## Connect Inspector / ChatGPT locally

```sh
npx @modelcontextprotocol/inspector
```

Choose **Streamable HTTP**, enter `http://127.0.0.1:8000/mcp`, and list tools.
If a secret is configured, use `/mcp/<secret>` instead.
For the synthetic test sample, use week ending `2026-11-27`, activity ID
`21078195405`, or date range `2026-11-22`–`2026-11-22`: 6,068.2 m, 2,951 s,
approximately 8:06/km.

For a local HTTPS tunnel, run `ngrok http 8000`, configure
`MCP_PUBLIC_URL=https://your-host.ngrok-free.dev` and `MCP_PATH_SECRET`, and restart
the server when the hostname changes. Use
`https://your-host.ngrok-free.dev/mcp/<secret>` in ChatGPT's custom connector with
no authentication selected. Host/origin validation permits the configured origin
and localhost. For production, use the Render connector URL above.

## Tests and dependency updates

```sh
pytest -q
```

Tests create and remove a disposable PostgreSQL 17 Docker container using host
networking and a random loopback-only port. Docker host networking must be
available (native on Linux; enable it on Docker Desktop). Alternatively:

```sh
TEST_SUPABASE_DB_URL='postgresql://USER:PASSWORD@localhost:5432/fitness_test' pytest -q
```

The test database name must end in `_test`; its `fitness` tables are truncated.
Tests isolate the real `.env` database credentials and generate synthetic ZIPs;
no private archive or real Google credentials are needed. Coverage includes
ingestion, deduplication, newer-only updates, rollback, analytics, read-only
connections, MCP tools over real HTTP, secret-path access, host/origin validation,
public health checks without database credentials, and Render port selection.

Runtime versions are constrained by `requirements.lock` in Docker, Actions, and
the development install command. Update constraints deliberately and rerun
`just test` and the Docker build before deploying dependency upgrades.
