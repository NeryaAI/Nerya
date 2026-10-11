"""Replay log batches model tokens without losing ordered durable evidence."""
from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.agent.command_event_buffer import CommandEventBuffer
from nerya.agent.streaming import get_default_bus
from test_conversation_commands import CommandRuntime, config, payload

pytestmark = pytest.mark.smoke


def thought(index: int, value: str = "字") -> dict:
    return {
        "event_id": f"thought-{index}", "kind": "turn.step", "seq": index + 1,
        "mode": "append", "stream_mode": "stream", "stream_id": "t:1:1",
        "session_id": "conversation-1", "turn_id": "turn-fixture",
        "step": {"kind": "thinking", "status": "running", "detail": {"text": value, "summary": ""}},
    }


def test_token_storm_is_batched_but_replay_text_and_receipts_remain_exact(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    runtime.submit(payload(), start=False)
    stream = CommandEventBuffer(runtime.store, "command-001")
    original = []
    for n in range(12000):
        event = thought(n)
        if n == 4000:
            event["stream_id"] = "t:1:2"
        original.append(event)
        stream.append(event)
    stream.append({"event_id": "tool-start", "kind": "tool.start", "action": "read_status"})
    stream.append({"event_id": "tool-done", "kind": "tool.complete", "action": "read_status", "ok": True})
    stream.close()
    stream.close()
    records = []
    cursor = 0
    while True:
        page = runtime.store.events("conversation-1", "command-001", after=cursor, limit=500)
        records.extend(page["events"])
        cursor = page["cursor"]
        if not page["has_more"]:
            break
    deltas = [item for item in records if item["kind"] == "turn.step"]
    assert "".join(event["step"]["detail"]["text"] for event in deltas) == "字" * 12000
    assert len(deltas) < 100
    assert [item["event_id"] for item in records[-2:]] == ["tool-start", "tool-done"]
    assert original[0]["step"]["detail"]["text"] == "字"


def test_live_text_and_interleaved_receipts_preserve_stream_boundaries(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    runtime.submit(payload(), start=False)
    buffer = CommandEventBuffer(runtime.store, "command-001")
    messages = [
        {"event_id": "delta-a", "kind": "message.delta", "stream_id": "a", "mode": "append",
         "session_id": "conversation-1", "turn_id": "t", "text": "hello"},
        {"event_id": "delta-b", "kind": "message.delta", "stream_id": "a", "mode": "append",
         "session_id": "conversation-1", "turn_id": "t", "text": " world"},
        {"event_id": "tool", "kind": "tool.complete", "action": "write", "ok": True},
        {"event_id": "delta-c", "kind": "message.delta", "stream_id": "a", "mode": "append",
         "session_id": "conversation-1", "turn_id": "t", "text": "after tool"},
        {"event_id": "delta-d", "kind": "message.delta", "stream_id": "b", "mode": "append",
         "session_id": "conversation-1", "turn_id": "t", "text": "new stream"},
    ]
    snapshot = deepcopy(messages)
    for item in messages:
        buffer.append(item)
    buffer.close()
    replay = runtime.store.events("conversation-1", "command-001")["events"]
    assert [(r["kind"], r.get("text", "")) for r in replay] == [
        ("message.delta", "hello world"), ("tool.complete", ""),
        ("message.delta", "after tool"), ("message.delta", "new stream"),
    ]
    assert messages == snapshot


def test_failed_flush_keeps_uncommitted_fragments_for_safe_retry():
    class Once:
        attempts = 0
        stored: list[dict] = []
        def record_events(self, cid, events):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("temporary IO failure")
            self.stored.extend(deepcopy(events))
    store = Once()
    stream = CommandEventBuffer(store, "command-001")
    stream.append(thought(0))
    with pytest.raises(OSError, match="temporary IO"):
        stream.close()
    stream.close()
    assert len(store.stored) == 1
    assert store.stored[0]["step"]["detail"]["text"] == "字"


def test_real_command_worker_projects_batched_live_deltas(tmp_path):
    bus = get_default_bus()

    def execute(config, request):
        for n in range(1000):
            bus.publish("turn.step", **{key: value for key, value in thought(n).items()
                                        if key not in ("kind", "seq", "event_id", "turn_id")},
                        turn_id=request["turn_id"])
        bus.publish("tool.complete", session_id=request["session_id"], turn_id=request["turn_id"],
                    action="fixture_read", ok=True)
        return {"stopped_reason": "end_turn", "final_text": "Read complete"}

    runtime = CommandRuntime(config(tmp_path), execute, epoch="a")
    runtime.submit(payload(), start=False)
    claim = runtime.store.claim("conversation-1", "owner")
    runtime._run(claim, "owner")
    command = runtime.store.snapshot("conversation-1", "command-001")["command"]
    assert command["state"] == "succeeded"
    events = runtime.store.events("conversation-1", "command-001")["events"]
    assert "".join(e["step"]["detail"]["text"] for e in events
                   if e["kind"] == "turn.step") == "字" * 1000
    assert len(events) < 20
    assert [e["kind"] for e in events][-2:] == ["tool.complete", "command.outcome"]
