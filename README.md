# Strava Sync

Python 3.11+. Health Connect exports are ingested into **Supabase PostgreSQL** and
queried through five read-only FastMCP tools. Supabase is the only application
database: missing configuration or a connection failure is an error.

## Configure and run

```sh
python -m venv .venv
. .venv/bin/activate
pip install -c requirements.lock -e '.[test,drive]'
```

Create `.env` from `.env.example` if you do not already have one. In Supabase's
**Connect** panel, copy the PostgreSQL URI, preferably the **session pooler** on
port 5432 for IPv4 connectivity. Use your database password, not an API key;
URL-encode any special password characters.

```dotenv
SUPABASE_DB_URL='postgresql://postgres.PROJECT:PASSWORD@POOLER_HOST:5432/postgres?sslmode=require'
```

All CLI entry points automatically load `.env` from the current working directory.
Existing shell environment variables take precedence.

For a new Supabase project, create the tables once (safe to repeat):

```sh
python -m server.store
```

Tables appear under the **fitness** schema in Supabase's Table Editor:

- `fitness.activities`: deduplicated activities and original modification times.
- `fitness.ingest_runs`: processed-file identities, checksums, counts, and outcomes.

To ingest exports, put ZIPs in `inbox/` and run:

```sh
python -m server.ingest --source local --path ./inbox
python -m server.mcp_server
```

After configuration, a one-command run is:

```sh
python -m server.ingest --source local --path ./inbox && python -m server.mcp_server
```

The MCP endpoint defaults to `http://127.0.0.1:8000/mcp` (unless `MCP_HOST`,
`MCP_PORT`, or `MCP_PATH_SECRET` is set). Schedule ingestion externally if desired;
MCP requests only query the stored data and do not trigger ingestion.

## Docker

With `.env` configured and the cloud schema created:

```sh
docker compose up --build
```

Compose forwards the Supabase connection settings and MCP authentication settings.
Compose uses Linux host networking for builds and running containers, avoiding
Docker bridge/veth requirements. The MCP server binds to `127.0.0.1:8000` on the
host; expose it through your HTTPS tunnel or reverse proxy.
It mounts `inbox/` and `archive/`; no application database volume is required.
MCP startup does not ingest the inbox; run local ingestion explicitly with
`just local-ingest` (or `just prod-ingest` on production). A malformed export
therefore cannot prevent the MCP server from starting. The server has a TCP
liveness healthcheck; this does not verify Supabase connectivity or data freshness.
Container logs rotate at 10 MB with three retained files per service.
To ingest a newly added local ZIP while the server is running:

```sh
docker compose exec server python -m server.ingest --source local --path /app/inbox
```

## Doppler: development and production

With [just](https://just.systems) installed, run `just local` on your development
machine or `just prod` on your production host. These select Doppler's `dev` and
`prd` configs explicitly and start both the MCP server and Drive worker.
Use `just local-refresh` or `just prod-refresh` after secret changes, `just logs`
to follow logs, and `just stop` to stop the stack on the current host.

Create a Doppler project named `strava-sync` with `dev` and `prd` configs.
Install the [Doppler CLI](https://docs.doppler.com/docs/install-cli) on your laptop
and production host. Run `doppler setup` to select `strava-sync` and `dev` locally;
the just recipes select their configs explicitly.

Set these secrets/settings in Doppler:

| Name | Value |
| --- | --- |
| `SUPABASE_DB_URL` | Supabase PostgreSQL connection URI |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Entire original service-account JSON object |
| `DRIVE_FOLDER_ID` | Shared Google Drive export folder ID |
| `MCP_PATH_SECRET` | Long random secret for the connector URL |
| `MCP_PUBLIC_URL` | Environment's HTTPS origin, without a path |
| `DRIVE_INGEST_TIME` | Optional; defaults to `06:00` |
| `DRIVE_INGEST_TIMEZONE` | Optional; defaults to `Asia/Kolkata` |
| `DRIVE_INGEST_TIMEOUT_S` | Optional; defaults to `1800` seconds |

Paste the service-account file's contents into Doppler's secret editor as JSON,
including the private key. Do not base64-encode it or wrap the whole object in
another JSON string. The application parses it and creates credentials in memory;
no credentials file is written or mounted. Compose passes this secret only to the
Drive worker, not the MCP server.

For the free-plan setup, save the same JSON value in `dev` and `prd`. In Doppler's
save dialog, apply the credential change to both environments so future key
rotations update both copies. Do the same for the folder ID if both environments
use the same exports. Database and MCP settings can be configured independently.
Cross-config references such as `${prd.GOOGLE_SERVICE_ACCOUNT_JSON}` require a
paid Doppler plan, so they are not needed for this setup.

### Local development

```sh
doppler login
doppler setup
doppler run -- .venv/bin/python -m server.store
doppler run -- .venv/bin/python -m server.ingest --source drive
doppler run -- .venv/bin/python -m server.mcp_server
```

Install `.[drive]` for Python Drive commands. For the full Compose deployment:

```sh
doppler run -- docker compose --profile drive up --build -d
```

Doppler injects environment variables; they take precedence over `.env`. Once
Doppler is configured, you do not need to keep secrets in `.env`.

### Production on a Docker Compose host

Use a small Linux VM with Docker Compose and the Doppler CLI. Create a read-only
Doppler **service token scoped to `prd`** and supply it as `DOPPLER_TOKEN` in the
deployment shell or your host's secret configuration. Do not put the token in
the repository, Docker image, or Compose file. The token is only needed by the
host CLI; Compose does not forward it into the application containers.

```sh
doppler run --project strava-sync --config prd -- docker compose --profile drive up --build -d
```

Compose restart policies keep the containers running after deployment and host
reboots when Docker starts automatically. Doppler is used at deployment time,
not on each MCP request or scheduled ingestion. After changing secrets, refresh
the containers explicitly:

```sh
doppler run --project strava-sync --config prd -- docker compose --profile drive up -d --force-recreate
```

The server still needs an externally reachable HTTPS tunnel or reverse proxy for
ChatGPT. Free VM availability depends on the provider; a continuously running VM
fits this daily worker better than hosting that sleeps when idle. Avoid running
both development and production workers continuously against the same database
unless you intend both to ingest.

## Google Drive

```sh
pip install -e '.[drive]'
```

Set `DRIVE_FOLDER_ID` and `GOOGLE_SERVICE_ACCOUNT_JSON` in Doppler (or `.env`).
For direct Python usage, `GOOGLE_APPLICATION_CREDENTIALS` can instead point to a
local JSON file. The JSON environment variable takes precedence when both are set. Enable the
Drive API and share the export folder with the service account's email as a viewer.

```sh
python -m server.ingest --source drive
```

Each invocation downloads at most one ZIP: the newest **unseen version**, ordered
by Drive modification time, tracking file ID + modifiedTime + MD5 in Supabase.
Repeated invocations may process older unseen snapshots after the latest one.
Drive access uses the read-only scope. Failed ingests remain eligible for retry.

### Daily automatic Drive ingestion

Configure the Drive settings above, then set the daily time in `.env`:

```dotenv
DRIVE_INGEST_TIME=06:00
DRIVE_INGEST_TIMEZONE=Asia/Kolkata
```

The time uses 24-hour `HH:MM`; the timezone is an IANA name. With Docker,
set `GOOGLE_SERVICE_ACCOUNT_JSON`; no host credentials file is needed.

Start the MCP server and the separate scheduled worker:

```sh
docker compose --profile drive up --build -d
docker compose logs -f drive-ingest
```

The worker waits until the next scheduled time, runs one Drive ingestion, and
logs the result plus the next scheduled time. It does not ingest immediately on
startup or catch up missed runs while the host was offline. Each run processes
at most one newest unseen ZIP. Failures are logged and retried at the next daily
run; the MCP server continues independently. Both services restart automatically
unless explicitly stopped, provided Docker is running. Archive copies accumulate
in the host's `archive/` directory; cleanup remains manual.
Drive HTTP calls have a 60-second timeout. Scheduled ingestion jobs are terminated
after `DRIVE_INGEST_TIMEOUT_S` (default 30 minutes); shutdown also terminates and
reaps an active child process. Export database extraction is limited to 1 GiB.

For an immediate ingestion using the worker's configuration:

```sh
docker compose --profile drive exec drive-ingest python -m server.ingest --source drive
```

After changing the schedule or credentials in `.env`, apply the changes with
`docker compose --profile drive up -d`. To run the scheduler without Docker:

```sh
.venv/bin/python -m server.scheduler
```

Keep that process running with your deployment's process manager. Scheduling
follows the chosen timezone's daylight-saving rules; nonexistent local times are
skipped and repeated local times run only at the first occurrence.

## Data handling

```text
Local folder / Google Drive ZIP → parse + normalize → Supabase PostgreSQL
                                                         ↑
                                                read-only MCP tools
```

- Downloads and extracted databases use temporary directories. Original ZIPs are
  retained in `archive/` by SHA-256 filename (`STRAVA_SYNC_ARCHIVE` overrides this path;
  the legacy `FITNESS_ARCHIVE` variable is also accepted).
  These are source files, not an application database. Identical ZIP content uses
  the same archive filename; source files are not automatically deleted.
- Health Connect's ZIP contains `health_connect_export.db`, so parsing still uses
  Python's SQLite reader with `mode=ro`. Only the named source database is extracted.
  All processed activities and ingestion state live exclusively in Supabase.
- Sessions join to distance by app ID and matching start/end milliseconds. Multiple
  exact-match distances use the newest row. Missing distance remains null.
- Activity key: `(strava, trailing activity ID)` for Strava, otherwise
  `(health_connect, sha1(package|start_ms|end_ms|type_code))`. Exercise codes remain
  raw integers in `exercise_type_code`.
- Updates require a newer modification time (maximum of session and matched
  distance). Full snapshots add/update records; disappearance does not delete
  history. Each file's activity writes are atomic; failures retain their run record.
- SQL uses a private `fitness` schema through PostgreSQL connections, rather than
  Supabase's public Data API. Schema setup requires create/ownership privileges.
  Ingestion and MCP use `SUPABASE_DB_URL`. MCP connections use read-only transactions.

## Tools and analytics

All five tools have `readOnlyHint=true`:

- `get_data_status()`
- `get_weekly_summary(week_ending?)`
- `compare_weeks(week_ending?, prior_weeks=4)`
- `list_activities(start_date, end_date)`
- `get_activity(source_id)`

Weeks use Asia/Kolkata activity start dates, Saturday through Friday inclusive.
`week_ending` must be a Friday; the default is the most recent Friday, including
today if Friday. Missing source offset defaults to +05:30.

Pace is measured time / distance; speed is distance / measured time. Activities
without distance, with zero distance, or with zero duration are excluded from rate
calculations. Totals include all exercise types; they are not a running-only filter.
Comparison responses explain
averaging and null-baseline rules. Every response includes `data_available`,
`last_ingest_utc`, and `limitations`; comparison availability describes the requested
week. Successful distinct snapshots determine freshness, and checksum skips do not
reset it. More than 36 hours triggers a staleness warning.

Empty dates mean **no activity recorded**, not evidence of inactivity. The source
has no heart rate, laps, or route, so interval quality cannot be assessed.

## Connect Inspector / ChatGPT

With the server running, launch Inspector (requires Node.js):

```sh
npx @modelcontextprotocol/inspector
```

Choose **Streamable HTTP**, enter `http://127.0.0.1:8000/mcp`, connect, and list tools.
For the sample, use week ending `2026-11-27`, activity ID `21078195405`, or date range
`2026-11-22`–`2026-11-22`. Expected: 6,068.2 m, 2,951 s, approximately 8:06/km.

ChatGPT needs an externally reachable HTTPS endpoint. Run `ngrok http 8000` and set
`MCP_PUBLIC_URL=https://your-host.ngrok-free.dev` in `.env`, without `/mcp` or a secret.
Restart the server whenever the tunnel hostname changes. Host/Origin validation
permits this configured public origin and localhost.

For personal ChatGPT access, set `MCP_PATH_SECRET` to a long URL-safe random string.
The server requires it whenever `MCP_PUBLIC_URL` is configured.
The endpoint becomes `/mcp/<secret>`. Generate a secret with:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Restart the server after setting the secret. For ChatGPT developer mode's custom
app/connector (where available), use `https://your-host.ngrok-free.dev/mcp/<secret>`
and select **no authentication**. Access is controlled by knowing the secret URL;
no bearer header is required. Keep the full connector URL private.

For local-only usage, leave `MCP_PATH_SECRET` empty and connect Inspector to
`http://127.0.0.1:8000/mcp`. If a secret is set, Inspector uses `/mcp/<secret>` too.
Application HTTP access logging is disabled so secret paths are not logged.

## Tests

```sh
pytest -q
```

Tests start a disposable PostgreSQL 17 Docker container automatically (host networking,
random loopback-only port) and remove it afterward. They cover ingestion, deduplication, newer-only updates, rollback,
analytics, read-only connections, and all five MCP tools over real HTTP, including
secret-path access and public-host validation. Docker with host networking must be available
(native on Linux; on Docker Desktop it must be enabled).

Alternatively supply a dedicated PostgreSQL test database:

```sh
TEST_SUPABASE_DB_URL='postgresql://USER:PASSWORD@localhost:5432/fitness_test' pytest -q
```

The test database name must end in `_test`; its `fitness` tables are truncated.
Tests do not use the real `.env` database credentials.

Tests generate a synthetic Health Connect SQLite export inside a ZIP. No private
archived sample, real Google credentials, or Doppler account is required.

## Production operations

Runtime dependency versions are constrained by `requirements.lock` in Docker and
the development install command. Update the constraints deliberately and rerun
`just test` and the Docker build before deploying dependency upgrades.

The Compose deployment targets a Linux host with host networking. MCP listens only
on loopback port 8000; run your HTTPS tunnel or reverse proxy on that host. Configure
the production HTTPS origin and a long `MCP_PATH_SECRET` in Doppler. Keep proxy
access logs from recording the secret URL. Doppler-injected environment secrets are
visible to administrators with Docker access; no credentials are baked into images.

Create the Supabase schema once before first deployment. Use a PostgreSQL connection
URI with TLS (`sslmode=require`, or `verify-full` with a trusted CA), and enable
backups appropriate to your data. Keep `fitness` outside Supabase's exposed API
schemas. Archive ZIPs accumulate on the host: retain a backup if needed and monitor
disk usage. The archive is not a replacement for a database backup.

After deployment, check `docker compose ps` and `just logs`, run an immediate Drive
ingestion, and call `get_data_status` from the connector. The worker log reports its
next scheduled run; TCP health alone does not establish fresh data. A real Drive
fetch and the chosen host's HTTPS/Doppler setup must be verified in that environment.
