"""A model-preview failure must not replay an already executed client tool."""
from copy import deepcopy
import json
import ssl

import pytest

from nerya.core.errors import LLMError
from nerya.harness.cancellation import CancelToken
from nerya.llm.messages import MessagesRequest
from test_managed_io_polish import (
    StreamTransport, backend, delta, finish, gateway, make_loop, tool_chunks,
)

pytestmark = pytest.mark.smoke


class InterruptedOnce(StreamTransport):
    def stream_json(self, url, *, headers, body, timeout, on_event):
        if not self.calls:
            self.calls.append(deepcopy(body))
            for chunk in self.chunks:
                on_event(chunk)
            raise ConnectionResetError("temporary stream loss")
        self.chunks = [delta(content="Verified from saved evidence."), finish()]
        return super().stream_json(url, headers=headers, body=body, timeout=timeout, on_event=on_event)


@pytest.mark.parametrize("chunks", [
    [delta(content="unfinished answer")],
    [delta(reasoning_content="unfinished reasoning")],
    tool_chunks()[:1],
    tool_chunks()[:-1],  # Even valid JSON is a preview until the response ends.
])
def test_loop_discards_preview_and_keeps_previous_write_exactly_once(tmp_path, monkeypatch, chunks):
    first = StreamTransport(tool_chunks())
    interrupted = InterruptedOnce(chunks)
    writes, events = [], []
    gw = gateway(tmp_path, monkeypatch, [first])
    calls = []

    def resolve(*args, **kwargs):
        chosen = first if not calls else interrupted
        calls.append(chosen)
        return backend(chosen)

    monkeypatch.setattr(gw, "_resolve_messages_backend", resolve)
    loop = make_loop(gw, events, writes)
    loop.config.llm_retry_base_delay = loop.config.llm_retry_max_delay = 0
    result = loop.run(system="fixture", user_message="execute fixture", turn_id="buffered")
    assert not result.aborted and result.final_text == "Verified from saved evidence."
    assert writes == [{"value": 1}] and result.tool_calls == 1
    assert result.extra_llm_attempts == 1
    assert result.extra_llm_attempts_by_reason == {"buffered_stream_retry": 1}
    assert interrupted.calls[0]["messages"] == interrupted.calls[1]["messages"]
    assert "fixture write succeeded" in json.dumps(interrupted.calls[1]["messages"])
    discarded = [e.block for e in events if e.block.get("status") == "discarded"]
    assert len(discarded) == 1 and discarded[0]["stream_id"] == "buffered:2:1"
    final = [e.block for e in events if e.block["kind"] == "text"]
    assert final[-1]["stream_id"] == "buffered:2:2"


def test_buffered_retry_is_bounded_without_executing_partial_tool(tmp_path, monkeypatch):
    class AlwaysInterrupted(InterruptedOnce):
        def stream_json(self, url, *, headers, body, timeout, on_event):
            self.calls.append(deepcopy(body))
            for chunk in self.chunks:
                on_event(chunk)
            raise ConnectionResetError("still unavailable")

    transport = AlwaysInterrupted(tool_chunks()[:-1])
    writes, events = [], []
    loop = make_loop(gateway(tmp_path, monkeypatch, [transport]), events, writes)
    loop.config.llm_retry_attempts = 2
    loop.config.llm_retry_base_delay = loop.config.llm_retry_max_delay = 0
    result = loop.run(system="fixture", user_message="fixture")
    assert result.aborted and result.abort_reason == "stream_interrupted"
    assert len(transport.calls) == 2 and not writes and result.tool_calls == 0
    assert result.extra_llm_attempts == 1


@pytest.mark.parametrize("native_search", [False, True])
def test_explicit_boundary_does_not_authorize_upstream_capability_replay(native_search):
    transport = InterruptedOnce([delta(content="partial")])
    request = MessagesRequest(system="fixture", messages=[], stream=True, metadata={
        "tool_execution_boundary": "response_complete",
        "provider_native_web_search": {"enabled": native_search},
    })
    with pytest.raises(LLMError) as caught:
        backend(transport)(request)
    assert caught.value.stream_request_replay_safe is (not native_search)
    assert caught.value.retryable is (not native_search)
    assert len(transport.calls) == 1  # Retry belongs to the loop, not the adapter.


def test_cancel_after_discard_starts_no_second_request(tmp_path, monkeypatch):
    transport = InterruptedOnce(tool_chunks()[:1])
    writes, events = [], []
    token = CancelToken()
    loop = make_loop(gateway(tmp_path, monkeypatch, [transport]), events, writes)
    original_sink = loop.event_sink

    def sink(event):
        original_sink(event)
        if event.block.get("status") == "discarded":
            token.cancel("operator_cancel")

    loop.event_sink = sink
    loop.config.llm_retry_base_delay = loop.config.llm_retry_max_delay = 0
    result = loop.run(system="fixture", user_message="fixture", cancel_token=token)
    assert result.aborted and result.abort_reason.startswith("cancelled")
    assert len(transport.calls) == 1 and not writes


@pytest.mark.parametrize("error", [ssl.SSLEOFError("TLS failure"), ValueError("parser failure")])
def test_buffered_boundary_never_promotes_permanent_errors(error):
    class Broken:
        def stream_json(self, *args, on_event, **kwargs):
            on_event(delta(content="partial"))
            raise error

    with pytest.raises(LLMError) as caught:
        backend(Broken())(MessagesRequest(system="fixture", messages=[], stream=True,
            metadata={"tool_execution_boundary": "response_complete"}))
    assert caught.value.retryable is False
    assert caught.value.stream_request_replay_safe is False


@pytest.mark.parametrize("missing", ["done", "finish_reason"])
def test_original_missing_terminal_frame_recovers_with_unchanged_input(tmp_path, monkeypatch, missing):
    class MissingTerminalOnce(InterruptedOnce):
        def stream_json(self, url, *, headers, body, timeout, on_event):
            if not self.calls:
                self.calls.append(deepcopy(body))
                on_event(delta(content="unfinished"))
                if missing == "done":
                    on_event(finish())
                return 200, {"stream_complete": missing != "done"}, {}
            self.chunks = [delta(content="Recovered."), finish()]
            return StreamTransport.stream_json(self, url, headers=headers, body=body,
                timeout=timeout, on_event=on_event)

    transport = MissingTerminalOnce([])
    writes, events = [], []
    loop = make_loop(gateway(tmp_path, monkeypatch, [transport]), events, writes)
    loop.config.llm_retry_base_delay = loop.config.llm_retry_max_delay = 0
    outcome = loop.run(system="fixture", user_message="fixture")
    assert not outcome.aborted and outcome.final_text == "Recovered."
    assert len(transport.calls) == 2 and not writes
    assert transport.calls[0]["messages"] == transport.calls[1]["messages"]
    assert sum(e.block.get("status") == "discarded" for e in events) == 1


def test_partial_recovery_cannot_bypass_turn_shared_attempt_budget(tmp_path, monkeypatch):
    transport = InterruptedOnce([delta(content="unfinished")])
    loop = make_loop(gateway(tmp_path, monkeypatch, [transport]), [], [])
    loop.config.max_extra_llm_attempts_per_turn = 0
    loop.config.llm_retry_base_delay = loop.config.llm_retry_max_delay = 0
    outcome = loop.run(system="fixture", user_message="fixture")
    assert outcome.aborted and len(transport.calls) == 1
    assert outcome.extra_llm_attempts == 0
