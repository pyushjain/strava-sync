import asyncio
import os
import socket
import subprocess
import sys
import time
from unittest.mock import patch

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


@pytest.fixture(params=[False, True], ids=["local", "secret-path"])
def running_server(ingested, request):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "MCP_PORT": str(port),
           "MCP_PUBLIC_URL": "https://fitness-test.ngrok-free.dev" if request.param else "",
           "MCP_HOST": "127.0.0.1", "MCP_PATH_SECRET": ""}
    if request.param:
        env.update(MCP_PATH_SECRET="test-secret")
    env.pop("PORT", None)
    process = subprocess.Popen([sys.executable, "-m", "server.mcp_server"], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("MCP server exited before accepting connections")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("MCP server did not start")
        yield base, request.param
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_all_tools_over_streamable_http(running_server):
    base, secret_path = running_server
    url = base + ("/mcp/test-secret" if secret_path else "/mcp")
    if secret_path:
        assert httpx.post(base + "/mcp").status_code == 404
        assert httpx.post(base + "/mcp/wrong-secret").status_code == 404

    async def check():
        async with httpx.AsyncClient() as client:
            async with streamable_http_client(url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert "not the full Strava API" in initialized.instructions
                    tools = (await session.list_tools()).tools
                    calls = {
                        "get_data_status": {},
                        "get_weekly_summary": {"week_ending": "2026-11-27"},
                        "compare_weeks": {"week_ending": "2026-11-27"},
                        "list_activities": {"start_date": "2026-11-22", "end_date": "2026-11-22"},
                        "get_activity": {"source_id": "21078195405"},
                    }
                    assert {t.name for t in tools} == set(calls)
                    assert all(t.annotations.readOnlyHint for t in tools)
                    assert all("heart rate" in " ".join(t.description.split()) for t in tools)
                    for name, arguments in calls.items():
                        response = await session.call_tool(name, arguments)
                        assert not response.isError
                        data = response.structuredContent
                        assert data["data_available"] is True
                        assert data["last_ingest_utc"]
                        assert isinstance(data["limitations"], list)
                        if name == "get_data_status":
                            assert data["total_activities"] == 1

    asyncio.run(check())


def test_public_host_and_origin_allowlist(running_server):
    base, secret_path = running_server
    url = base + ("/mcp/test-secret" if secret_path else "/mcp")
    headers = {"Host": "fitness-test.ngrok-free.dev",
               "Origin": "https://fitness-test.ngrok-free.dev",
               "Accept": "application/json, text/event-stream"}
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "tunnel-test", "version": "1"},
    }}
    response = httpx.post(url, headers=headers, json=request)
    if not secret_path:
        assert response.status_code == 421
        return
    assert response.status_code == 200
    assert "result" in response.json()
    assert httpx.post(url, headers={**headers, "Host": "untrusted.example"}, json=request).status_code == 421
    assert httpx.post(url, headers={**headers, "Origin": "https://untrusted.example"}, json=request).status_code == 403


def test_public_endpoint_requires_secret(monkeypatch):
    from server.mcp_server import create_server

    monkeypatch.setenv("MCP_PUBLIC_URL", "https://fitness-test.ngrok-free.dev")
    monkeypatch.setenv("MCP_PATH_SECRET", "")
    with pytest.raises(ValueError, match="MCP_PATH_SECRET is required"):
        create_server()


@pytest.mark.parametrize("secret", ["", "test-secret"])
def test_health_without_database_or_authentication(monkeypatch, secret):
    from server.mcp_server import create_app

    monkeypatch.setenv("SUPABASE_DB_URL", "")
    monkeypatch.setenv("MCP_PATH_SECRET", secret)
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://health-test.onrender.com" if secret else "")
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),
                                     base_url="https://health-test.onrender.com") as client:
            response = await client.get("/healthz")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            if secret:
                assert (await client.post("/mcp")).status_code == 404
                assert (await client.post("/mcp/wrong-secret")).status_code == 404

    with patch("server.store.connect", side_effect=AssertionError("Health must not query the database")):
        asyncio.run(check())


def test_http_startup_without_supabase_credentials(monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "PORT": str(port), "MCP_PORT": "0",
           "SUPABASE_DB_URL": "", "MCP_HOST": "127.0.0.1",
           "MCP_PUBLIC_URL": "https://health-test.onrender.com", "MCP_PATH_SECRET": "test-secret"}
    process = subprocess.Popen([sys.executable, "-m", "server.mcp_server"], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("Server exited without Supabase credentials")
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5)
            except httpx.TransportError:
                time.sleep(0.1)
                continue
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            break
        else:
            pytest.fail("Health endpoint did not become ready")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_health_over_http(running_server):
    base, _ = running_server
    response = httpx.get(base + "/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("render_port,local_port,expected", [
    ("10000", "8001", 10000), (None, "8001", 8001), (None, None, 8000),
])
def test_startup_port_and_database_independence(monkeypatch, render_port, local_port, expected):
    from server.mcp_server import main

    for name, value in [("PORT", render_port), ("MCP_PORT", local_port)]:
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.delenv("MCP_HOST", raising=False)
    monkeypatch.setenv("MCP_PUBLIC_URL", "")
    monkeypatch.setenv("MCP_PATH_SECRET", "")
    with patch("server.mcp_server.load_environment"), patch("server.mcp_server.uvicorn.run") as run:
        main()
    assert run.call_args.kwargs == {
        "host": "0.0.0.0", "port": expected, "access_log": False, "log_level": "warning",
    }


def test_startup_host_override(monkeypatch):
    from server.mcp_server import main

    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("MCP_PUBLIC_URL", "")
    with patch("server.mcp_server.load_environment"), patch("server.mcp_server.uvicorn.run") as run:
        main()
    assert run.call_args.kwargs["host"] == "127.0.0.1"


@pytest.mark.parametrize("port", ["0", "65536", "invalid", ""])
def test_invalid_render_port(monkeypatch, port):
    from server.mcp_server import main

    monkeypatch.setenv("PORT", port)
    monkeypatch.setenv("MCP_PORT", "8000")
    monkeypatch.setenv("MCP_PUBLIC_URL", "")
    with patch("server.mcp_server.load_environment"), pytest.raises(SystemExit, match="PORT must be"):
        main()
