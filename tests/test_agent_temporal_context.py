from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

import nerya.agent.kernel as kernel_module
from nerya.agent.kernel import (
    AgentKernel,
    _model_user_text_for_trigger,
    _model_user_text_with_scope_boundary,
)
from nerya.agent.loop import LoopOutcome
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core.time import reset_clock, set_clock
from nerya.db.repositories import AgentSessionRepository
from nerya.db.sqlite import connect
from nerya.llm.task_classes import COMPLEX_REASONING, normalise_task_class


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(
        paths=WorkspacePaths(root=tmp_path),
        data=deepcopy(DEFAULT_CONFIG),
    )


def test_system_prompt_includes_current_date_and_freshness_rules(tmp_path) -> None:
    set_clock(lambda: datetime(2026, 5, 2, 15, 58, 10, tzinfo=timezone.utc))
    try:
        cfg = _config(tmp_path)
        kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
        deps = kernel._ensure_registry()

        prompt = kernel._build_system_prompt(deps, session_id="s1")
    finally:
        reset_clock()

    assert "Today's date is 2026-05-02" in prompt
    assert "Current UTC time is 2026-05-02T15:58:10Z" in prompt
    assert "web_search_fetch" in prompt
    assert "current/latest/recent/today/this year" in prompt
    assert "Do not describe 2024-2025 as the current environment" in prompt


def test_browser_turn_focus_blocks_unrequested_trading_pivots(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    deps = kernel._ensure_registry()

    prompt = kernel._build_system_prompt(
        deps,
        session_id="s1",
        user_text="https://example.com 需要操作浏览器验证页面交互",
    )

    assert "Turn focus: browser/web interaction." in prompt
    assert "Browser scope rule" in prompt
    assert "smallest representative interaction" in prompt
    assert "Do not introduce trading, portfolio, market-signal" in prompt
    assert 'skill_id="browser"' in prompt
    assert '"expression"' in prompt


def test_trading_turn_focus_keeps_market_tools_in_scope(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    deps = kernel._ensure_registry()

    prompt = kernel._build_system_prompt(
        deps,
        session_id="s1",
        user_text="给我 BTCUSDT 的交易信号和风险检查",
    )

    assert "Turn focus: trading/market task." in prompt
    assert "Trading and market-data tools are in scope" in prompt
    assert "Do not introduce trading, portfolio, market-signal" not in prompt


def test_system_prompt_matches_final_answer_to_latest_request_language(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    deps = kernel._ensure_registry()

    prompt = kernel._build_system_prompt(
        deps,
        session_id="s1",
        user_text="帮我启动AgentTeam分析英伟达",
    )

    assert "Output language:" in prompt
    assert "Chinese (中文)" not in prompt
    assert "Japanese (日本語)" not in prompt
    assert "Korean (한국어)" not in prompt
    assert "same natural language as the latest user request" in prompt
    assert "Write the final answer and any user-visible conclusion" in prompt
    assert "Translate or synthesize tool/sub-agent outputs" in prompt
    assert "Translate headings, labels, and natural-language field names" in prompt
    assert "do not leave a mixed-language report" in prompt


def test_browser_scope_boundary_is_generic_and_respects_full_scope() -> None:
    bounded = _model_user_text_with_scope_boundary(
        "https://example.com 需要操作浏览器验证页面交互"
    )
    full_scope = _model_user_text_with_scope_boundary(
        "https://example.com 用浏览器完成全部页面交互"
    )

    assert "Browser scope boundary" in bounded
    assert "smallest representative interaction" in bounded
    assert "Browser scope boundary" not in full_scope


def test_prior_chat_history_skips_failed_max_iteration_turns(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]

    con = connect(cfg.paths.db)
    repo = AgentSessionRepository(con)
    repo.upsert_session(session_id="s1")
    repo.record_message(
        message_id="t1:user",
        session_id="s1",
        turn_id="t1",
        role="user",
        content="old browser prompt",
        ts=1,
    )
    repo.record_message(
        message_id="t1:assistant",
        session_id="s1",
        turn_id="t1",
        role="assistant",
        content=(
            "Turn stopped before a complete model-written final answer was "
            "produced. - abort_reason: max_iterations"
        ),
        ts=2,
        meta={
            "turn": {
                "budget": {
                    "aborted": True,
                    "abort_reason": "max_iterations",
                },
            },
        },
    )
    repo.record_message(
        message_id="t2:user",
        session_id="s1",
        turn_id="t2",
        role="user",
        content="fresh prompt",
        ts=3,
    )
    repo.record_message(
        message_id="t2:assistant",
        session_id="s1",
        turn_id="t2",
        role="assistant",
        content="fresh answer",
        ts=4,
    )
    con.commit()
    con.close()

    messages = kernel._load_prior_chat_messages(session_id="s1")

    assert messages == [
        {"role": "user", "content": "fresh prompt"},
        {"role": "assistant", "content": "fresh answer"},
    ]


def test_approval_resume_replays_interrupted_tool_context(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]

    con = connect(cfg.paths.db)
    repo = AgentSessionRepository(con)
    repo.upsert_session(session_id="s_resume")
    repo.record_message(
        message_id="t1:user",
        session_id="s_resume",
        turn_id="t1",
        role="user",
        content="Original task that needs prior tool evidence",
        ts=1,
    )
    repo.record_tool_event(
        event_id="t1:0:tool_use:call_1",
        session_id="s_resume",
        turn_id="t1",
        call_id="call_1",
        tool="native.generic_data",
        phase="tool_use",
        payload={"payload": {"query": "bounded evidence"}},
        ts=2,
    )
    repo.record_tool_event(
        event_id="t1:1:tool_result:call_1",
        session_id="s_resume",
        turn_id="t1",
        call_id="call_1",
        tool="native.generic_data",
        phase="tool_result",
        ok=True,
        payload={"result": {"rows": [{"id": "observed-row", "score": 9}]}},
        ts=3,
    )
    repo.record_tool_event(
        event_id="t1:2:tool_use:call_2",
        session_id="s_resume",
        turn_id="t1",
        call_id="call_2",
        tool="native.requires_permission",
        phase="tool_use",
        payload={"payload": {"operation": "continue"}},
        ts=4,
    )
    repo.record_tool_event(
        event_id="t1:3:tool_result:call_2",
        session_id="s_resume",
        turn_id="t1",
        call_id="call_2",
        tool="native.requires_permission",
        phase="tool_result",
        ok=False,
        payload={"error_kind": "permission_pending", "error": "operator approval required"},
        ts=5,
    )
    repo.record_message(
        message_id="t2:user",
        session_id="s_resume",
        turn_id="t2",
        role="user",
        content="The requested permission was approved.",
        ts=6,
        meta={"source": "approval_continue"},
    )
    con.commit()
    con.close()

    messages = kernel._load_prior_chat_messages(
        session_id="s_resume",
        exclude_turn_id="t2",
        include_interrupted_resume_context=True,
    )

    assert messages[0] == {
        "role": "user",
        "content": "Original task that needs prior tool evidence",
    }
    resume_context = messages[1]["content"]
    assert messages[1]["role"] == "assistant"
    assert "[interrupted turn context]" in resume_context
    assert "observed-row" in resume_context
    assert "generic_data" in resume_context
    assert "permission_pending" in resume_context
    assert "Do not treat the approval notice as a new task" in resume_context
    assert "Turn stopped before a complete model-written final answer" not in resume_context


def test_approval_continue_prompt_is_resume_instruction() -> None:
    text = _model_user_text_for_trigger(
        "The requested permission was approved.",
        {
            "source": "approval_continue",
            "kind": "approval.continue",
            "payload": {"channel": "approval_continue"},
        },
    )

    assert "Continue the prior user task" in text
    assert "Do not treat this approval notice as a new task" in text
    assert "already completed discovery" in text
    assert "The requested permission was approved" not in text
    assert "agent loop" not in text.lower()


def test_approval_continue_with_reused_turn_id_keeps_original_task(
    monkeypatch,
    tmp_path,
) -> None:
    cfg = _config(tmp_path)
    cfg.data.setdefault("llm", {}).setdefault("tiers", {}).setdefault(
        "medium",
        {},
    )["allowed_tasks"] = ["agent.loop"]
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    kernel.sessions.ensure("s_resume")

    con = connect(cfg.paths.db)
    repo = AgentSessionRepository(con)
    repo.upsert_session(session_id="s_resume")
    repo.record_message(
        message_id="t_pending:user",
        session_id="s_resume",
        turn_id="t_pending",
        role="user",
        content="原始任务：继续使用上一轮已经拿到的数据",
        ts=1,
    )
    repo.record_tool_event(
        event_id="t_pending:0:tool_use:call_1",
        session_id="s_resume",
        turn_id="t_pending",
        call_id="call_1",
        tool="native.generic_data",
        phase="tool_use",
        payload={"payload": {"query": "evidence"}},
        ts=2,
    )
    repo.record_tool_event(
        event_id="t_pending:1:tool_result:call_1",
        session_id="s_resume",
        turn_id="t_pending",
        call_id="call_1",
        tool="native.generic_data",
        phase="tool_result",
        ok=True,
        payload={"result": {"id": "already-observed"}},
        ts=3,
    )
    repo.record_tool_event(
        event_id="t_pending:2:tool_result:call_2",
        session_id="s_resume",
        turn_id="t_pending",
        call_id="call_2",
        tool="native.requires_permission",
        phase="tool_result",
        ok=False,
        payload={"error_kind": "permission_pending"},
        ts=4,
    )
    con.commit()
    con.close()

    captured: dict[str, object] = {}

    class FakeLoop:
        def __init__(self, **_kwargs):
            pass

        def run(self, **kwargs):
            captured.update(kwargs)
            return LoopOutcome(
                transcript=[],
                iterations=1,
                stop_reason="end_turn",
                final_text="done",
                tool_calls=0,
                error_count=0,
            )

    monkeypatch.setattr(kernel_module, "WorkspaceNativeAgentLoop", FakeLoop)

    kernel.run_turn(
        trigger={
            "source": "approval_continue",
            "kind": "approval.continue",
            "payload": {
                "text": "The requested permission was approved.",
                "channel": "approval_continue",
            },
        },
        session_id="s_resume",
        turn_id="t_pending",
    )

    prior_messages = captured["prior_messages"]
    assert isinstance(prior_messages, list)
    assert prior_messages[0]["content"] == "原始任务：继续使用上一轮已经拿到的数据"
    assert "already-observed" in prior_messages[1]["content"]
    assert "permission_pending" in prior_messages[1]["content"]

    con = connect(cfg.paths.db)
    rows = AgentSessionRepository(con).transcript("s_resume", limit=0)
    con.close()
    user_rows = [row for row in rows if row["role"] == "user"]
    assert user_rows[0]["content"] == "原始任务：继续使用上一轮已经拿到的数据"
    assert "The requested permission was approved" not in user_rows[0]["content"]


def test_freeform_investment_analysis_tasks_route_to_complex_reasoning() -> None:
    assert normalise_task_class("analysis") == COMPLEX_REASONING
    assert normalise_task_class("a_share_investment_guide") == COMPLEX_REASONING
