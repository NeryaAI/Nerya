"""End-to-end (in-process) integration test for the chart pipeline.

Wires the real ``markets.get_candles`` script output through the
real kernel hook (`extract_chart_blocks` + `_splice_chart_blocks`)
against a real ``LoopOutcome`` shape. The only thing we don't touch
is the daemon HTTP server — everything else is the production code
path, so a failure here would surface in a live agent turn.

Why this matters: the unit tests cover each layer independently
(schema / composer / hook / splice / SDK). They don't catch a typo
where the skill emits ``"chart_block"`` but the hook looks for
``"chart_blocks"`` — only an end-to-end run does. We also exercise
the run_shell-style result wrapping (text preamble + raw stdout +
text recap) so we know the lenient JSON walker holds up against
real shell output.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from nerya.agent.chart_hook import extract_chart_blocks, extract_chart_marker_ids
from nerya.agent.kernel import AgentKernel
from nerya.agent.loop import LoopOutcome
from nerya.agent.transcript_blocks import BlockEnvelope
from nerya.skills.builtin.markets.scripts.get_candles import run as get_candles_run


pytestmark = pytest.mark.smoke


def _shell_text_wrapper(stdout_json: str, *, command: str) -> str:
    """Mimic what ``run_shell`` puts in ``ToolResultBlock.result``.

    Cf. ``nerya/tools/native/shell.py``: shell_part.text() concatenates
    stdout + stderr, and a text_part adds the preamble. ``r.text()``
    joins both with newlines, so what the kernel actually sees is::

        <raw stdout>\\n<raw stderr>\\n$ <cmd>\\n[exit=0, took ...]\\n## stdout\\n<stdout>
    """

    return (
        f"{stdout_json}\n"
        f"\n"
        f"$ {command}\n"
        f"[exit=0, took 412ms]\n\n"
        f"## stdout\n{stdout_json}\n"
    )


def test_markets_get_candles_e2e_through_kernel_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real markets.get_candles → real chart_hook.extract → real splice."""

    monkeypatch.setenv("NERYA_ALLOW_MOCK_DATA", "1")
    skill_output = get_candles_run(
        market="mock:BTC/USDT",
        interval="1h",
        limit=8,
        path="inline",  # keep it in-memory so we don't need a workspace
    )
    assert skill_output["error"] is None
    assert isinstance(skill_output.get("chart_blocks"), list)
    assert len(skill_output["chart_blocks"]) == 1

    # Wrap in shell text the way ``run_shell`` would.
    shell_result = _shell_text_wrapper(
        json.dumps(skill_output, default=str),
        command="python -m nerya.skills.builtin.markets.scripts.get_candles",
    )

    # Hook on the wrapped string — same code path as the kernel.
    extracted = extract_chart_blocks(shell_result)
    assert len(extracted) == 1
    assert extracted[0]["chart_kind"] == "candlestick"
    assert extracted[0]["chart_id"] == skill_output["chart_blocks"][0]["chart_id"]

    # Build a synthetic outcome and splice — this is exactly what the
    # kernel does after a tool_result lands.
    outcome = LoopOutcome(
        transcript=[],
        iterations=1,
        stop_reason="end_turn",
        final_text="Here are the candles.",
        tool_calls=1,
        error_count=0,
        blocks=[
            BlockEnvelope(
                seq=1,
                turn_id="t1",
                message_id="m1",
                role="assistant",
                block={"kind": "tool_use", "call_id": "c1", "action": "run_shell"},
            ),
            BlockEnvelope(
                seq=2,
                turn_id="t1",
                message_id="m1",
                role="tool",
                block={
                    "kind": "tool_result",
                    "call_id": "c1",
                    "action": "run_shell",
                    "ok": True,
                    "result": shell_result,
                },
            ),
        ],
    )
    AgentKernel._splice_chart_blocks(
        outcome,
        [("c1", extracted[0])],
        turn_id="t1",
    )
    kinds = [env.block.get("kind") for env in outcome.blocks]
    assert kinds == ["tool_use", "tool_result", "chart"]
    chart_env = outcome.blocks[2]
    assert chart_env.block["chart_id"] == extracted[0]["chart_id"]
    assert chart_env.block["call_id"] == "c1"
    # Inline path → series.data populated, no bulk URI.
    series = chart_env.block.get("series") or []
    assert series and series[0].get("data")
    assert series[0]["data"][0]["close"] > 0


def test_publish_marker_e2e_through_kernel_hook(tmp_path: Path) -> None:
    """Real ``charts.publish`` SDK call → marker stdout → kernel marker hook.

    Simulates the dynamic-code recipe end-to-end without booting a
    daemon. We:

    1. Persist a chart artifact via the publish endpoint.
    2. Compose a ``run_shell`` result whose stdout looks like a
       script that printed the marker after publishing.
    3. Confirm ``extract_chart_marker_ids`` finds it.
    4. Confirm a synthetic kernel can rebuild a chart envelope from
       the artifact + chart_id (the same code lives in
       ``kernel._event_sink``; we mirror it here so the test stays
       focused on the contract).
    """

    from nerya.api.routes_charts import _post_publish
    from nerya.charting import load_chart_artifact
    from nerya.core.paths import WorkspacePaths
    from nerya.workspace.artifact_store import ArtifactStore

    class _StubClient:
        class _Cfg:
            paths = WorkspacePaths(root=tmp_path)

        config = _Cfg()

    block = {
        "chart_kind": "line",
        "title": "rolling sharpe (60d)",
        "series": [
            {
                "type": "line",
                "name": "sharpe",
                "data": [{"time": 1700000000 + i * 86400, "value": 0.8 + i * 0.01} for i in range(20)],
            }
        ],
        "source": {"skill": "agent", "action": "dynamic_code"},
    }
    pub = _post_publish(_StubClient(), {"chart_block": block})
    assert pub["ok"] is True
    chart_id = pub["chart_id"]

    # The script's stdout (typical shape after publish + marker print).
    shell_result = (
        "computing rolling sharpe...\n"
        "publish ok: " + chart_id + "\n"
        f"@@nerya:chart@@ {chart_id}\n"
    )
    ids = extract_chart_marker_ids(shell_result)
    assert ids == [chart_id]

    # Artifact is loadable with the same store the kernel would use.
    store = ArtifactStore(WorkspacePaths(root=tmp_path))
    payload = load_chart_artifact(store, chart_id)
    assert payload is not None
    assert payload["title"] == "rolling sharpe (60d)"

    # Inline-extract returns nothing (the script never printed the
    # full block) — confirms the marker path is the *only* way the
    # kernel can recover this chart, and it works.
    assert extract_chart_blocks(shell_result) == []


def test_pipeline_handles_publish_then_marker_idempotency(tmp_path: Path) -> None:
    """Same chart_id, same call_id — splice should keep exactly one envelope."""

    from nerya.api.routes_charts import _post_publish
    from nerya.core.paths import WorkspacePaths

    class _StubClient:
        class _Cfg:
            paths = WorkspacePaths(root=tmp_path)

        config = _Cfg()

    block = {
        "chart_kind": "line",
        "title": "stable id test",
        "series": [{"type": "line", "name": "v", "data": [{"time": 1, "value": 1}]}],
        "source": {"skill": "agent", "action": "dyn"},
    }
    pub1 = _post_publish(_StubClient(), {"chart_block": block})
    pub2 = _post_publish(_StubClient(), {"chart_block": block})
    assert pub1["chart_id"] == pub2["chart_id"]

    chart_dict = pub1["chart_block"]
    outcome = LoopOutcome(
        transcript=[],
        iterations=1,
        stop_reason="end_turn",
        final_text="",
        tool_calls=1,
        error_count=0,
        blocks=[
            BlockEnvelope(
                seq=1,
                turn_id="t1",
                message_id="m1",
                role="tool",
                block={
                    "kind": "tool_result",
                    "call_id": "c1",
                    "action": "run_shell",
                    "ok": True,
                    "result": "ok",
                },
            ),
        ],
    )
    AgentKernel._splice_chart_blocks(outcome, [("c1", chart_dict)], turn_id="t1")
    AgentKernel._splice_chart_blocks(outcome, [("c1", chart_dict)], turn_id="t1")
    chart_envs = [e for e in outcome.blocks if e.block.get("kind") == "chart"]
    assert len(chart_envs) == 1
    assert chart_envs[0].block["chart_id"] == pub1["chart_id"]
