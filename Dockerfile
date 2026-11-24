FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
COPY requirements.lock ./
COPY server ./server
RUN pip install --no-cache-dir -c requirements.lock '.[drive]'
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV MCP_HOST=0.0.0.0 STRAVA_SYNC_ARCHIVE=/app/archive
EXPOSE 8000
CMD ["python", "-m", "server.mcp_server"]
