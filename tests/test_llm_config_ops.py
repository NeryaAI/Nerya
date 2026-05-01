from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core import yaml_io
from nerya.llm import ops
from nerya.llm.gateway import LLMGateway
from nerya.security.secrets import SecretVault

pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


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
    assert "classify" in cfg.get("llm.tiers")["intent"]["allowed_tasks"]


def test_classify_defaults_to_configured_intent_tier(tmp_path):
    cfg = _config(tmp_path)
    cfg.data["llm"]["intent_tier"] = "intent"
    cfg.data["llm"]["tiers"]["intent"] = {
        "provider": "mock",
        "model": "intent-model",
        "allowed_tasks": ["classify"],
        "allowed_classes": ["classification"],
        "daily_budget_usd": 1,
    }

    result = LLMGateway(cfg).classify(
        caller="test",
        text="breaking hack risk",
        labels=["alpha", "risk", "noise"],
    )

    assert result["label"] == "risk"
