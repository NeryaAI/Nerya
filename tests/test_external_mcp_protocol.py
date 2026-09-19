"""Official SDK wire tests. HTTP uses ASGI memory transport, never a public port."""
from __future__ import annotations

import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp", reason="Install nerya[mcp] to run protocol tests")
from starlette.testclient import TestClient

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.mcp.catalog import ExposedTool, ToolCatalog
from nerya.mcp.server import create_http_app, create_server
from nerya.mcp.settings import ServerSettings
from nerya.mcp.tools import NeryaTools

pytestmark = pytest.mark.smoke
TOKEN = "protocol-test-token-" + "x" * 32


@pytest.fixture
def http_client(tmp_path):
    data = deepcopy(DEFAULT_CONFIG)
    data["mcp"]["enabled"] = True
    config = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    tools = NeryaTools(client=SimpleNamespace(config=config))
    catalog = ToolCatalog()
    catalog.add(ExposedTool("echo", "Echo a number", {
        "type": "object", "properties": {"number": {"type": "integer"}},
        "required": ["number"], "additionalProperties": False,
    }, True, lambda number: {"number": number}))
    settings = ServerSettings("streamable-http", "127.0.0.1", 8765,
                              ["testserver"], ["https://trusted.example"], TOKEN)
    app = create_http_app(create_server(tools, catalog=catalog), settings)
    with TestClient(app) as client:
        yield client, catalog


def rpc(client, method, params=None, *, extra_headers=None):
    headers = {"Authorization": "Bearer " + TOKEN,
               "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-11-25"}
    headers.update(extra_headers or {})
    return client.post("/mcp", headers=headers,
                       json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_http_initialize_discover_and_call_share_exact_schema(http_client):
    client, catalog = http_client
    init = rpc(client, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                      "clientInfo": {"name": "integration-test", "version": "1"}})
    assert init.status_code == 200, init.text
    assert init.json()["result"]["serverInfo"]["name"] == "nerya"
    listed = rpc(client, "tools/list")
    assert listed.status_code == 200, listed.text
    schema = listed.json()["result"]["tools"][0]["inputSchema"]
    assert schema == catalog.describe("echo")["inputSchema"]
    result = rpc(client, "tools/call", {"name": "echo", "arguments": {"number": 7}})
    assert result.status_code == 200, result.text
    body = result.json()["result"]
    assert body["structuredContent"] == {"number": 7}
    assert json.loads(body["content"][0]["text"]) == body["structuredContent"]
    assert body["isError"] is False


@pytest.mark.parametrize("params,code", [
    ({"name": "echo", "arguments": {}}, "invalid_arguments"),
    ({"name": "echo", "arguments": {"number": "secret-input"}}, "invalid_arguments"),
    ({"name": "missing", "arguments": {}}, "not_found"),
])
def test_http_tool_failures_have_iserror_and_structured_error(http_client, params, code):
    response = rpc(http_client[0], "tools/call", params)
    assert response.status_code == 200, response.text
    body = response.json()["result"]
    assert body["isError"] is True
    assert body["structuredContent"]["error"]["code"] == code
    assert "secret-input" not in response.text


@pytest.mark.parametrize("method", ["get", "post", "delete"])
def test_all_http_methods_require_authentication(http_client, method):
    client, _ = http_client
    assert getattr(client, method)("/mcp").status_code == 401
    assert getattr(client, method)("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/mcp", headers=[("Authorization", "Bearer " + TOKEN),
                                       ("Authorization", "Bearer wrong")]).status_code == 401


def test_http_rejects_dns_rebinding_and_untrusted_origins(http_client):
    client, _ = http_client
    bad_host = rpc(client, "tools/list", extra_headers={"Host": "untrusted.example"})
    assert bad_host.status_code == 421, bad_host.text
    bad_origin = rpc(client, "tools/list", extra_headers={"Origin": "https://untrusted.example"})
    assert bad_origin.status_code == 403, bad_origin.text
    good_origin = rpc(client, "tools/list", extra_headers={"Origin": "https://trusted.example"})
    assert good_origin.status_code == 200, good_origin.text


@pytest.mark.asyncio
async def test_stdio_real_process_initialize_list_call_and_clean_shutdown(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    (tmp_path / "nerya.yml").write_text(
        "mcp:\n  enabled: true\n  native_tools:\n    enabled: false\n"
        "  dynamic_tools:\n    enabled: false\n", encoding="utf-8",
    )
    params = StdioServerParameters(
        command=str(Path(sys.executable).with_name("nerya")),
        args=["mcp", "serve", "--workspace", str(tmp_path)],
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1]),
             "NERYA_VAULT_PASSPHRASE": "isolated-mcp-test-passphrase"},
    )
    async def exercise():
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "nerya"
                listed = await session.list_tools()
                assert any(t.name == "nerya_config_get" for t in listed.tools)
                result = await session.call_tool("nerya_config_get", {"target": "agents.yml"})
                assert result.isError is False
                assert result.structuredContent["target"] == "agents.yml"
                invalid = await session.call_tool("nerya_market_ticker", {})
                assert invalid.isError is True
    await asyncio.wait_for(exercise(), timeout=20)


def test_http_dispatches_real_agent_and_config_handlers(tmp_path):
    tools = NeryaTools.boot(tmp_path)
    tools.client.config.data["mcp"]["enabled"] = True
    tools.client.config.data["mcp"]["native_tools"].update(allow_mutating=True, mode="auto")
    settings = ServerSettings("streamable-http", "127.0.0.1", 8765, ["testserver"], [], TOKEN)
    with TestClient(create_http_app(create_server(tools), settings)) as client:
        for name, arguments in (
            ("nerya_native_role_save", {"name": "http_researcher", "prompt": "Research only."}),
            ("nerya_native_role_get", {"name": "http_researcher"}),
            ("nerya_native_evolve_core_config_patch", {
                "target": "agents.yml", "summary": "HTTP proposal", "config_after": {"max_parallel": 2},
            }),
        ):
            response = rpc(client, "tools/call", {"name": name, "arguments": arguments})
            assert response.status_code == 200, response.text
            assert response.json()["result"]["isError"] is False, response.text
    assert (tmp_path / "subagents" / "http_researcher.agent.md").exists()
    assert not (tmp_path / "agents.yml").exists()
    assert list((tmp_path / "evolution" / "proposals").glob("*/after/agents.yml"))
