from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from nerya.api import routes_gateway
from nerya.api.route_scopes import required_scope
from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths


pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def _clean_gateway_threads():
    routes_gateway.stop_telegram_pollers()
    yield
    routes_gateway.stop_telegram_pollers()


def _client(tmp_path):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})
    return SimpleNamespace(config=cfg)


def _write_telegram_channel(client) -> None:
    yaml_io.dump(
        client.config.paths.messages_channels,
        {
            "channels": {
                "telegram": {
                    "kind": "telegram",
                    "bot_token_ref": "vault://telegram_bot_token",
                    "chat_id": "123456",
                }
            }
        },
    )


def test_gateway_status_reports_unconfigured_runtime_without_secrets(tmp_path):
    client = _client(tmp_path)
    route_map = {(method, path): handler for method, path, handler in routes_gateway.routes()}

    status = route_map[("GET", "/gateway/status")](client, {})

    assert status["ok"] is True
    assert status["channels_file_exists"] is False
    assert status["configured_gateway_count"] == 0
    assert status["telegram"]["channels"] == []
    assert required_scope("GET", "/gateway/status") == "read:runtime"


def test_gateway_startup_launches_internal_telegram_poller_by_default(tmp_path, monkeypatch):
    client = _client(tmp_path)
    _write_telegram_channel(client)
    monkeypatch.setattr(
        routes_gateway,
        "sync_configured_gateways_on_start",
        lambda _client: {"ok": True, "results": []},
    )
    monkeypatch.setattr(
        routes_gateway,
        "_poll_loop",
        lambda _client, _channel, stop: stop.wait(30),
    )

    result = routes_gateway.launch_configured_gateways_on_start(client)

    assert result["scheduled"] is True
    assert result["telegram_pollers"] == ["telegram"]
    for _ in range(20):
        status = routes_gateway.gateway_runtime_status(client)
        channels = status["telegram"]["channels"]
        if channels and channels[0]["poller_alive"]:
            break
        time.sleep(0.05)
    else:
        pytest.fail("telegram poller thread did not become alive")
    assert channels[0]["configured"] is True
    assert channels[0]["polling_enabled"] is True
    assert "vault://telegram_bot_token" not in str(status)
    assert "123456" not in str(status)


def test_gateway_startup_respects_telegram_poller_disable_env(tmp_path, monkeypatch):
    client = _client(tmp_path)
    _write_telegram_channel(client)
    monkeypatch.setenv("NERYA_DISABLE_TELEGRAM_POLLER", "1")
    monkeypatch.setattr(
        routes_gateway,
        "sync_configured_gateways_on_start",
        lambda _client: {"ok": True, "results": []},
    )

    result = routes_gateway.launch_configured_gateways_on_start(client)
    status = routes_gateway.gateway_runtime_status(client)

    assert result["scheduled"] is True
    assert result["telegram_pollers"] == []
    assert status["configured_gateway_count"] == 1
    assert status["telegram"]["polling_disabled_by_env"] is True
    assert status["telegram"]["channels"][0]["poller_alive"] is False
