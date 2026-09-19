"""Public endpoint tests use loopback sockets and an isolated workspace only."""
import threading
from copy import deepcopy

import httpx
import pytest

from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.api.auth import set_admin_password
from nerya.api.local_server import build_server
from nerya.mcp import openai_tunnel

pytestmark = pytest.mark.smoke


def configured(tmp_path):
    cfg = Config(paths=WorkspacePaths(tmp_path), data=deepcopy(DEFAULT_CONFIG))
    set_admin_password(cfg, "temporary-admin-test-password")
    cfg.data["mcp"].update(enabled=True, public_url="https://public.example")
    yaml_io.dump(cfg.paths.config, cfg.data)
    return cfg


def test_integrated_listener_keeps_oauth_and_rest_auth_separate(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from nerya.mcp.oauth import AdminOAuthProvider
    from nerya.mcp.bridge import _RUNNERS
    cfg = configured(tmp_path)
    monkeypatch.setenv("NERYA_DISABLE_TUNNEL_RESTORE", "1")
    server = build_server(cfg, port=0, start_cron=False, start_continuous=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with httpx.Client(base_url=url, trust_env=False, timeout=30) as client:
            response = client.get("/mcp")
            assert response.status_code == 401, response.text
            assert "https://public.example/.well-known/oauth-protected-resource/mcp" in response.headers["WWW-Authenticate"]
            metadata = client.get("/.well-known/oauth-authorization-server/mcp/oauth")
            assert metadata.status_code == 200, metadata.text
            assert metadata.json()["issuer"] == "https://public.example/mcp/oauth"
            provider = AdminOAuthProvider(cfg, "https://public.example")
            tokens = provider.issue("isolated-test-client", ["mcp"])
            listed = client.post("/mcp", headers={"Authorization": "Bearer " + tokens.access_token,
                "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            assert listed.status_code == 200, listed.text
            assert any(t["name"] == "nerya_skill_read" for t in listed.json()["result"]["tools"])
            assert client.post("/mcp", headers={"Authorization": "Bearer dashboard-token"}).status_code == 401
            raw = yaml_io.load(cfg.paths.config)
            raw["mcp"]["enabled"] = False
            yaml_io.dump(cfg.paths.config, raw)
            assert client.get("/mcp").status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert str(cfg.paths.root.resolve()) not in _RUNNERS


def test_disabled_bridge_does_not_require_optional_sdk(tmp_path):
    cfg = Config(paths=WorkspacePaths(tmp_path), data=deepcopy(DEFAULT_CONFIG))
    from nerya.mcp.bridge import _runner
    assert _runner(cfg, 18317) is None


def test_openai_tunnel_uses_runtime_key_environment_not_arguments(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from nerya.security.secrets import SecretVault
    cfg = configured(tmp_path)
    key = "runtime-key-for-isolated-test"
    SecretVault.open(cfg.paths.vault_enc).put(name="test_tunnel", value=key, kind="test", scope=["mcp_tunnel"])
    cfg.data["mcp"]["openai_tunnel"] = {"enabled": True, "tunnel_id": "tunnel_test12345678", "api_key_ref": "vault://test_tunnel"}
    calls = []
    class Process:
        exit = None
        def poll(self): return self.exit
        def terminate(self): self.exit = 0
        def wait(self, timeout): return 0
    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return Process()
    monkeypatch.setattr(openai_tunnel, "executable", lambda: "/test/bin/tunnel-client")
    monkeypatch.setattr(openai_tunnel.subprocess, "Popen", popen)
    result = openai_tunnel.start(cfg, api_port=19317)
    assert result["ok"] and result["running"] and not result["ready"], result
    command, kwargs = calls[0]
    assert command[:4] == ["/test/bin/tunnel-client", "run", "--mcp.server-url", "http://127.0.0.1:19317/mcp"]
    assert key not in " ".join(command)
    assert kwargs["env"]["CONTROL_PLANE_API_KEY"] == key
    assert kwargs["env"]["CONTROL_PLANE_TUNNEL_ID"] == "tunnel_test12345678"
    assert kwargs["env"]["MCP_OAUTH_TRUSTED_ORIGINS"] == "https://public.example"
    assert not any("AUTHORIZATION" in k or "ADMIN" in k for k in kwargs["env"])
    assert openai_tunnel.start(cfg)["running"] and len(calls) == 1
    assert not openai_tunnel.stop(cfg)["running"]
    cfg.data["mcp"]["public_url"] = "http://localhost:18380"
    assert not openai_tunnel.start(cfg)["ok"]


def test_mcp_management_routes_require_admin_scope():
    from nerya.api.route_scopes import required_scope
    assert required_scope("GET", "/mcp-settings") == "admin:ops"
    assert required_scope("POST", "/mcp-settings/tunnel/start") == "admin:ops"
    assert required_scope("POST", "/mcp-settings/revoke") == "admin:ops"
