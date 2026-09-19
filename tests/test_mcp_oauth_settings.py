"""MCP OAuth uses real SDK handlers and the existing administrator password."""
import base64
import hashlib
import html
import json
import re
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("mcp")
from starlette.testclient import TestClient
from nerya.api.auth import set_admin_password
from nerya.api import routes_mcp
from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.mcp.server import create_http_app, create_server
from nerya.mcp.settings import ServerSettings
from nerya.mcp.tools import NeryaTools

pytestmark = pytest.mark.smoke
ORIGIN = "https://nerya.example"
RESOURCE = ORIGIN + "/mcp"
PASSWORD = "local-test-administrator-password"
VERIFIER = "verifier-" + "x" * 50
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("NERYA_USER_SKILLS_ROOT", str(tmp_path / "no-home-skills"))
    cfg = Config(paths=WorkspacePaths(tmp_path), data=deepcopy(DEFAULT_CONFIG))
    set_admin_password(cfg, PASSWORD)
    cfg.data["mcp"].update(enabled=True, public_url=ORIGIN)
    yaml_io.dump(cfg.paths.config, cfg.data)
    tools = NeryaTools.boot(tmp_path)
    settings = ServerSettings("streamable-http", "127.0.0.1", 18317,
        ["nerya.example", "127.0.0.1:18317"], [ORIGIN], auth_mode="oauth2", public_origin=ORIGIN)
    app = create_http_app(create_server(tools), settings, config=tools.client.config)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        yield client, tools, app.provider


def register(client, **overrides):
    response = client.post("/mcp/oauth/register", json={
        "client_name": "External test agent", "redirect_uris": ["https://client.example/callback"],
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "scope": "mcp", **overrides})
    return response


def consent(client, client_id):
    response = client.get("/mcp/oauth/authorize", params={"client_id": client_id,
        "redirect_uri": "https://client.example/callback", "response_type": "code", "scope": "mcp",
        "state": "state-roundtrip", "code_challenge": CHALLENGE, "code_challenge_method": "S256",
        "resource": RESOURCE})
    assert response.status_code in {302, 303}, response.text
    page = client.get(response.headers["location"])
    assert page.status_code == 200, page.text
    fields = {name: html.unescape(re.search(fr'name="{name}" value="([^"]+)"', page.text)[1])
              for name in ("ticket", "csrf")}
    return fields


def auth_code(client, client_id):
    fields = consent(client, client_id)
    response = client.post("/mcp/oauth/login", data={**fields, "password": PASSWORD, "decision": "allow"})
    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == ["state-roundtrip"]
    return query["code"][0]


def exchange(client, client_id, code, **overrides):
    return client.post("/mcp/oauth/token", data={"client_id": client_id, "grant_type": "authorization_code",
        "code": code, "code_verifier": VERIFIER, "redirect_uri": "https://client.example/callback",
        "resource": RESOURCE, **overrides})


def rpc(client, token=""):
    return client.post("/mcp", headers={"Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})


def test_discovery_and_complete_oauth_pkce_flow(configured):
    client, tools, provider = configured
    response = rpc(client)
    assert response.status_code == 401
    assert ORIGIN + "/.well-known/oauth-protected-resource/mcp" in response.headers["WWW-Authenticate"]
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == RESOURCE
    issuer = prm["authorization_servers"][0]
    metadata = client.get("/.well-known/oauth-authorization-server/mcp/oauth").json()
    assert metadata["issuer"] == issuer
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    registered = register(client)
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    assert "client_secret" not in registered.json() or registered.json()["client_secret"] is None
    code = auth_code(client, client_id)
    result = exchange(client, client_id, code)
    assert result.status_code == 200, result.text
    token = result.json()["access_token"]
    listed = rpc(client, token)
    assert listed.status_code == 200, listed.text
    assert any(t["name"] == "nerya_skills_catalog" for t in listed.json()["result"]["tools"])
    assert exchange(client, client_id, code).status_code == 400
    raw = provider.path.read_bytes()
    assert all(value.encode() not in raw for value in (PASSWORD, token, code, result.json()["refresh_token"]))
    assert rpc(client, "dashboard-jwt-is-not-an-oauth-token").status_code == 401


def test_pkce_resource_and_redirect_binding(configured):
    client, _, _ = configured
    cid = register(client).json()["client_id"]
    code = auth_code(client, cid)
    assert exchange(client, cid, code, code_verifier="wrong" * 10).status_code == 400
    assert exchange(client, cid, code, resource="https://evil.example/mcp").status_code == 400
    assert exchange(client, cid, code, redirect_uri="https://evil.example/callback").status_code == 400
    assert exchange(client, cid, code).status_code == 200
    assert register(client, redirect_uris=["https://client.example/callback#fragment"]).status_code == 400
    assert register(client, redirect_uris=["http://evil.example/callback"]).status_code == 400
    assert register(client, token_endpoint_auth_method="client_secret_post").status_code == 400


def test_consent_csrf_wrong_password_and_denial(configured):
    client, _, _ = configured
    cid = register(client).json()["client_id"]
    fields = consent(client, cid)
    invalid = client.post("/mcp/oauth/login", data={**fields, "csrf": "wrong", "password": PASSWORD, "decision": "allow"})
    assert invalid.status_code == 403
    wrong = client.post("/mcp/oauth/login", data={**fields, "password": "incorrect", "decision": "allow"})
    assert wrong.status_code == 401
    assert PASSWORD not in wrong.text and "Content-Security-Policy" in wrong.headers
    fields = consent(client, cid)
    response = client.post("/mcp/oauth/login", data={**fields, "password": PASSWORD, "decision": "deny"})
    assert response.status_code == 303
    assert "access_denied" in response.headers["location"] and "code=" not in response.headers["location"]


def test_refresh_rotation_replay_revoke_and_disable(configured):
    client, tools, provider = configured
    cid = register(client).json()["client_id"]
    tokens = exchange(client, cid, auth_code(client, cid)).json()
    def refresh(value):
        return client.post("/mcp/oauth/token", data={"client_id": cid, "grant_type": "refresh_token",
            "refresh_token": value, "resource": RESOURCE})
    rotated = refresh(tokens["refresh_token"])
    assert rotated.status_code == 200, rotated.text
    assert rpc(client, tokens["access_token"]).status_code == 401
    assert rpc(client, rotated.json()["access_token"]).status_code == 200
    assert refresh(tokens["refresh_token"]).status_code == 400
    assert rpc(client, rotated.json()["access_token"]).status_code == 401
    fresh = exchange(client, cid, auth_code(client, cid)).json()
    rev = routes_mcp.revoke(tools.client, {"revision": routes_mcp.status(tools.client)["revision"]})
    assert rev["ok"]
    assert rpc(client, fresh["access_token"]).status_code == 401
    new = exchange(client, cid, auth_code(client, cid)).json()
    set_admin_password(tools.client.config, "different-administrator-password")
    assert rpc(client, new["access_token"]).status_code == 401
    raw = yaml_io.load(tools.client.config.paths.config)
    raw["mcp"]["enabled"] = False
    yaml_io.dump(tools.client.config.paths.config, raw)
    assert rpc(client).status_code == 404


def test_settings_revision_vault_and_no_secret_disclosure(configured, monkeypatch):
    _, tools, _ = configured
    config = tools.client.config
    before = routes_mcp.status(tools.client)
    assert routes_mcp.save(tools.client, {"revision": "stale", "enabled": False})["_status"] == 409
    monkeypatch.setattr("nerya.mcp.openai_tunnel.stop", lambda *a: {"ok": True})
    key = "sk-runtime-secret-" + "x" * 36
    result = routes_mcp.save(tools.client, {"revision": before["revision"],
        "openai_tunnel": {"tunnel_id": "tunnel_abcdefgh1234", "api_key": key}})
    assert result["ok"], result
    assert result["openai_tunnel"]["api_key_configured"]
    assert key not in json.dumps(result) and key not in config.paths.config.read_text()
    from nerya.security.secrets import SecretVault
    assert SecretVault.open(config.paths.vault_enc).resolve("mcp_openai_runtime_key", required_scope="mcp_tunnel") == key
    assert routes_mcp.save(tools.client, {"revision": result["revision"], "public_url": "http://evil.example"})["_status"] == 400
