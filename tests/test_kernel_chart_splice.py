"""Tests for ``AgentKernel._splice_chart_blocks``.

The splice logic is pure (no daemon, no LLM, no I/O), so we exercise it
directly: build a synthetic ``LoopOutcome.blocks`` list, drop in a
captured chart block, and check that the resulting transcript has the
chart envelope right after the originating ``tool_result``.
"""

from __future__ import annotations

import pytest

from nerya.agent.kernel import AgentKernel
from nerya.agent.loop import LoopOutcome
from nerya.agent.transcript_blocks import BlockEnvelope


pytestmark = pytest.mark.smoke


_CHART = {
    "kind": "chart",
    "version": "v1",
    "chart_id": "test.chart.alpha",
    "chart_kind": "candlestick",
    "title": "test",
    "series": [{"type": "candlestick", "name": "ohlc", "data_uri": "nerya://chart/test.chart.alpha#series/ohlc"}],
    "source": {"skill": "markets", "action": "get_candles", "as_of": ""},
    "path": "bulk",
    "bulk_data_uri": "nerya://chart/test.chart.alpha",
}


def _make_outcome() -> LoopOutcome:
    return LoopOutcome(
        transcript=[],
        iterations=1,
        stop_reason="end_turn",
        final_text="here is your chart",
        tool_calls=1,
        error_count=0,
        blocks=[
            BlockEnvelope(
                seq=1,
                turn_id="t1",
                message_id="m1",
                role="assistant",
                block={"kind": "text", "text": "fetching candles"},
            ),
            BlockEnvelope(
                seq=2,
                turn_id="t1",
                message_id="m1",
                role="assistant",
                block={
                    "kind": "tool_use",
                    "call_id": "call-1",
                    "skill_id": "native",
                    "action": "run_shell",
                },
            ),
            BlockEnvelope(
                seq=3,
                turn_id="t1",
                message_id="m1",
                role="tool",
                block={
                    "kind": "tool_result",
                    "call_id": "call-1",
                    "skill_id": "native",
                    "action": "run_shell",
                    "ok": True,
                    "result": "<stdout omitted>",
                },
            ),
            BlockEnvelope(
                seq=4,
                turn_id="t1",
                message_id="m1",
                role="assistant",
                block={"kind": "text", "text": "summary"},
            ),
        ],
    )


def test_splice_inserts_after_matching_tool_result() -> None:
    outcome = _make_outcome()
    AgentKernel._splice_chart_blocks(outcome, [("call-1", _CHART)], turn_id="t1")
    kinds = [env.block.get("kind") for env in outcome.blocks]
    assert kinds == ["text", "tool_use", "tool_result", "chart", "text"]
    chart_env = outcome.blocks[3]
    assert chart_env.block["chart_id"] == "test.chart.alpha"
    assert chart_env.block["call_id"] == "call-1"
    assert chart_env.role == "tool"
    assert chart_env.seq > 4  # next_seq came from max() + 1


def test_splice_appends_when_anchor_missing() -> None:
    outcome = _make_outcome()
    AgentKernel._splice_chart_blocks(outcome, [("ghost-call", _CHART)], turn_id="t1")
    last = outcome.blocks[-1]
    assert last.block["kind"] == "chart"
    assert last.block["chart_id"] == "test.chart.alpha"


def test_splice_is_idempotent_on_chart_id() -> None:
    outcome = _make_outcome()
    AgentKernel._splice_chart_blocks(outcome, [("call-1", _CHART)], turn_id="t1")
    AgentKernel._splice_chart_blocks(outcome, [("call-1", _CHART)], turn_id="t1")
    chart_envs = [env for env in outcome.blocks if env.block.get("kind") == "chart"]
    assert len(chart_envs) == 1


def test_splice_no_op_when_captured_empty() -> None:
    outcome = _make_outcome()
    snapshot = list(outcome.blocks)
    AgentKernel._splice_chart_blocks(outcome, [], turn_id="t1")
    assert outcome.blocks == snapshot


def test_splice_handles_multiple_charts_per_turn() -> None:
    outcome = _make_outcome()
    second_chart = dict(_CHART)
    second_chart["chart_id"] = "test.chart.beta"
    AgentKernel._splice_chart_blocks(
        outcome,
        [
            ("call-1", _CHART),
            ("call-1", second_chart),
        ],
        turn_id="t1",
    )
    chart_envs = [env for env in outcome.blocks if env.block.get("kind") == "chart"]
    assert [env.block["chart_id"] for env in chart_envs] == [
        "test.chart.alpha",
        "test.chart.beta",
    ]
