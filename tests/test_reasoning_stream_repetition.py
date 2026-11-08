"""Synthetic reasoning streams only; no provider or private transcript fixtures."""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
import socket

import pytest

from nerya.agent.command_runtime import outcome_state
from nerya.agent.outcome_summary import render_outcome_summary
from nerya.core.errors import LLMError
from nerya.llm.adapters import _base
from nerya.llm.messages import MessagesRequest
from test_managed_io_polish import (
    StreamTransport, backend, delta, finish, gateway, make_loop, tool_chunks,
)


pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("NERYA_HOME", str(tmp_path))
    monkeypatch.setenv("NERYA_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("NERYA_PROFILE", "")

    def no_network(*args, **kwargs):
        pytest.fail("reasoning regression must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)


def synthetic_period(length):
    lines = [f"Synthetic observation {i}: {hashlib.sha256(str(i).encode()).hexdigest()}. "
             for i in range(length // 60 + 1)]
    return "".join(lines)[:length]


def reasoning_chunks(text, channel, chunk_size):
    if channel == "content":
        text = "<think>" + text + "</think>"
    return [delta(**{channel: text[i:i + chunk_size]})
            for i in range(0, len(text), chunk_size)]


class CountingStream(StreamTransport):
    def __init__(self, chunks):
        super().__init__(chunks)
        self.consumed = 0
        self.closed = False

    def stream_json(self, url, *, headers, body, timeout, on_event):
        self.calls.append(deepcopy(body))
        try:
            for chunk in self.chunks:
                self.consumed += 1
                on_event(chunk)
            return 200, {"stream_complete": True}, {}
        finally:
            self.closed = True


def test_tool_argument_repetition_discards_partial_without_replay():
    emitted = []
    request = MessagesRequest(system="fixture", messages=[], stream=True,
        metadata={"tool_execution_boundary": "response_complete"}, on_event=emitted.append)
    for cycle, chunk_size in [("caf1ef880c5bb971258", 7), ("x", 137),
                              (synthetic_period(363), 100000)]:
        arguments = json.dumps({"value": cycle * 10000})
        chunks = [delta(tool_calls=[{"index": 0, "id": "call-fixture",
            "function": {"name": "fixture_write", "arguments": ""}}])]
        chunks += [delta(tool_calls=[{"index": 0,
            "function": {"arguments": arguments[i:i + chunk_size]}}])
            for i in range(0, len(arguments), chunk_size)]
        chunks.append(finish("tool_calls"))
        transport = CountingStream(chunks)
        emitted.clear()
        with pytest.raises(LLMError) as caught:
            backend(transport)(request)
        error = caught.value
        assert error.code == "model_tool_argument_repetition"
        assert error.retryable is False and error.stream_request_replay_safe is False
        assert error.stream_interrupted is True
        assert transport.closed and transport.consumed < len(chunks)
        assert len(transport.calls) == 1
        assert not any(e["type"] == "tool_use" for e in emitted)
        assert sum(len(e.get("partial_json", "")) for e in emitted) < len(arguments)
        assert cycle not in str(error)

    # Each tool can deliberately contain three copies; never combine tools or
    # requests into one repetition history. Long, varying source remains valid.
    code = "\n".join(f"value_{i} = {i}  # generated source line {i}" for i in range(6000))
    inputs = [{"value": synthetic_period(1024) * 3}] * 2 + [{"code": code}]
    chunks = []
    for index, value in enumerate(inputs):
        arguments = json.dumps(value)
        chunks.append(delta(tool_calls=[{"index": index, "id": f"call-{index}",
            "function": {"name": "fixture_write", "arguments": ""}}]))
        chunks += [delta(tool_calls=[{"index": index,
            "function": {"arguments": arguments[i:i + 137]}}])
            for i in range(0, len(arguments), 137)]
    transport = CountingStream([*chunks, finish("tool_calls")])
    adapter = backend(transport)
    for _ in range(2):
        response = adapter(request)
        assert response.stop_reason == "tool_use"
        assert [tool["input"] for tool in response.tool_uses()] == inputs
    assert len(transport.calls) == 2


@pytest.mark.parametrize("channel", ["reasoning_content", "reasoning", "content"])
@pytest.mark.parametrize("period,chunk_size", [(363, 37), (6143, 211), (6143, 100000)])
def test_periodic_reasoning_stops_without_retry_or_tool_commit(channel, period, chunk_size):
    text = synthetic_period(period) * 20
    chunks = [*tool_chunks()[:1], *reasoning_chunks(text, channel, chunk_size),
              *tool_chunks()[1:]]
    transport = CountingStream(chunks)
    emitted = []
    request = MessagesRequest(system="fixture", messages=[], stream=True,
        metadata={"tool_execution_boundary": "response_complete"}, on_event=emitted.append)

    with pytest.raises(LLMError) as caught:
        backend(transport)(request)

    error = caught.value
    assert error.code == "model_reasoning_repetition"
    assert error.retryable is False
    assert error.stream_request_replay_safe is False
    assert error.stream_interrupted is True
    assert transport.closed and transport.consumed < len(chunks)
    # Allow the crossing chunk, but never consume a fifth long-period copy.
    assert (transport.consumed - 2) * chunk_size < max(period * 4, 2048 + period) + 256
    assert len(transport.calls) == 1
    assert sum(len(e.get("text", "")) for e in emitted) < len(text)
    assert synthetic_period(period) not in str(error)


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_loop_does_not_continue_repetition_or_replay_completed_tool(
    tmp_path, monkeypatch, finish_reason,
):
    first = StreamTransport(tool_chunks())
    repeating = CountingStream([
        *reasoning_chunks(synthetic_period(6143) * 20, "reasoning_content", 211),
        finish(finish_reason),
    ])
    backup = StreamTransport([delta(content="unexpected fallback"), finish()])
    gw = gateway(tmp_path, monkeypatch, [first, backup])

    def resolve(*args, **kwargs):
        if kwargs["route_cfg"]["provider"] == "fixture1":
            return backend(backup)
        return backend(repeating if first.calls else first)

    monkeypatch.setattr(gw, "_resolve_messages_backend", resolve)
    events, writes = [], []
    loop = make_loop(gw, events, writes)
    loop.config.llm_retry_base_delay = loop.config.llm_retry_max_delay = 0
    result = loop.run(system="fixture", user_message="请执行测试任务", turn_id="repeated")

    assert result.aborted and result.stop_reason == "stream_interrupted"
    assert result.abort_reason == "stream_interrupted"
    assert result.iterations == 2 and result.extra_llm_attempts == 0
    assert len(first.calls) == len(repeating.calls) == 1 and not backup.calls
    assert repeating.closed and repeating.consumed < len(repeating.chunks)
    assert writes == [{"value": 1}] and result.tool_calls == 1
    assert result.final_text == render_outcome_summary(
        reason="model_reasoning_repetition", user_text="请执行测试任务", tool_calls=1,
    )
    assert "Synthetic observation" not in result.final_text
    assert "fixture write succeeded" not in result.final_text
    assert outcome_state({"stopped_reason": result.stop_reason, "aborted": result.aborted}) == "interrupted"
    interruptions = [e.block for e in events if e.block.get("status") == "interrupted"]
    assert len(interruptions) == 1 and interruptions[0]["retryable"] is False
    assert not any(e.block.get("status") == "discarded" or e.block.get("retry") for e in events)


def test_loop_never_executes_tool_preview_from_repetitive_response(tmp_path, monkeypatch):
    transport = CountingStream([*tool_chunks()[:-1],
        *reasoning_chunks(synthetic_period(363) * 20, "reasoning_content", 37),
        finish("tool_calls")])
    writes = []
    loop = make_loop(gateway(tmp_path, monkeypatch, [transport]), [], writes)
    result = loop.run(system="fixture", user_message="fixture")
    assert result.aborted and result.stop_reason == "stream_interrupted"
    assert result.tool_calls == 0 and not writes
    assert result.extra_llm_attempts == 0 and len(transport.calls) == 1


@pytest.mark.parametrize("channel", ["reasoning_content", "content"])
def test_long_nonperiodic_reasoning_is_not_capped(channel):
    text = synthetic_period(200000)
    transport = StreamTransport([*reasoning_chunks(text, channel, 137),
                                 delta(content="Complete."), finish()])
    emitted = []
    response = backend(transport)(MessagesRequest(
        system="fixture", messages=[], stream=True, on_event=emitted.append,
    ))
    assert response.text() == "Complete."
    assert "".join(e["text"] for e in emitted if e["type"] == "thinking_delta") == text


def test_repeated_visible_output_is_not_reasoning():
    text = synthetic_period(363) * 20
    transport = StreamTransport([delta(content=text), finish()])
    assert backend(transport)(MessagesRequest(
        system="fixture", messages=[], stream=True,
    )).text() == text


@pytest.mark.parametrize("progress", ["text", "tool"])
def test_progress_separates_nonconsecutive_reasoning_cycles(progress):
    # Three copies can be deliberate review; a new visible/tool step separates
    # the next reasoning phase even when it revisits the same material.
    text = synthetic_period(6143) * 3
    separator = delta(content="Next step.") if progress == "text" else tool_chunks()[0]
    tail = [finish()] if progress == "text" else tool_chunks()[1:]
    transport = StreamTransport([
        *reasoning_chunks(text, "reasoning_content", 211), separator,
        *reasoning_chunks(text, "reasoning_content", 37), *tail,
    ])
    response = backend(transport)(MessagesRequest(system="fixture", messages=[], stream=True))
    assert response.stop_reason == ("end_turn" if progress == "text" else "tool_use")


def test_reasoning_guard_is_per_request():
    text = synthetic_period(6143) * 3
    transport = StreamTransport([*reasoning_chunks(text, "reasoning_content", 211), finish()])
    adapter = backend(transport)
    for _ in range(2):
        assert adapter(MessagesRequest(system="fixture", messages=[], stream=True)).stop_reason == "end_turn"
    assert len(transport.calls) == 2


def test_repetition_unwinds_actual_sse_transport_without_reading_terminal(monkeypatch):
    chunks = [*reasoning_chunks(synthetic_period(6143) * 20, "reasoning_content", 211),
              finish("length")]

    class Response:
        status = 200
        headers = {"content-type": "text/event-stream"}

        def __init__(self):
            self.lines = [line for event in chunks
                          for line in (("data: " + json.dumps(event) + "\n").encode(), b"\n")]
            self.lines.extend([b"data: [DONE]\n", b"\n"])
            self.read_count = 0
            self.closed = False

        def readline(self, limit):
            line = self.lines[self.read_count]
            self.read_count += 1
            return line

    response = Response()

    @contextmanager
    def opened(*args):
        try:
            yield response
        finally:
            response.closed = True

    monkeypatch.setattr(_base, "_managed_urlopen", opened)
    with pytest.raises(LLMError) as caught:
        backend(_base.UrllibTransport())(MessagesRequest(system="fixture", messages=[], stream=True))
    assert caught.value.code == "model_reasoning_repetition"
    assert caught.value.retryable is False
    assert response.closed and response.read_count < len(response.lines) - 4
