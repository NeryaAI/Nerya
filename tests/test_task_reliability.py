"""Regression cases from the 18780 multi-agent interruption incident. Offline only."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import ssl
import time

import pytest

from nerya.db.sqlite import connect
from nerya.db.migrations import apply_migrations
from nerya.llm.retry import is_quota_error, provider_retryable
from test_conversation_commands import CommandRuntime, config, payload
from nerya.agent.command_runtime import _control_busy_can_retry
from nerya.core.errors import LLMError
from nerya.llm.messages import MessagesRequest, OpenAIMessagesBackend, _make_llm_error
from nerya.agent.loop import LoopConfig
from test_agent_loop_context_recovery import _make_loop, _tool_use_response

pytestmark = pytest.mark.smoke


def test_current_schema_never_requests_a_write_lock(tmp_path):
    db = tmp_path / "nerya.db"
    with closing(connect(db)) as writer, closing(sqlite3.connect(db, timeout=0.05)) as reader:
        writer.execute("BEGIN IMMEDIATE")
        queries = []
        reader.set_trace_callback(queries.append)
        assert apply_migrations(reader) == []
        assert not any(sql.upper().startswith(("BEGIN IMMEDIATE", "CREATE", "INSERT")) for sql in queries)


def test_current_connection_opens_during_a_writer_transaction(tmp_path):
    db = tmp_path / "nerya.db"
    with closing(connect(db)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with closing(connect(db)) as reader:
            assert reader.execute("SELECT count(*) FROM schema_version").fetchone()[0] > 0


def test_healthy_command_snapshot_and_idle_guides_are_read_only(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="test")
    runtime.submit(payload(), start=False)
    row = runtime.store.claim("conversation-1", "worker")
    with closing(connect(runtime.config.paths.db)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        assert runtime.store.snapshot("conversation-1")["commands"][0]["state"] == "running"
        assert runtime.store.snapshot("conversation-1", "command-001")["command"]["state"] == "running"
        runtime._guides("conversation-1", row["turn_id"])


@pytest.mark.parametrize("doc", [
    {"error": {"message": "Insufficient balance or no resource package. Please recharge."}},
    {"message": "Insufficient balance or no resource package. Please recharge."},
    {"error": "Insufficient balance or no resource package. Please recharge."},
    {"error": {"message": "余额不足，请充值"}},
    {"error": {"code": "insufficient_quota"}},
])
def test_balance_exhaustion_is_not_an_rpm_retry(doc):
    assert is_quota_error(doc)
    assert not provider_retryable(429, doc)


@pytest.mark.parametrize("message", ["rpm exhausted", "Rate limit exceeded", "Too many requests", "token rate limit exceeded"])
def test_temporary_rate_limits_remain_retryable(message):
    doc = {"error": {"message": message}}
    assert not is_quota_error(doc)
    assert provider_retryable(429, doc)


def test_only_short_database_contention_is_tolerated():
    assert _control_busy_can_retry(sqlite3.OperationalError("database is locked"), 100, 105)
    assert not _control_busy_can_retry(sqlite3.OperationalError("database is locked"), 100, 111)
    assert not _control_busy_can_retry(sqlite3.OperationalError("disk I/O error"), 100, 101)
    assert not _control_busy_can_retry(RuntimeError("database is locked"), 100, 101)


def test_concurrent_writers_and_polling_keep_every_event(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="test")
    runtime.submit(payload(), start=False)
    runtime.store.claim("conversation-1", "worker")
    def work(index):
        for number in range(30):
            if index % 2:
                runtime.store.record_event("command-001", {"event_id":f"{index}:{number}", "kind":"message.delta", "text":"fixture"})
            else:
                assert runtime.store.snapshot("conversation-1")["commands"][0]["state"] == "running"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work,range(8)))
    assert len(runtime.store.events("conversation-1","command-001")["events"]) == 120


def test_quota_error_preserves_cause_without_secret_body_in_command_code():
    error = _make_llm_error(provider="fixture",status=429,
        doc={"error":{"message":"Insufficient balance or no resource package. Please recharge."}},
        resp_headers={"x-request-id":"fixture-request"})
    assert error.code == "model_quota_exhausted"
    assert error.retryable is False
    assert error.request_id == "fixture-request"


class Disconnect:
    def __init__(self, fragment=None, error=None):
        self.fragment,self.error,self.calls=fragment,error,0

    def stream_json(self,url,*,headers,body,timeout,on_event):
        self.calls += 1
        if self.calls == 1 or self.error:
            if self.fragment:
                on_event({"choices":[{"index":0,"delta":self.fragment}]})
            raise self.error or ConnectionResetError("fixture stream disconnected")
        on_event({"choices":[{"index":0,"delta":{"content":"Recovered from saved evidence."}}]})
        on_event({"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]})
        return 200,{"stream_complete":True},{}


@pytest.mark.parametrize("fragment",[None,{"content":"partial"},{"reasoning_content":"partial thought"},
    {"tool_calls":[{"index":0,"id":"partial-call","function":{"name":"read_status","arguments":"{"}}]}])
def test_stream_retries_only_before_any_model_output(fragment):
    transport=Disconnect(fragment)
    backend=OpenAIMessagesBackend(api_key="fixture",model="gpt-4o",transport=transport)
    with pytest.raises(LLMError) as caught:
        backend(MessagesRequest(system="fixture",messages=[],stream=True))
    assert caught.value.stream_output_committed is bool(fragment)
    assert caught.value.retryable is (fragment is None)
    assert transport.calls==1


def test_tls_failure_is_not_retried():
    transport=Disconnect(error=ssl.SSLCertVerificationError("fixture invalid certificate"))
    backend=OpenAIMessagesBackend(api_key="fixture",model="gpt-4o",transport=transport)
    with pytest.raises(LLMError) as caught:
        backend(MessagesRequest(system="fixture",messages=[],stream=True))
    assert caught.value.code=="model_stream_tls_error" and caught.value.retryable is False


def test_pre_output_recovery_keeps_completed_tool_once():
    transport=Disconnect()
    backend=OpenAIMessagesBackend(api_key="fixture",model="gpt-4o",transport=transport)
    class Gateway:
        calls=0
        def call_messages(self,**kwargs):
            self.calls+=1
            if self.calls==1:
                return _tool_use_response("read-once")
            return backend(MessagesRequest(system=kwargs["system"],messages=kwargs["messages"],
                tools=kwargs.get("tools",[]),stream=True,on_event=kwargs.get("on_event")))
    loop=_make_loop(Gateway(),config=LoopConfig(max_iterations=5,llm_retry_base_delay=0,llm_retry_max_delay=0))
    result=loop.run(system="fixture",user_message="Read status then report.")
    assert not result.aborted and result.final_text=="Recovered from saved evidence."
    assert result.tool_calls==1 and transport.calls==2 and result.extra_llm_attempts==1


def test_partial_stream_is_not_replayed_as_a_new_tool():
    transport=Disconnect({"tool_calls":[{"index":0,"id":"partial-call","function":{"name":"read_status","arguments":"{"}}]})
    backend=OpenAIMessagesBackend(api_key="fixture",model="gpt-4o",transport=transport)
    class Gateway:
        def call_messages(self,**kwargs):
            return backend(MessagesRequest(system=kwargs["system"],messages=kwargs["messages"],stream=True,on_event=kwargs.get("on_event")))
    result=_make_loop(Gateway(),config=LoopConfig(max_iterations=5)).run(system="fixture",user_message="Read status.")
    assert result.aborted and result.abort_reason=="stream_interrupted"
    assert result.tool_calls==0 and transport.calls==1
    assert "stream was interrupted" in result.final_text
