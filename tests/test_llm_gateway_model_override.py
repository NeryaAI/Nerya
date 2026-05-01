from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.llm.gateway import LLMGateway

pytestmark = pytest.mark.smoke


def _config(tmp_path, tiers: dict) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["llm"]["tiers"] = tiers
    return Config(paths=WorkspacePaths(tmp_path), data=data)


def test_messages_call_uses_frontend_model_override_without_mutating_tier(tmp_path):
    cfg = _config(
        tmp_path,
        {
            "medium": {
                "provider": "mock",
                "model": "medium-model",
                "allowed_tasks": ["agent.loop"],
            },
        },
    )
    gateway = LLMGateway(cfg)

    response = gateway.call_messages(
        task="agent.loop",
        caller="test",
        system="You are testing.",
        messages=[{"role": "user", "content": "hello"}],
        tier="medium",
        model_provider="mock",
        model_id="front-end-picked-model",
    )

    assert response.provider == "mock"
    assert response.model == "front-end-picked-model"
    assert cfg.get("llm.tiers")["medium"]["model"] == "medium-model"


def test_provider_override_reuses_matching_provider_credentials(tmp_path):
    cfg = _config(
        tmp_path,
        {
            "medium": {
                "provider": "mock",
                "model": "medium-model",
            },
            "alt": {
                "provider": "openrouter",
                "model": "default-openrouter-model",
                "provider_key_env": "OPENROUTER_API_KEY",
                "base_url": "https://openrouter.ai/api/v1",
            },
        },
    )

    effective = LLMGateway(cfg)._effective_tier_cfg(
        "medium",
        provider_override="openrouter",
        model_override="operator-selected-model",
    )

    assert effective["provider"] == "openrouter"
    assert effective["model"] == "operator-selected-model"
    assert effective["provider_key_env"] == "OPENROUTER_API_KEY"
    assert effective["base_url"] == "https://openrouter.ai/api/v1"
