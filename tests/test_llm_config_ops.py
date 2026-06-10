from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import routes_llm
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.errors import LLMError
from nerya.core.paths import WorkspacePaths
from nerya.core import yaml_io
from nerya.llm import ops
from nerya.llm.gateway import LLMGateway
from nerya.security.secrets import SecretVault

pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def _route(method: str, path: str):
    for m, p, handler in routes_llm.routes():
        if m == method and p == path:
            return handler
    raise AssertionError(f"route not found: {method} {path}")


def test_llm_config_set_persists_model_assignments_without_plaintext_secret(tmp_path):
    cfg = _config(tmp_path)

    out = ops.llm_config_set(
        cfg,
        default_tier="light",
        tiers=[
            {
                "tier": "light",
                "provider": "openai",
                "model": "gpt-5.4-mini",
                "base_url": "https://api.openai.com/v1",
                "provider_key_ref": "vault://openai_key",
            }
        ],
    )

    assert out["ok"] is True
    assert out["default_tier"] == "light"
    assert cfg.get("llm.default_tier") == "light"
    assert cfg.get("llm.tiers")["light"]["model"] == "gpt-5.4-mini"
    saved = yaml_io.load(tmp_path / "nerya.yml", default={})
    assert saved["llm"]["tiers"]["light"]["provider_key_ref"] == "vault://openai_key"


def test_llm_config_set_converts_plaintext_key_refs_to_vault(tmp_path):
    cfg = _config(tmp_path)

    out = ops.llm_config_set(
        cfg,
        tiers=[
            {
                "tier": "light",
                "provider": "openai",
                "model": "gpt-5.4-mini",
                "provider_key_ref": "sk-not-allowed",
            }
        ],
    )

    ref = out["tiers"][0]["provider_key_ref"]
    assert ref.startswith("vault://")
    saved = yaml_io.load(tmp_path / "nerya.yml", default={})
    assert saved["llm"]["tiers"]["light"]["provider_key_ref"] == ref
    vault = SecretVault.open(tmp_path / "vault" / "secrets.enc")
    assert vault.resolve(ref.removeprefix("vault://"), required_scope="llm") == "sk-not-allowed"


def test_llm_config_set_accepts_multiple_models_and_comma_separated_keys(tmp_path):
    cfg = _config(tmp_path)

    out = ops.llm_config_set(
        cfg,
        tiers=[
            {
                "tier": "medium",
                "provider": "openai",
                "models": ["primary-model", "backup-model"],
                "base_url": "https://api.openai.com/v1",
                "provider_key": "sk-primary, sk-backup",
            }
        ],
    )

    row = next(t for t in out["tiers"] if t["tier"] == "medium")
    ref = row["provider_key_ref"]
    assert row["model"] == "primary-model, backup-model"
    assert row["models"] == ["primary-model", "backup-model"]
    assert ref.startswith("vault://")
    saved = yaml_io.load(tmp_path / "nerya.yml", default={})
    assert saved["llm"]["tiers"]["medium"]["model"] == "primary-model, backup-model"
    assert "sk-primary" not in str(saved)
    vault = SecretVault.open(tmp_path / "vault" / "secrets.enc")
    assert vault.resolve(ref.removeprefix("vault://"), required_scope="llm") == (
        "sk-primary, sk-backup"
    )


def test_llm_config_set_accepts_multiple_provider_routes_per_tier_without_plaintext_secret(tmp_path):
    cfg = _config(tmp_path)

    out = ops.llm_config_set(
        cfg,
        default_tier="medium",
        tiers=[
            {
                "tier": "medium",
                "routes": [
                    {
                        "provider": "agnes",
                        "model": "agnes-2.0-flash",
                        "base_url": "https://apihub.agnes-ai.com/v1",
                        "provider_key": "sk-route-a",
                        "kind": "chat_completions",
                    },
                    {
                        "provider": "stepfun",
                        "models": ["step-3.5-flash", "step-3.7-flash"],
                        "base_url": "https://api.stepfun.com/step_plan/v1",
                        "provider_keys": ["sk-route-b", "sk-route-c"],
                    },
                ],
            }
        ],
    )

    row = next(t for t in out["tiers"] if t["tier"] == "medium")
    assert row["provider"] == "agnes"
    assert row["model"] == "agnes-2.0-flash"
    assert len(row["routes"]) == 2
    assert row["routes"][0]["provider_key_ref"].startswith("vault://")
    assert row["routes"][1]["provider_key_ref"].startswith("vault://")
    assert row["routes"][1]["provider_key_refs"] == [row["routes"][1]["provider_key_ref"]]
    assert row["routes"][1]["models"] == ["step-3.5-flash", "step-3.7-flash"]

    saved = yaml_io.load(tmp_path / "nerya.yml", default={})
    assert saved["llm"]["tiers"]["medium"]["routes"][0]["provider"] == "agnes"
    assert saved["llm"]["tiers"]["medium"]["routes"][1]["provider"] == "stepfun"
    assert "sk-route-a" not in str(saved)
    assert "sk-route-b" not in str(saved)
    vault = SecretVault.open(tmp_path / "vault" / "secrets.enc")
    ref_a = row["routes"][0]["provider_key_ref"].removeprefix("vault://")
    ref_b = row["routes"][1]["provider_key_ref"].removeprefix("vault://")
    assert vault.resolve(ref_a, required_scope="llm") == "sk-route-a"
    assert vault.resolve(ref_b, required_scope="llm") == "sk-route-b, sk-route-c"

    effective = ops.effective_tiers(cfg)["medium"]
    assert effective["provider"] == "agnes"
    assert len(effective["routes"]) == 2
    assert effective["routes"][1]["model"] == "step-3.5-flash, step-3.7-flash"


def test_llm_config_set_vaults_plaintext_items_inside_provider_key_refs(tmp_path):
    cfg = _config(tmp_path)

    out = ops.llm_config_set(
        cfg,
        tiers=[
            {
                "tier": "medium",
                "routes": [
                    {
                        "provider": "openai",
                        "model": "primary-model",
                        "provider_key_refs": [
                            "vault://already-safe",
                            "sk-mixed-plaintext",
                        ],
                    },
                ],
            }
        ],
    )

    row = next(t for t in out["tiers"] if t["tier"] == "medium")
    refs = row["routes"][0]["provider_key_refs"]
    assert refs[0] == "vault://already-safe"
    assert refs[1].startswith("vault://")

    saved = yaml_io.load(tmp_path / "nerya.yml", default={})
    saved_ref = saved["llm"]["tiers"]["medium"]["routes"][0]["provider_key_ref"]
    assert "sk-mixed-plaintext" not in str(saved)
    assert saved_ref == ", ".join(refs)

    vault = SecretVault.open(tmp_path / "vault" / "secrets.enc")
    assert vault.resolve(refs[1].removeprefix("vault://"), required_scope="llm") == "sk-mixed-plaintext"


def test_models_import_persists_selected_catalog_rows(tmp_path):
    cfg = _config(tmp_path)

    out = ops.models_import(
        cfg,
        provider="openai",
        base_url="https://api.openai.com/v1",
        models=[
            {"id": "gpt-test-a", "owned_by": "openai"},
            {"id": "gpt-test-b", "capabilities": ["chat"]},
        ],
    )

    assert out["ok"] is True
    assert out["counts"]["openai"] == 2
    saved = yaml_io.load(tmp_path / "llm" / "model_catalog.json", default={})
    ids = [row["id"] for row in saved["providers"]["openai"]]
    assert ids == ["gpt-test-a", "gpt-test-b"]


def test_models_discover_custom_openai_compat_uses_vaulted_key(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    calls = []

    class FakeCompatAdapter:
        def list_models(self, *, api_key, base_url=None, provider_name="compat"):
            calls.append({
                "api_key": api_key,
                "base_url": base_url,
                "provider_name": provider_name,
            })
            return [{"id": "cliproxy-model", "owned_by": "cliproxy"}]

    monkeypatch.setattr(
        ops,
        "builtin_providers",
        lambda: {"compat": FakeCompatAdapter()},
    )

    out = ops.models_discover(
        cfg,
        provider="cliproxy",
        base_url="http://3.112.67.22:8317/v1",
        provider_key="sk-custom-test",
        api_mode="chat_completions",
    )

    assert out["ok"] is True
    assert out["count"] == 1
    assert out["models"][0]["id"] == "cliproxy-model"
    assert out["provider_key_ref"].startswith("vault://")
    assert calls == [{
        "api_key": "sk-custom-test",
        "base_url": "http://3.112.67.22:8317/v1",
        "provider_name": "cliproxy",
    }]
    saved = yaml_io.load(tmp_path / "nerya.yml", default={})
    profile = saved["llm"]["providers"]["cliproxy"]
    assert profile["base_url"] == "http://3.112.67.22:8317/v1"
    assert profile["provider_key_ref"] == out["provider_key_ref"]
    assert "sk-custom-test" not in str(saved)


def test_llm_provider_profile_is_vaulted_and_inherited_by_tier(tmp_path):
    cfg = _config(tmp_path)

    out = ops.llm_config_set(
        cfg,
        default_tier="medium",
        intent_tier="intent",
        providers=[
            {
                "provider": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "provider_key": "sk-provider-level",
                "provider_native_web_search": {
                    "enabled": True,
                    "search_context_size": "low",
                },
            }
        ],
        tiers=[
            {"tier": "medium", "provider": "openrouter", "model": "router-medium"},
            {"tier": "intent", "provider": "openrouter", "model": "router-small"},
        ],
    )

    assert out["ok"] is True
    assert out["intent_tier"] == "intent"
    profile_ref = out["provider_profiles"][0]["provider_key_ref"]
    assert profile_ref.startswith("vault://")
    saved = yaml_io.load(tmp_path / "nerya.yml", default={})
    assert saved["llm"]["providers"]["openrouter"]["provider_key_ref"] == profile_ref
    assert "sk-provider-level" not in str(saved)
    effective = ops.effective_tiers(cfg)
    assert effective["medium"]["provider_key_ref"] == profile_ref
    assert effective["intent"]["base_url"] == "https://openrouter.ai/api/v1"
    assert effective["medium"]["provider_native_web_search"] == {
        "enabled": True,
        "search_context_size": "low",
    }
    cfg.data["llm"]["tiers"]["medium"]["provider_native_web_search"] = {
        "enabled": False,
    }
    assert ops.effective_tiers(cfg)["medium"]["provider_native_web_search"] == {
        "enabled": False,
    }
    assert "classify" in cfg.get("llm.tiers")["intent"]["allowed_tasks"]


def test_classify_defaults_to_configured_intent_tier(tmp_path):
    cfg = _config(tmp_path)
    cfg.data["llm"]["intent_tier"] = "intent"
    cfg.data["llm"]["tiers"]["intent"] = {
        "provider": "mock",
        "model": "intent-model",
        "allowed_tasks": ["classify"],
        "allowed_classes": ["classification"],
    }

    result = LLMGateway(cfg).classify(
        caller="test",
        text="breaking hack risk",
        labels=["alpha", "risk", "noise"],
    )

    assert result["label"] == "risk"


def test_llm_classify_route_uses_prompt_as_text(tmp_path):
    class FakeLLMAPI:
        def classify(self, *, prompt, labels, caller):  # noqa: ANN001
            return {"prompt": prompt, "labels": labels, "caller": caller}

    client = SimpleNamespace(llm=FakeLLMAPI())

    result = _route("POST", "/llm/classify")(
        client,
        {"prompt": "breaking hack risk", "labels": ["alpha", "risk", "noise"]},
    )

    assert result == {
        "prompt": "breaking hack risk",
        "labels": ["alpha", "risk", "noise"],
        "caller": "http",
    }


def test_llm_messages_probe_uses_messages_backend(tmp_path):
    cfg = _config(tmp_path)
    cfg.data["llm"]["tiers"]["medium"] = {
        "provider": "mock",
        "model": "messages-model",
        "allowed_tasks": ["agent.loop"],
        "allowed_classes": ["agent_loop"],
    }
    client = SimpleNamespace(config=cfg)

    result = _route("POST", "/llm/messages/probe")(
        client,
        {"tier": "medium", "prompt": "hello", "max_tokens": 16},
    )

    assert result["ok"] is True
    assert result["provider"] == "mock"
    assert result["model"] == "messages-model"
    assert "[mock] you said" in result["text_preview"]


def test_llm_messages_probe_reports_provider_failures(monkeypatch):
    class FailingGateway:
        def __init__(self, _config):  # noqa: ANN001
            pass

        def call_messages(self, **_kwargs):  # noqa: ANN001
            exc = LLMError("mimo messages api error (429): Too many requests")
            setattr(exc, "status_code", 429)
            setattr(exc, "request_id", "req-test")
            setattr(exc, "raw_body", '{"error":{"message":"Too many requests"}}')
            raise exc

    monkeypatch.setattr(routes_llm, "LLMGateway", FailingGateway)
    client = SimpleNamespace(config=object())

    result = routes_llm._messages_probe(client, {"tier": "medium"})

    assert result["_status"] == 503
    assert result["ok"] is False
    assert result["error"] == "llm_messages_probe_failed"
    assert result["status_code"] == 429
    assert result["request_id"] == "req-test"
