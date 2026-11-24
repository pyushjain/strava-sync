import asyncio
import os
import socket
import subprocess
import sys
import time

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
