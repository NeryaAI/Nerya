from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.agent.kernel import AgentKernel
from nerya.agent.session_compaction import (
    CHECKPOINT_HEADER,
    SESSION_COMPACTION_META_KEY,
    SessionCompactionPolicy,
    compact_session_history,
)
from nerya.agent.transcript_compact import compact_transcript
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.db.repositories import AgentSessionRepository
from nerya.db.sqlite import connect
from nerya.llm.messages import _openai_render_messages


pytestmark = pytest.mark.smoke


def _row(i: int, role: str, content: str) -> dict:
    return {
        "message_id": f"m{i}",
        "session_id": "s1",
        "turn_id": f"t{i // 2}",
        "role": role,
        "content": content,
        "ts": float(1000 + i),
        "meta_json": "{}",
    }


def _config(tmp_path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data.setdefault("agent", {}).setdefault("native", {})
    return Config(paths=WorkspacePaths(root=tmp_path), data=data)


def test_session_compaction_injects_checkpoint_and_keeps_recent_tail() -> None:
    rows = [
        _row(0, "user", "Please implement context compression for nerya/agent/kernel.py."),
        _row(1, "assistant", "Decision: preserve recent tail and add a checkpoint."),
        _row(2, "user", "Also remember tests/test_session_compaction.py must cover it."),
        _row(3, "assistant", "Verified plan: update repository meta and kernel replay."),
        _row(4, "user", "Recent question A"),
        _row(5, "assistant", "Recent answer A"),
        _row(6, "user", "Recent question B"),
        _row(7, "assistant", "Recent answer B"),
    ]

    result = compact_session_history(
        rows,
        policy=SessionCompactionPolicy(keep_recent_pairs=2, trigger_pairs=2),
    )

    assert result.checkpoint is not None
    assert len(result.messages) == 5
    checkpoint = result.messages[0]["content"]
    assert CHECKPOINT_HEADER in checkpoint
    assert "context compression" in checkpoint
    assert "nerya/agent/kernel.py" in checkpoint
    assert "tests/test_session_compaction.py" in checkpoint
    assert "Recent question A" == result.messages[1]["content"]
    assert "Recent answer B" == result.messages[-1]["content"]


def test_session_compaction_merges_incrementally_without_replaying_old_span() -> None:
    policy = SessionCompactionPolicy(keep_recent_pairs=1, trigger_pairs=1)
    first_rows = [
        _row(0, "user", "Read nerya/agent/loop.py and preserve tool pairs."),
        _row(1, "assistant", "Decision: keep tool_use/tool_result pairs together."),
        _row(2, "user", "Recent one"),
        _row(3, "assistant", "Recent answer one"),
    ]
    first = compact_session_history(first_rows, policy=policy)

    second_rows = first_rows + [
        _row(4, "user", "Add docs/plans/context.md to the artifact trail."),
        _row(5, "assistant", "Open thread: run focused tests next."),
        _row(6, "user", "Recent two"),
        _row(7, "assistant", "Recent answer two"),
    ]
    second = compact_session_history(
        second_rows,
        existing_checkpoint=first.checkpoint,
        policy=policy,
    )

    assert second.checkpoint is not None
    rendered = second.checkpoint["rendered"]
    assert "nerya/agent/loop.py" in rendered
    assert "docs/plans/context.md" in rendered
    assert second.checkpoint["digest"]["user_requests"].count(
        "Read nerya/agent/loop.py and preserve tool pairs."
    ) == 1
    assert second.messages[-2]["content"] == "Recent two"


def test_kernel_prior_history_uses_persisted_session_checkpoint(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    con = connect(cfg.paths.db)
    repo = AgentSessionRepository(con)
    repo.upsert_session(session_id="s1", title="Session")
    for i in range(10):
        role = "user" if i % 2 == 0 else "assistant"
        repo.record_message(
            message_id=f"m{i}",
            session_id="s1",
            turn_id=f"t{i // 2}",
            role=role,
            content=(
                "Please edit nerya/agent/kernel.py"
                if i == 0
                else f"{role} message {i}"
            ),
            ts=float(1000 + i),
        )

    prior = kernel._load_prior_chat_messages(session_id="s1", max_pairs=2)
    session_row = repo.get_session("s1")
    con.close()

    assert prior[0]["role"] == "user"
    assert CHECKPOINT_HEADER in prior[0]["content"]
    assert "nerya/agent/kernel.py" in prior[0]["content"]
    assert [m["content"] for m in prior[-4:]] == [
        "user message 6",
        "assistant message 7",
        "user message 8",
        "assistant message 9",
    ]
    assert session_row is not None
    assert SESSION_COMPACTION_META_KEY in session_row["meta_json"]


def test_transcript_compact_breadcrumb_carries_structured_signal() -> None:
    messages = [
        {"role": "user", "content": "Read nerya/agent/kernel.py first."},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "read_file",
                    "input": {"path": "nerya/agent/kernel.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": [
                        {"type": "text", "text": "contents from nerya/agent/kernel.py"}
                    ],
                }
            ],
        },
        {"role": "assistant", "content": "Decision: keep a checkpoint."},
        {"role": "user", "content": "tail question"},
        {"role": "assistant", "content": "tail answer"},
    ]

    compacted, report = compact_transcript(
        messages,
        keep_tail_messages=2,
        max_messages=3,
    )

    assert report.summary_inserted is True
    breadcrumbs = [
        m for m in compacted
        if m.get("kind") == "transcript.compact.breadcrumb"
    ]
    assert breadcrumbs, "expected a compaction breadcrumb"
    breadcrumb = breadcrumbs[0]["content"]
    assert "Preserved summary" in breadcrumb
    assert "read_file" in breadcrumb
    assert "nerya/agent/kernel.py" in breadcrumb
    # The operator user anchor survives compaction ahead of the breadcrumb.
    assert compacted[0]["content"] == "Read nerya/agent/kernel.py first."
    assert compacted[-1]["content"] == "tail answer"


def test_transcript_compact_preserves_operator_user_anchor_for_chat_shape() -> None:
    messages: list[dict] = [{"role": "user", "content": "Create and backtest a BTC strategy."}]
    for i in range(8):
        call_id = f"call_{i}"
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": "read_status",
                            "input": {"i": i},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": [{"type": "text", "text": f"result {i}"}],
                        }
                    ],
                },
            ]
        )

    compacted, report = compact_transcript(
        messages,
        keep_tail_messages=4,
        max_messages=6,
    )
    rendered = _openai_render_messages(system="system", messages=compacted)
    non_system_roles = [m["role"] for m in rendered if m["role"] != "system"]

    assert report.summary_inserted is True
    assert {"role": "user", "content": "Create and backtest a BTC strategy."} in compacted
    assert non_system_roles[0] == "user"
