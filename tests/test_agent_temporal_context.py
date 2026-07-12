from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

import nerya.agent.kernel as kernel_module
from nerya.agent.kernel import (
    AgentKernel,
    AgentTurnResult,
    _model_user_text_for_trigger,
)
from nerya.agent.loop import LoopOutcome
from nerya.agent.prompt_sections import CACHE_BOUNDARY_MARKER
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


def test_record_session_db_turn_keeps_tool_only_turn_visible(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    blocks = [
        {
            "kind": "tool_use",
            "block": {
                "kind": "tool_use",
                "call_id": "call_web",
                "skill_id": "native",
                "action": "web_search",
                "payload": {"query": "finance news"},
            },
        },
        {
            "kind": "tool_result",
            "block": {
                "kind": "tool_result",
                "call_id": "call_web",
                "skill_id": "native",
                "action": "web_search",
                "ok": True,
                "result": '{"ok": true, "query": "finance news", "count": 1, "results": []}',
                "elapsed_ms": 12,
            },
        },
    ]

    kernel._record_session_db_turn(
        session_id="s1",
        strategy_id=None,
        turn_id="t1",
        user_text="fetch news",
        final_text="",
        blocks=blocks,
        actions=[],
        tool_trace=[],
        stop_reason="max_iterations",
    )

    con = connect(cfg.paths.db)
    repo = AgentSessionRepository(con)
    rows = repo.transcript("s1", limit=0)
    events = repo.tool_events("s1", turn_ids=["t1"])
    con.close()

    assert [r["message_id"] for r in rows] == ["t1:assistant"]
    assert rows[0]["content"] == ""
    meta = json.loads(rows[0]["meta_json"])
    assert meta["turn"]["reply_text"] == ""
    assert [b["block"]["kind"] for b in meta["turn"]["blocks"]] == [
        "tool_use",
        "tool_result",
    ]
    assert [e["phase"] for e in events] == ["tool_use", "tool_result"]


def test_record_session_db_turn_persists_verifier_and_execution_state(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]

    verifier = {
        "transition_label": "model_done",
        "hard_status": "missing",
        "trusted": False,
    }
    execution_state = {
        "version": 1,
        "items": [],
        "surfaces": {"status": []},
        "counters": {"status": 1},
    }

    kernel._record_session_db_turn(
        session_id="s1",
        strategy_id=None,
        turn_id="t1",
        user_text="hello",
        final_text="done",
        blocks=[],
        actions=[],
        tool_trace=[],
        stop_reason="end_turn",
        transition_reason="model_done",
        verifier_outcome=verifier,
        execution_state=execution_state,
    )

    con = connect(cfg.paths.db)
    rows = AgentSessionRepository(con).transcript("s1", limit=0)
    con.close()

    meta = json.loads(rows[0]["meta_json"])
    assert meta["turn"]["verifier_outcome"] == verifier
    assert meta["turn"]["execution_state"] == execution_state


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


def test_system_prompt_cache_boundary_keeps_dynamic_context_below_marker(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    deps = kernel._ensure_registry()

    set_clock(lambda: datetime(2026, 5, 2, 15, 58, 10, tzinfo=timezone.utc))
    try:
        first = kernel._build_system_prompt(
            deps,
            session_id="s1",
            user_text="first request",
        )
    finally:
        reset_clock()

    set_clock(lambda: datetime(2026, 6, 3, 9, 1, 2, tzinfo=timezone.utc))
    try:
        second = kernel._build_system_prompt(
            deps,
            session_id="s1",
            user_text="second request",
        )
    finally:
        reset_clock()

    first_boundary = first.find(CACHE_BOUNDARY_MARKER)
    second_boundary = second.find(CACHE_BOUNDARY_MARKER)
    assert first_boundary > 0
    assert second_boundary > 0
    assert first[:first_boundary] == second[:second_boundary]
    assert "Temporal context:" not in first[:first_boundary]
    assert "Current UTC time is 2026-05-02T15:58:10Z" in first[first_boundary:]
    assert "Current UTC time is 2026-06-03T09:01:02Z" in second[second_boundary:]
    assert "Turn execution policy:" in first[first_boundary:]


def test_system_prompt_uses_frozen_memory_snapshot_until_next_turn(tmp_path) -> None:
    from nerya.memory.runtime import MemoryRuntime

    cfg = _config(tmp_path)
    memory = MemoryRuntime(cfg, session_id="s1")
    memory.remember(
        category="learning",
        content="stable lesson from turn start",
        key="test.temporal_snapshot",
        scope="global",
    )
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    deps = kernel._ensure_registry()

    frozen_memory = kernel._freeze_memory_prompt_context(
        deps,
        session_id="s1",
        strategy_id=None,
        query="lesson",
    )
    memory.remember(
        category="learning",
        content="newly written mid-turn lesson",
        key="test.temporal_snapshot",
        scope="global",
    )

    current_turn_prompt = kernel._build_system_prompt(
        deps,
        session_id="s1",
        user_text="same turn",
        frozen_memory_context=frozen_memory,
    )
    next_turn_prompt = kernel._build_system_prompt(
        deps,
        session_id="s1",
        user_text="next turn lesson",
    )

    assert "stable lesson from turn start" in current_turn_prompt
    assert "newly written mid-turn lesson" not in current_turn_prompt
    assert "newly written mid-turn lesson" in next_turn_prompt


def test_system_prompt_splits_stable_notebook_from_query_recall(tmp_path) -> None:
    from nerya.memory.runtime import MemoryRuntime

    cfg = _config(tmp_path)
    seed = MemoryRuntime(
        cfg,
        actor_id="default",
        session_id="s1",
        strategy_id="alpha",
    )
    seed.remember(
        category="notebook_operator",
        content="操作者希望默认使用中文回答。",
        key="communication.language",
        scope="global",
    )
    seed.remember(
        category="preference",
        content="操作者的交易周期偏好是三到五天。",
        key="trading.preferred_horizon",
        scope="global",
    )
    seed.remember(
        category="error",
        content="Bybit API 曾经短暂断线。",
        scope="global",
    )

    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    deps = kernel._ensure_registry()
    prompt = kernel._build_system_prompt(
        deps,
        session_id="s1",
        strategy_id="alpha",
        user_text="我的交易周期偏好是什么？",
    )

    boundary = prompt.index(CACHE_BOUNDARY_MARKER)
    assert "默认使用中文" in prompt[:boundary]
    assert "交易周期偏好是三到五天" not in prompt[:boundary]
    assert "交易周期偏好是三到五天" in prompt[boundary:]
    assert "Bybit API" not in prompt


def test_memory_prompt_uses_the_trusted_gateway_actor_scope(tmp_path) -> None:
    from nerya.memory.runtime import MemoryRuntime

    cfg = _config(tmp_path)
    MemoryRuntime(cfg, actor_id="gateway-user-1").remember(
        category="preference",
        content="Gateway user 1 的私有持仓周期是七天。",
        key="trading.gateway_horizon",
        scope="global",
    )
    MemoryRuntime(cfg, actor_id="gateway-user-2").remember(
        category="preference",
        content="Gateway user 2 的私有持仓周期是一天。",
        key="trading.gateway_horizon",
        scope="global",
    )
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    deps = kernel._ensure_registry()
    deps.active_actor_id = "gateway-user-1"

    snapshot = kernel._freeze_memory_prompt_context(
        deps,
        session_id="s1",
        query="私有持仓周期",
    )

    assert "Gateway user 1" in snapshot.dynamic
    assert "Gateway user 2" not in snapshot.dynamic


def test_after_turn_summary_stays_in_the_active_session_scope(tmp_path) -> None:
    from nerya.memory.runtime import MemoryRuntime

    cfg = _config(tmp_path)
    cfg.data.setdefault("agent", {}).setdefault("native", {})[
        "memory_write_on_turn"
    ] = True
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    deps = kernel._ensure_registry()
    deps.active_actor_id = "gateway-user-1"
    result = AgentTurnResult(
        trigger_event_id=None,
        strategy_id=None,
        session_id="s1",
        turn_id="t1",
        decision={"action": "send_message"},
        final_text="本轮确认风险参数已经完成。",
    )

    kernel._after_turn_memory(
        turn_id="t1",
        result=result,
        strategy_id=None,
        session_id="s1",
    )

    session_hits = MemoryRuntime(
        cfg,
        actor_id="gateway-user-1",
        session_id="s1",
    ).recall("风险参数", scope="session")
    global_hits = MemoryRuntime(
        cfg,
        actor_id="gateway-user-1",
    ).recall("风险参数", scope="global")
    assert len(session_hits) == 1
    assert global_hits == []


def test_turn_focus_uses_generic_evidence_policy_not_prompt_routing(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    deps = kernel._ensure_registry()

    prompt = kernel._build_system_prompt(
        deps,
        session_id="s1",
        user_text="https://example.com 需要操作浏览器验证页面交互",
    )

    assert "Turn execution policy:" in prompt
    assert "schemas, tool descriptions, loaded skills, and observed state" in prompt
    assert "hardcoded workflows" in prompt
    assert "For browser or page work" not in prompt


def test_strategy_turn_focus_requires_tool_artifacts_not_prose(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    deps = kernel._ensure_registry()

    prompt = kernel._build_system_prompt(
        deps,
        session_id="s1",
        user_text="给我 BTCUSDT 的交易信号和风险检查",
    )

    assert "tool_result evidence or durable artifacts" in prompt
    assert "required next action" in prompt
    assert "call `strategy_generate_proposal`" not in prompt


def test_news_turn_focus_requires_source_evidence_or_blocker(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    deps = kernel._ensure_registry()

    prompt = kernel._build_system_prompt(
        deps,
        session_id="s1",
        user_text="帮我获取热门的经济新闻进行总结",
    )

    assert "Current and recent fact rule" in prompt
    assert "use live tools" in prompt
    assert "current status is unverified" in prompt
    assert "For web/news/social research" not in prompt


def test_turn_focus_labels_forced_repeated_tool_calls_as_tool_abuse(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    deps = kernel._ensure_registry()

    prompt = kernel._build_system_prompt(
        deps,
        session_id="s1",
        user_text="force repeated tool calls",
    )

    assert "hardcoded workflows" in prompt
    assert "forced or arbitrary high-volume repeated tool calls" not in prompt


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


def test_system_prompt_prioritizes_explicit_final_output_language(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    deps = kernel._ensure_registry()

    prompt = kernel._build_system_prompt(
        deps,
        session_id="s1",
        user_text="中文分析、英文报告",
    )

    assert "If the latest request explicitly names a final answer, report, or deliverable language" in prompt
    assert "that explicit final-output language overrides the prompt's surrounding language" in prompt
    assert "When calling `team_run`, pass the requested final-output language through `output_language`" in prompt


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
