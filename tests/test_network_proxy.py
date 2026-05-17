from __future__ import annotations

import os
from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import routes_network
from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core.proxy import (
    apply_network_proxy,
    browser_proxy_config_for_workspace,
    parse_proxy_pool_response,
    proxy_env_for_workspace,
    resolve_proxy_env,
    save_proxy_config,
)
from nerya.security.runtime_env import build_process_env


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def _routes():
    return {(method, path): handler for method, path, handler in routes_network.routes()}


def test_proxy_config_direct_env_and_subprocess_env(tmp_path):
    cfg = _config(tmp_path)
    save_proxy_config(
        cfg,
        {
            "enabled": True,
            "mode": "direct",
            "preset": "custom",
            "all_url": "http://127.0.0.1:7890",
            "no_proxy": "localhost,127.0.0.1",
        },
    )

    env, details = resolve_proxy_env(cfg)

    assert details["enabled"] is True
    assert env["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert env["NO_PROXY"] == "localhost,127.0.0.1"
    assert proxy_env_for_workspace(tmp_path)["ALL_PROXY"] == "http://127.0.0.1:7890"
    assert build_process_env({}, tmp_path)["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert browser_proxy_config_for_workspace(tmp_path) == {
        "server": "http://127.0.0.1:7890",
        "bypass": "localhost,127.0.0.1",
    }


def test_proxy_urls_with_credentials_are_vaulted(tmp_path):
    cfg = _config(tmp_path)
    save_proxy_config(
        cfg,
        {
            "enabled": True,
            "mode": "direct",
            "all_url": "http://user:secret@proxy.local:8080",
        },
    )

    saved = yaml_io.load(tmp_path / "nerya.yml", default={})
    proxy = saved["network"]["proxy"]

    assert "all_url" not in proxy
    assert proxy["all_url_ref"].startswith("vault://network_proxy_all_url_")
    assert "secret" not in str(saved)
    env, _details = resolve_proxy_env(cfg)
    assert env["HTTP_PROXY"] == "http://user:secret@proxy.local:8080"


def test_network_proxy_routes_save_and_apply(monkeypatch, tmp_path):
    cfg = _config(tmp_path)
    routes = _routes()
    client = SimpleNamespace(config=cfg)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)

    out = routes[("POST", "/network/proxy")](
        client,
        {
            "enabled": True,
            "mode": "direct",
            "all_url": "http://127.0.0.1:7891",
        },
    )

    assert out["ok"] is True
    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:7891"
    assert out["applied"]["env"]["HTTP_PROXY"] == "http://127.0.0.1:7891"

    apply_network_proxy(Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG)))


def test_proxy_pool_response_parsing():
    assert parse_proxy_pool_response('{"proxy": "1.2.3.4:8080"}') == "http://1.2.3.4:8080"
    assert parse_proxy_pool_response(
        '{"data": {"ip": "2.3.4.5", "port": 3128}}',
        pool_format="smart_json",
    ) == "http://2.3.4.5:3128"
    assert parse_proxy_pool_response("socks5h://127.0.0.1:1080", pool_format="text") == "socks5h://127.0.0.1:1080"
