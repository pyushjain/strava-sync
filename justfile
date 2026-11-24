# Generate a random value to store as MCP_PATH_SECRET in Doppler.
path-secret:
    python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Run the local stack using Doppler's development config.
local:
    doppler run --project strava-sync --config dev -- docker compose --profile drive up --build -d

# Run the production stack (set DOPPLER_TOKEN on the production host).
prod:
    doppler run --project strava-sync --config prd -- docker compose --profile drive up --build -d

# Apply updated secrets to each stack.
local-refresh:
    doppler run --project strava-sync --config dev -- docker compose --profile drive up -d --force-recreate

prod-refresh:
    doppler run --project strava-sync --config prd -- docker compose --profile drive up -d --force-recreate

logs:
    docker compose --profile drive logs -f

stop:
    docker compose --profile drive down

# Run the complete suite with a disposable PostgreSQL container.
test:
    .venv/bin/python -m pytest -q

# Explicit local ingestion; a bad inbox export never blocks MCP startup.
local-ingest:
    doppler run --project strava-sync --config dev -- docker compose exec server python -m server.ingest --source local --path /app/inbox

prod-ingest:
    doppler run --project strava-sync --config prd -- docker compose exec server python -m server.ingest --source local --path /app/inbox
