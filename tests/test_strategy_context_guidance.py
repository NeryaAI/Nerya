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
    assert "strategy_generate_proposal / strategy_validate /" in prompt
    assert "strategy_backtest using proposal_id" in prompt
    assert "stop probing after bounded evidence" in prompt
    assert "coverage_ok is false" in prompt
    assert "Do not fabricate synthetic/random/placeholder replay data" in prompt
    assert "do not stop to ask for missing market" in prompt
    assert "Read it with skill_view before connector_list" in prompt
    assert "pick a liquid market with real historical candles" in prompt
    assert "do not override files.main.py" in prompt
    assert "custom strategy-authoring tasks" in prompt
    assert "using StrategyContext" in prompt
    assert "StrategyAgentTask" in prompt
    assert "only as the proposal packager" in prompt
    assert "repair it in the same turn" in prompt
    assert "never multiply them by 100" in prompt
    assert "one bounded" in prompt
    assert "market_data/backtest attempt" in prompt
    assert "stop discovery and write the SDK" in prompt
    assert "Do not call shell, glob, or raw file reads" in prompt
    assert "the efficient evidence boundary is wallet capability" in prompt
    assert "generate the SDK strategy" in prompt
    assert "until strategy_generate_proposal with SDK files has been" in prompt
    assert "do not satisfy that request with CEX symbols" in prompt
    assert "silently substituting CEX candles" in prompt


def test_strategy_author_skill_contains_soft_context_rules() -> None:
    text = (
        __import__("pathlib")
        .Path("nerya/skills/builtin/strategy_author/SKILL.md")
        .read_text(encoding="utf-8")
    )

    assert "Market context inheritance" in text
    assert "Use to author SDK strategy code" in text
    assert "- meme" in text
    assert "- wallet" in text
    assert "- onchain" in text
    assert "advisory context for your judgment, not a hard router" in text
    assert "Preserve the market scope that the session has already established" in text
    assert "Examples are examples, not defaults" in text
    assert "Market scope assumption" in text
    assert "strategy_generate_proposal" in text
    assert "strategy_backtest({\"proposal_id\"" in text
    assert "--proposal-id <proposal_id>" in text
    assert "do not ask another confirmation question first" in text
    assert "proposal_paths" in text
    assert "coverage_ok" in text
    assert "reason:no_historical_data" in text
    assert "regenerate a strategy only because" in text
    assert "promoted strategy path is absent" in text
    assert "do not reply with a questionnaire" in text
    assert "non-live mode, modest sizing" in text
    assert "do not override `files.main.py`" in text
    assert "draft the package files yourself with the Nerya strategy SDK" in text
    assert "strategy_generate_proposal` is only the proposal" in text
    assert "For custom strategies, write `files.main.py`" in text
    assert "StrategyAgentTask" in text
    assert "Never multiply raw `_pct` fields by 100" in text
    assert "For on-chain meme, news, social" in text
    assert "do not request shell just to" in text
    assert "stop discovery and write the SDK proposal" in text
    assert "Do not call shell, glob, or raw file reads" in text
    assert "the efficient evidence boundary is" in text
    assert "generate the SDK strategy package immediately" in text
    assert "until a `strategy_generate_proposal` call with SDK `files`" in text
    assert "do not satisfy\nthat request with CEX proxies" in text
    assert "not a valid on-chain\nbacktest" in text


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
    assert "next_required_action" in desc
    assert "proposal_paths" in desc
    assert "proposal rationale" in desc
    assert "instead of probing promoted strategy paths" in desc
    assert "infer the intended scope from the conversation" in desc
    assert "unrelated example market" in desc
    assert "draft the SDK package files first" in desc
    assert "not invent the core strategy logic" in desc


def test_run_shell_description_defers_strategy_authoring_to_native_tools(tmp_path) -> None:
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

    desc = registry.get("run_shell").description
    assert "Do not use this for strategy authoring" in desc
    assert "connector/data-source discovery" in desc
    assert "wallet/on-chain provider inspection" in desc
    assert "strategy_generate_proposal with `files`" in desc
    assert "SDK code when custom logic is needed" in desc
    assert "reserve shell for explicit operator commands" in desc


def test_data_api_description_routes_meme_guide_to_sdk_authoring(tmp_path) -> None:
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

    desc = registry.get("data_api").description
    assert "meme_strategy_guide returns next_required_action" in desc
    assert "bounded_sequence" in desc
    assert "SDK strategy package authoring" in desc
    assert "do not use shell or public web search" in desc


def test_strategy_backtest_tool_supports_proposal_targets(tmp_path) -> None:
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

    desc = registry.get("strategy_backtest").description
    assert "proposal_id" in desc
    assert "before promotion" in desc
    assert "45-day window" in desc
    assert "coverage_ok" in desc
    assert "one-month-plus backtest" in desc
    assert "no_historical_data" in desc
    assert "strategy_root" in desc
    assert "metrics_display" in desc
    assert "percentage points" in desc
    assert "Never multiply them by 100" in desc
    assert "operator_summary_text" in desc
    assert "raw_metrics_file" in desc


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
