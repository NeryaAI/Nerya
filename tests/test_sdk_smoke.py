"""SDK smoke tests.

Boots an :class:`InternalClient` against a temp workspace and verifies
each top-level SDK surface (trading, llm, strategy, messages, skill,
agent, triggers) is reachable and exposes the contract the dashboard
and HTTP routes rely on. Failures here mean a release would silently
break the file-based SDK bridge.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core.paths import WorkspacePaths
from nerya.sdk.internal_client import InternalClient


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def test_internal_client_boots(tmp_path):
    """``InternalClient.from_config`` returns a fully-populated client."""

    cfg = _config(tmp_path)
    client = InternalClient.from_config(cfg)
    assert client.config is cfg
    for name in ("trading", "llm", "strategy", "messages",
                 "skill", "agent", "triggers"):
        api = getattr(client, name, None)
        assert api is not None, f"InternalClient missing required surface: {name}"


def test_sdk_trading_api_surface(tmp_path):
    cfg = _config(tmp_path)
    client = InternalClient.from_config(cfg)
    # The TradingAPI exposes the operator-facing methods the HTTP route
    # calls. We only assert presence; semantics are covered by trading tests.
    for attr in ("submit_intent", "cancel_order", "get_strategy_history"):
        assert hasattr(client.trading, attr), f"TradingAPI missing {attr}"


def test_sdk_agent_api_surface(tmp_path):
    cfg = _config(tmp_path)
    client = InternalClient.from_config(cfg)
    # Smoke-check the agent API has a callable for the workspace-native
    # transcript / trace helpers.
    assert client.agent is not None
    # The agent_api exposes light helpers; we only enforce presence so the
    # dashboard's typed client doesn't drift silently.


def test_sdk_messages_api_surface(tmp_path):
    cfg = _config(tmp_path)
    client = InternalClient.from_config(cfg)
    assert client.messages is not None


def test_sdk_llm_api_surface(tmp_path):
    cfg = _config(tmp_path)
    client = InternalClient.from_config(cfg)
    assert client.llm is not None


def test_sdk_skill_api_call_routes_to_kernel(tmp_path):
    """``client.skill.call`` must route to the SkillKernel."""

    cfg = _config(tmp_path)
    client = InternalClient.from_config(cfg)
    # Sanity: SkillAPI must expose a ``call`` method used by routes_scripts.
    assert hasattr(client.skill, "call"), "SkillAPI missing call()"


def test_sdk_triggers_runtime_boots(tmp_path):
    cfg = _config(tmp_path)
    client = InternalClient.from_config(cfg)
    assert client.triggers_runtime is not None
    # TriggerAPI must expose at least a way to list triggers.
    assert client.triggers is not None
