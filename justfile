# Generate a random value to store as MCP_PATH_SECRET.
path-secret:
    python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Run the local MCP server using .env.
local:
    docker compose up --build -d

# Apply updated local settings.
local-refresh:
    docker compose up -d --force-recreate

logs:
    docker compose --profile drive logs -f

stop:
    docker compose --profile drive down

# Run the complete suite with a disposable PostgreSQL container.
test:
    .venv/bin/python -m pytest -q

# Explicit local ingestion; a bad inbox export never blocks MCP startup.
local-ingest:
    docker compose exec server python -m server.ingest --source local --path /app/inbox
