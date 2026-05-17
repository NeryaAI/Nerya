from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import route_scopes, routes_network
from nerya.core import tunnels
from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core.tunnels import (
    public_tunnel_status,
    restore_configured_tunnels_on_start,
    save_tunnel_config,
    start_tunnel,
    stop_tunnel,
)


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def _routes():
    return {(method, path): handler for method, path, handler in routes_network.routes()}


def test_tunnel_status_lists_supported_providers_without_installing(tmp_path):
    cfg = _config(tmp_path)

    status = public_tunnel_status(cfg)

    assert status["ok"] is True
    assert [row["spec"]["id"] for row in status["providers"]] == [
        "tailscale",
        "cloudflare",
        "zrok",
        "ngrok",
    ]
    assert all(row["config"]["enabled"] is False for row in status["providers"])
    assert status["auth"]["dashboard_target"] == "http://127.0.0.1:18380"


def test_tunnel_config_vaults_provider_token(tmp_path):
    cfg = _config(tmp_path)

    out = save_tunnel_config(
        cfg,
        {
            "provider": "ngrok",
            "enabled": True,
            "target": "dashboard",
            "token": "ngrok-secret-token",
        },
    )

    assert out["ok"] is True
    saved = yaml_io.load(tmp_path / "nerya.yml", default={})
    provider = saved["network"]["tunnels"]["providers"]["ngrok"]
    assert provider["enabled"] is True
    assert provider["target"] == "dashboard"
    assert provider["token_ref"].startswith("vault://network_tunnel_ngrok_token_")
    assert "ngrok-secret-token" not in str(saved)


def test_dashboard_port_config_controls_dashboard_tunnel_target(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.data.setdefault("runtime", {})["auth"] = {
        "admin_password_hash": "pbkdf2_sha256$1$2$abc",
        "mode": "local",
    }
    routes = _routes()
    client = SimpleNamespace(config=cfg)

    dashboard = routes[("POST", "/network/dashboard")](client, {"port": 19000})

    assert dashboard["ok"] is True
    assert dashboard["config"]["url"] == "http://127.0.0.1:19000"
    saved = yaml_io.load(tmp_path / "nerya.yml", default={})
    assert saved["dashboard"]["port"] == 19000
    status = routes[("GET", "/network/tunnels")](client, {})
    assert status["auth"]["dashboard_target"] == "http://127.0.0.1:19000"

    save_tunnel_config(cfg, {"provider": "ngrok", "enabled": True, "target": "dashboard"})
    monkeypatch.setattr(tunnels, "executable_path", lambda _paths, _provider: "ngrok")
    monkeypatch.setattr(tunnels, "_is_pid_running", lambda _pid: True)

    class FakePopen:
        pid = 12345

        def __init__(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(tunnels.subprocess, "Popen", FakePopen)

    started = start_tunnel(cfg, "ngrok")

    assert started["ok"] is True
    assert started["state"]["target_url"] == "http://127.0.0.1:19000"
    assert started["state"]["command"] == ["ngrok", "http", "http://127.0.0.1:19000"]


def test_tunnel_install_is_explicitly_operator_approved(tmp_path):
    cfg = _config(tmp_path)
    routes = _routes()
    client = SimpleNamespace(config=cfg)

    out = routes[("POST", "/network/tunnels/install")](
        client,
        {"provider": "ngrok"},
    )

    assert out["ok"] is False
    assert out["error"] == "operator_approval_required"


def test_start_requires_admin_password_before_public_exposure(tmp_path):
    cfg = _config(tmp_path)
    save_tunnel_config(
        cfg,
        {"provider": "ngrok", "enabled": True, "target": "dashboard"},
    )

    out = start_tunnel(cfg, "ngrok")

    assert out["ok"] is False
    assert out["error"] == "admin_password_required"


def test_direct_api_tunnel_requires_token_auth_mode(tmp_path):
    cfg = _config(tmp_path)
    cfg.data.setdefault("runtime", {})["auth"] = {
        "admin_password_hash": "pbkdf2_sha256$1$2$abc",
        "mode": "local",
    }
    save_tunnel_config(
        cfg,
        {"provider": "ngrok", "enabled": True, "target": "api"},
    )

    out = start_tunnel(cfg, "ngrok")

    assert out["ok"] is False
    assert out["error"] == "api_tunnel_requires_token_auth"


def test_tailscale_start_reports_not_ready_when_client_is_not_logged_in(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.data.setdefault("runtime", {})["auth"] = {
        "admin_password_hash": "pbkdf2_sha256$1$2$abc",
        "mode": "local",
    }
    save_tunnel_config(
        cfg,
        {"provider": "tailscale", "enabled": True, "target": "dashboard", "mode": "funnel"},
    )
    monkeypatch.setattr(tunnels, "executable_path", lambda _paths, _provider: "tailscale")
    monkeypatch.setattr(tunnels, "_tailscale_status", lambda _paths: {"BackendState": "NoState", "AuthURL": ""})

    out = start_tunnel(cfg, "tailscale")

    assert out["ok"] is False
    assert out["error"] == "tailscale_not_ready"
    assert out["tailscale"]["backend_state"] == "NoState"
    assert out["tailscale"]["login_command"] == "tailscale up"


def test_tailscale_start_returns_external_urls(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.data.setdefault("runtime", {})["auth"] = {
        "admin_password_hash": "pbkdf2_sha256$1$2$abc",
        "mode": "local",
    }
    save_tunnel_config(
        cfg,
        {"provider": "tailscale", "enabled": True, "target": "dashboard", "mode": "funnel"},
    )
    monkeypatch.setattr(tunnels, "executable_path", lambda _paths, _provider: "tailscale")
    monkeypatch.setattr(tunnels, "_tailscale_status", lambda _paths: {"BackendState": "Running"})
    monkeypatch.setattr(tunnels, "_tailscale_external_urls", lambda _paths, _mode="funnel": ["https://demo.tailnet.ts.net"])

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tunnels.subprocess, "run", fake_run)

    out = start_tunnel(cfg, "tailscale")

    assert out["ok"] is True
    assert out["external_urls"] == ["https://demo.tailnet.ts.net"]
    assert out["state"]["external_urls"] == ["https://demo.tailnet.ts.net"]


def test_tailscale_status_includes_external_urls(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    save_tunnel_config(
        cfg,
        {"provider": "tailscale", "enabled": True, "target": "dashboard", "mode": "funnel"},
    )
    tunnels._save_state(
        cfg.paths,
        "tailscale",
        {
            "started_at": 123.0,
            "target_url": "http://127.0.0.1:18380",
            "external_urls": ["https://demo.tailnet.ts.net"],
        },
    )
    monkeypatch.setattr(tunnels, "executable_path", lambda _paths, _provider: "tailscale")
    monkeypatch.setattr(tunnels, "_run_version", lambda _path: "1.94.2")
    monkeypatch.setattr(tunnels, "_tailscale_ready", lambda _paths: (True, {"backend_state": "Running"}))
    monkeypatch.setattr(tunnels, "_tailscale_external_urls", lambda _paths, _mode="funnel": [])

    status = public_tunnel_status(cfg)
    tailscale = next(row for row in status["providers"] if row["spec"]["id"] == "tailscale")

    assert tailscale["running"] is True
    assert tailscale["state"]["external_urls"] == ["https://demo.tailnet.ts.net"]


def test_cloudflare_status_uses_latest_quick_tunnel_url(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    save_tunnel_config(
        cfg,
        {"provider": "cloudflare", "enabled": True, "target": "dashboard"},
    )
    log_path = cfg.paths.dev_logs / "tunnels" / "cloudflare.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        """
--- nerya tunnel start 2026-05-11T07:50:50Z ---
Visit it at https://old-dead.trycloudflare.com
Request failed dest=https://old-dead.trycloudflare.com/api/proxy/health
--- nerya tunnel start 2026-05-16T18:51:07Z ---
Visit it at https://fresh-live.trycloudflare.com
Request failed dest=https://fresh-live.trycloudflare.com/api/proxy/health
""".strip(),
        encoding="utf-8",
    )
    tunnels._save_state(
        cfg.paths,
        "cloudflare",
        {
            "pid": 123,
            "started_at": 123.0,
            "target_url": "http://127.0.0.1:18380",
            "external_urls": ["https://old-dead.trycloudflare.com"],
        },
    )
    monkeypatch.setattr(tunnels, "executable_path", lambda _paths, _provider: "cloudflared")
    monkeypatch.setattr(tunnels, "_run_version", lambda _path: "cloudflared test")
    monkeypatch.setattr(tunnels, "_is_pid_running", lambda _pid: True)

    status = public_tunnel_status(cfg)
    cloudflare = next(row for row in status["providers"] if row["spec"]["id"] == "cloudflare")

    assert cloudflare["running"] is True
    assert cloudflare["state"]["external_urls"] == ["https://fresh-live.trycloudflare.com"]


def test_stopped_tunnel_status_hides_stale_external_urls(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    save_tunnel_config(
        cfg,
        {"provider": "cloudflare", "enabled": True, "target": "dashboard"},
    )
    tunnels._save_state(
        cfg.paths,
        "cloudflare",
        {
            "pid": 123,
            "started_at": 123.0,
            "target_url": "http://127.0.0.1:18380",
            "external_urls": ["https://old-dead.trycloudflare.com"],
        },
    )
    monkeypatch.setattr(tunnels, "executable_path", lambda _paths, _provider: "cloudflared")
    monkeypatch.setattr(tunnels, "_run_version", lambda _path: "cloudflared test")
    monkeypatch.setattr(tunnels, "_is_pid_running", lambda _pid: False)

    status = public_tunnel_status(cfg)
    cloudflare = next(row for row in status["providers"] if row["spec"]["id"] == "cloudflare")

    assert cloudflare["running"] is False
    assert cloudflare["state"]["external_urls"] == []


def test_tunnel_start_and_stop_persist_last_desired_state(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.data.setdefault("runtime", {})["auth"] = {
        "admin_password_hash": "pbkdf2_sha256$1$2$abc",
        "mode": "local",
    }
    save_tunnel_config(
        cfg,
        {"provider": "ngrok", "enabled": True, "target": "dashboard"},
    )
    monkeypatch.setattr(tunnels, "executable_path", lambda _paths, _provider: "ngrok")
    running_pids = {12345: True}
    monkeypatch.setattr(tunnels, "_is_pid_running", lambda pid: running_pids.get(int(pid), False))

    class FakePopen:
        pid = 12345

        def __init__(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(tunnels.subprocess, "Popen", FakePopen)

    out = start_tunnel(cfg, "ngrok")

    assert out["ok"] is True
    saved = yaml_io.load(tmp_path / "nerya.yml", default={})
    assert saved["network"]["tunnels"]["providers"]["ngrok"]["desired_running"] is True
    assert cfg.data["network"]["tunnels"]["providers"]["ngrok"]["desired_running"] is True

    running_pids[12345] = False
    stopped = stop_tunnel(cfg, "ngrok")

    assert stopped["ok"] is True
    saved = yaml_io.load(tmp_path / "nerya.yml", default={})
    assert saved["network"]["tunnels"]["providers"]["ngrok"]["desired_running"] is False
    assert cfg.data["network"]["tunnels"]["providers"]["ngrok"]["desired_running"] is False


def test_tunnel_start_failure_does_not_mark_desired_running(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.data.setdefault("runtime", {})["auth"] = {
        "admin_password_hash": "pbkdf2_sha256$1$2$abc",
        "mode": "local",
    }
    save_tunnel_config(
        cfg,
        {"provider": "cloudflare", "enabled": True, "target": "dashboard"},
    )
    monkeypatch.setattr(tunnels, "executable_path", lambda _paths, _provider: "cloudflared")
    monkeypatch.setattr(tunnels, "_is_pid_running", lambda _pid: False)

    class FakePopen:
        pid = 44444

        def __init__(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(tunnels.subprocess, "Popen", FakePopen)

    out = start_tunnel(cfg, "cloudflare")

    assert out["ok"] is False
    assert out["error"] == "start_failed"
    saved = yaml_io.load(tmp_path / "nerya.yml", default={})
    assert saved["network"]["tunnels"]["providers"]["cloudflare"]["desired_running"] is False
    assert not (tmp_path / "state" / "tunnels" / "cloudflare.json").exists()


def test_startup_restore_starts_only_last_running_tunnels(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    save_tunnel_config(
        cfg,
        {"provider": "cloudflare", "enabled": True, "target": "dashboard", "desired_running": True},
    )
    save_tunnel_config(
        cfg,
        {"provider": "ngrok", "enabled": True, "target": "dashboard", "desired_running": False},
    )
    started: list[str] = []

    monkeypatch.setattr(tunnels, "_provider_public_status", lambda _config, provider: {"running": False})

    def fake_start(_config, provider):
        started.append(provider)
        return {"ok": True, "provider": provider, "external_urls": [f"https://{provider}.example"]}

    monkeypatch.setattr(tunnels, "start_tunnel", fake_start)

    out = restore_configured_tunnels_on_start(cfg)

    assert out["ok"] is True
    assert started == ["cloudflare"]
    assert out["started"][0]["provider"] == "cloudflare"


def test_startup_restore_treats_legacy_state_as_desired_running(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.data.setdefault("network", {}).setdefault("tunnels", {}).setdefault("providers", {})["cloudflare"] = {
        "enabled": True,
        "target": "dashboard",
        "mode": "public",
    }
    tunnels._save_state(
        cfg.paths,
        "cloudflare",
        {
            "pid": 98765,
            "started_at": 123.0,
            "target_url": "http://127.0.0.1:18380",
        },
    )
    started: list[str] = []
    monkeypatch.setattr(tunnels, "_provider_public_status", lambda _config, provider: {"running": False})

    def fake_start(_config, provider):
        started.append(provider)
        return {"ok": True, "provider": provider, "external_urls": []}

    monkeypatch.setattr(tunnels, "start_tunnel", fake_start)

    out = restore_configured_tunnels_on_start(cfg)

    assert out["ok"] is True
    assert started == ["cloudflare"]


def test_tunnel_route_scopes_are_not_public():
    assert route_scopes.required_scope("GET", "/network/dashboard") == "read:runtime"
    assert route_scopes.required_scope("POST", "/network/dashboard") == "write:config"
    assert route_scopes.required_scope("GET", "/network/tunnels") == "read:runtime"
    assert route_scopes.required_scope("POST", "/network/tunnels/config") == "write:config"
    assert route_scopes.required_scope("POST", "/network/tunnels/install") == "admin:ops"
    assert route_scopes.required_scope("POST", "/network/tunnels/start") == "admin:ops"
    assert route_scopes.required_scope("POST", "/network/tunnels/stop") == "admin:ops"
