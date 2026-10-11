"""Database and billing regressions found in the isolated 18780 deployment."""
from contextlib import closing
import sqlite3
import pytest
from nerya.db.sqlite import connect
from nerya.db.migrations import apply_migrations
from nerya.llm.retry import is_quota_error, provider_retryable
from nerya.agent.command_runtime import _control_busy_can_retry
from nerya.llm.messages import _make_llm_error
from test_conversation_commands import CommandRuntime, config, payload

pytestmark = pytest.mark.smoke


def test_current_schema_and_healthy_queue_polling_do_not_write(tmp_path):
    runtime=CommandRuntime(config(tmp_path),lambda *_:{},epoch="test")
    runtime.submit(payload(),start=False)
    row=runtime.store.claim("conversation-1","worker")
    with closing(connect(runtime.config.paths.db)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with closing(connect(runtime.config.paths.db)) as reader:
            queries=[]
            reader.set_trace_callback(queries.append)
            assert apply_migrations(reader)==[]
            assert not any(s.upper().startswith(("BEGIN IMMEDIATE","CREATE","INSERT")) for s in queries)
        assert runtime.store.snapshot("conversation-1")["commands"][0]["state"]=="running"
        runtime._guides("conversation-1",row["turn_id"])


def test_short_contention_is_not_operator_cancellation():
    assert _control_busy_can_retry(sqlite3.OperationalError("database is locked"),100,105)
    assert not _control_busy_can_retry(sqlite3.OperationalError("database is locked"),100,111)
    assert not _control_busy_can_retry(sqlite3.OperationalError("disk I/O error"),100,101)


@pytest.mark.parametrize("doc",[
    {"error":{"message":"Insufficient balance or no resource package. Please recharge."}},
    {"error":"Insufficient balance"},{"message":"余额不足，请充值"}])
def test_billing_error_is_not_rate_limit(doc):
    assert is_quota_error(doc) and not provider_retryable(429,doc)
    error=_make_llm_error(provider="fixture",status=429,doc=doc,resp_headers={})
    assert error.code=="model_quota_exhausted" and error.retryable is False


def test_rpm_exhaustion_still_retries():
    assert provider_retryable(429,{"error":{"message":"rpm exhausted"}})
