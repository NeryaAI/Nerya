from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import routes_llm
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.llm import ops


@pytest.fixture
def config(tmp_path):
    data = deepcopy(DEFAULT_CONFIG)
    data["llm"]["providers"] = {"fixture": {"base_url": "https://profile.invalid/v1", "provider_key_ref": "vault://fixture-key", "kind": "anthropic_messages"}}
    data["llm"]["tiers"] = {"medium": {"context_window": 128000, "max_tokens": 2000, "routes": [
        {"provider": "fixture", "model": "primary"},
        {"provider": "fixture", "model": "fallback", "base_url": "https://explicit.invalid/v1", "provider_key_ref": "vault://route-key"},
    ]}}
    return Config(paths=WorkspacePaths(root=tmp_path), data=data)


def row(result):
    return next(t for t in result["tiers"] if t["tier"] == "medium")


def test_context_save_preserves_inheritance_and_explicit_connection(config):
    initial = ops.llm_config(config)
    primary, fallback = row(initial)["routes"]
    assert primary["base_url"] == "https://profile.invalid/v1"  # existing consumers
    assert "base_url" not in primary["declared"]
    assert primary["source"]["base_url"] == "provider"
    assert primary["source"]["context_window"] == "tier"
    assert fallback["source"]["base_url"] == "route"
    routes = [deepcopy(r["declared"]) for r in row(initial)["routes"]]
    routes[0]["context_window"] = 64000
    saved = ops.llm_config_set(config, tiers=[{"tier": "medium", "context_window": 128000, "routes": routes}], explicit_overrides=True)
    declared = config.get("llm.tiers")["medium"]
    assert declared["max_tokens"] == 2000
    assert declared["context_window"] == 128000
    assert row(saved)["routes"][1]["effective"]["context_window"] == 128000
    assert "base_url" not in declared["routes"][0]
    assert "provider_key_ref" not in declared["routes"][0]
    assert declared["routes"][1]["provider_key_ref"] == "vault://route-key"
    assert "context_window" not in declared["routes"][1]
    assert initial["revision"] != saved["revision"]
    ops.llm_config_set(config, providers=[{"provider": "fixture", "base_url": "https://new.invalid/v1"}])
    primary, fallback = row(ops.llm_config(config))["routes"]
    assert primary["effective"]["base_url"] == "https://new.invalid/v1"
    assert fallback["effective"]["base_url"] == "https://explicit.invalid/v1"


def test_restoring_inheritance_removes_route_and_profile_overrides(config):
    routes = [r["declared"] for r in row(ops.llm_config(config))["routes"]]
    routes[1].update(base_url="", provider_key_ref="", kind="")
    result = ops.llm_config_set(config, tiers=[{"tier": "medium", "routes": routes}], explicit_overrides=True)
    route = row(result)["routes"][1]
    assert route["effective"]["base_url"] == "https://profile.invalid/v1"
    assert route["source"]["provider_key_ref"] == "provider"
    result = ops.llm_config_set(config, providers=[{"provider": "fixture", "base_url": "", "provider_key_ref": ""}])
    assert "base_url" not in config.get("llm.providers")["fixture"]
    assert "provider_key_ref" not in config.get("llm.providers")["fixture"]


def test_read_and_save_never_call_a_model(config, monkeypatch):
    monkeypatch.setattr(routes_llm, "LLMGateway", lambda *_: pytest.fail("unexpected model call"))
    ops.llm_config(config)
    before = deepcopy(config.get("llm.tiers"))
    ops.llm_config_set(config, providers=[{"provider": "fixture", "base_url": "https://saved.invalid"}])
    assert config.get("llm.tiers") == before


@pytest.mark.parametrize("succeeds", [True, False])
def test_discovery_reports_write_semantics_and_does_not_save_failure(config, monkeypatch, succeeds):
    calls = []
    class FakeAdapter:
        def list_models(self, **kwargs):
            calls.append(kwargs)
            if not succeeds:
                raise RuntimeError("do not leak fixture-secret")
            return [{"id": "fixture-model"}]
    monkeypatch.setattr(ops, "builtin_providers", lambda: {"anthropic": FakeAdapter()})
    before = deepcopy(config.data)
    result = ops.models_discover(config, provider="fixture", provider_key="fixture-secret", api_mode="anthropic_messages")
    assert calls[0]["base_url"] == "https://profile.invalid/v1"
    assert calls[0]["api_key"] == "fixture-secret"
    assert result["connection_saved"] is succeeds
    assert "fixture-secret" not in str(result)
    if succeeds:
        assert result["revision"] == ops.config_revision(config)
        assert result["provider_profile"]["kind"] == "anthropic_messages"
        assert result["provider_key_ref"].startswith("vault://")
        assert "fixture-secret" not in config.paths.config.read_text()
    else:
        assert config.data == before
        assert not config.paths.config.exists()


def test_probe_refuses_stale_revision_without_network(config, monkeypatch):
    monkeypatch.setattr(routes_llm, "LLMGateway", lambda *_: pytest.fail("stale revision must not call model"))
    result = routes_llm._messages_probe(SimpleNamespace(config=config), {"revision": "old"})
    assert result["_status"] == 409
    assert result["revision"] == ops.config_revision(config)


@pytest.mark.parametrize("mode", ["ok", "failure", "changed"])
def test_probe_binds_results_to_saved_snapshot(config, monkeypatch, mode):
    snapshot = deepcopy(config.data)
    revision = ops.config_revision(config)
    class FakeGateway:
        def __init__(self, received):
            assert received is not config
            assert received.data == snapshot
        def call_messages(self, **kwargs):
            assert kwargs["max_tokens"] == 2000
            assert kwargs["reasoning_effort"] is None
            if mode == "failure":
                raise RuntimeError("never return fixture-secret")
            if mode == "changed":
                config.data["llm"]["default_tier"] = "light"
            return SimpleNamespace(provider="fixture", model="primary", stop_reason="end_turn", latency_ms=1, text=lambda: "READY", tool_uses=lambda: [])
    monkeypatch.setattr(routes_llm, "LLMGateway", FakeGateway)
    result = routes_llm._messages_probe(SimpleNamespace(config=config), {"revision": revision})
    assert result["revision"] == revision
    assert result["ok"] is (mode == "ok")
    assert "fixture-secret" not in str(result)
    if mode != "changed":
        assert config.data == snapshot
    assert not config.paths.config.exists()
