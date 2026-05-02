from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.agent.kernel import AgentKernel, AgentTurnResult
from nerya.agent.session import SessionStore
from nerya.api import routes_agent
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.tools.native.bootstrap import build_native_tool_deps, register_native_tools
from nerya.tools.registry import ToolRegistry


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def test_system_prompt_surfaces_session_market_context_as_advisory(tmp_path) -> None:
    cfg = _config(tmp_path)
    store = SessionStore(cfg.paths.root)
    store.ensure("sess_cn")
    store.update_meta(
        "sess_cn",
        {
            "market_context": {
                "market_domain": "cn_equity",
                "asset_class": "equity",
                "confidence": 0.86,
                "evidence": ["中国大模型", "A股"],
                "source": "prior_user_turns",
            }
        },
    )

    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    prompt = kernel._build_system_prompt(kernel._ensure_registry(), session_id="sess_cn")

    assert "Session market context (advisory)" in prompt
    assert "market_domain: cn_equity" in prompt
    assert "generic follow-up strategy requests should inherit this context" in prompt
    assert "do not substitute unrelated example markets" in prompt
    assert "not a keyword router or hard tool gate" in prompt


def test_strategy_author_skill_contains_soft_context_rules() -> None:
    text = (
        __import__("pathlib")
        .Path("nerya/skills/builtin/strategy_author/SKILL.md")
        .read_text(encoding="utf-8")
    )

    assert "Market context inheritance" in text
    assert "advisory context for your judgment, not a hard router" in text
    assert "Preserve the market scope that the session has already established" in text
    assert "Examples are examples, not defaults" in text
    assert "Market scope assumption" in text
    assert "strategy_generate_proposal" in text


def test_strategy_generate_proposal_description_reminds_market_context(tmp_path) -> None:
    cfg = _config(tmp_path)
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=cfg.paths.root,
        skill_roots=[],
        paths=cfg.paths,
        config=cfg,
        skills=None,
    )
    register_native_tools(registry, deps)

    desc = registry.get("strategy_generate_proposal").description
    assert "preserve the session market context" in desc
    assert "proposal rationale" in desc
    assert "infer the intended scope from the conversation" in desc
    assert "unrelated example market" in desc


def test_market_context_module_does_not_keyword_infer_domains() -> None:
    text = (
        __import__("pathlib")
        .Path("nerya/agent/market_context.py")
        .read_text(encoding="utf-8")
    )

    assert "infer_market_context" not in text
    assert "update_session_market_context_from_text" not in text
    assert "TERMS" not in text
    assert "for term in" not in text


def test_run_turn_reuses_requested_turn_id_for_resume(monkeypatch, tmp_path) -> None:
    cfg = _config(tmp_path)
    calls: list[dict] = []

    def fake_run_turn(self, **kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return AgentTurnResult(
            trigger_event_id="evt_resume",
            strategy_id=kwargs.get("strategy_id"),
            session_id=kwargs.get("session_id"),
            turn_id=kwargs.get("turn_id") or "new_turn",
            decision={"action": "send_message", "text": "resumed"},
            actions=[{"action": "send_message", "ok": True, "text": "resumed"}],
            tool_trace=[],
            stopped_reason="end_turn",
            final_text="resumed",
        )

    monkeypatch.setattr(AgentKernel, "run_turn", fake_run_turn)
    client = SimpleNamespace(config=cfg, skills=None)
    run_turn = next(
        handler
        for method, path, handler in routes_agent.routes()
        if method == "POST" and path == "/agent/run_turn"
    )

    response = run_turn(
        client,
        {
            "session_id": "sess_resume",
            "turn_id": "turn_pending",
            "strategy_id": "s1",
            "trigger": {
                "id": "evt_resume",
                "source": "approval_continue",
                "payload": {"text": "continue"},
            },
        },
    )

    assert calls[0]["turn_id"] == "turn_pending"
    assert response["turn_id"] == "turn_pending"
