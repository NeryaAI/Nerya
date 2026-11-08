"""Real canonicalization rejection, serialized and stored only in tmp_path."""
from contextlib import closing
import inspect
import json

import pytest

from nerya.agent.command_store import CommandStore
from nerya.db.sqlite import connect_readonly
from nerya.strategies import request_scope as scope
from nerya.tools.types import ToolCall
from test_request_scope import env, FakeGateway, admit, install_fake, spans_output

pytestmark = pytest.mark.smoke


def test_original_document_amendment_has_durable_host_check(env, monkeypatch, caplog):
    canary = 'PRIVATE_MODEL_BODY_19ad'
    admit(env, cid='lp-rejection', sid='lp-rejection', text=(
        '创建 Ethereum Uniswap V3 WETH/USDC LP 策略，不交易。 ' + canary))

    def choose(docs):
        evidence = spans_output(docs[0], protocol='Uniswap V3', chain='Ethereum',
                                base='WETH', quote='USDC')
        # This is schema-valid with exact spans, but the host rejects an
        # amendment to the first request in the real _canonical check.
        evidence['amendments'] = [evidence['fields']['base'].copy()]
        return evidence

    gateway = FakeGateway(choose)
    install_fake(monkeypatch, gateway)
    source, first = inspect.getsourcelines(scope._canonical)
    rejection_line = first + next(i + 1 for i, line in enumerate(source)
                                  if line.strip() == 'if doc is docs[0]:')
    result, pinned = scope.guard(env, ToolCall(name='strategy_draft_proposal'))
    assert result.is_error and pinned == {} and len(gateway.calls) == 1
    metadata = {'reason': 'host_check_rejected', 'host_check': '_canonical',
                'host_check_line': rejection_line}
    data = result.content[0].data
    for envelope in (data, data['next_required_action'], result.error.detail, result.error.recovery_hint):
        assert envelope['diagnostic_code'] == 'invalid_semantics'
        assert envelope['diagnostic_metadata'] == metadata
    assert result.error.retryable is False
    assert result.error.recovery_hint['safe_to_retry'] is False
    assert result.error.recovery_hint['preserve_candidate'] is True

    # Exercise existing event persistence, without a runtime or service.
    store = CommandStore(env.paths, epoch='offline')
    store.record_event('lp-rejection', {'event_id': 'host-rejection',
        'kind': 'tool.complete', 'recovery': result.error.recovery_hint,
        'result': result.asdict()})
    with closing(connect_readonly(env.paths.db)) as con:
        event = json.loads(con.execute(
            'SELECT payload_json FROM agent_command_events WHERE command_id=?',
            ('lp-rejection',)).fetchone()[0])
        context = json.loads(con.execute(
            'SELECT context_json FROM agent_commands WHERE command_id=?',
            ('lp-rejection',)).fetchone()[0])
    assert event['recovery']['diagnostic_metadata'] == metadata
    assert 'request_scope' not in context and not env.paths.proposals.exists()
    serialized = json.dumps(event) + caplog.text
    assert canary not in serialized and str(env.paths.root) not in serialized
    assert all(record.exc_info is None and record.stack_info is None for record in caplog.records)
    assert scope._safe_diagnostic_metadata({**metadata, 'host_check': canary,
        'host_check_line': True, 'prompt': canary, 'path': '/private/'}) == {
            'reason': 'host_check_rejected'}
