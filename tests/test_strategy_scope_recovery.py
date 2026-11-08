"""Cross-chat validation must retain the producer's original request scope."""
from contextlib import closing
from copy import deepcopy
import json

import pytest

from nerya.db.sqlite import connect, connect_readonly
from nerya.strategies import request_scope as scope
from nerya.tools.native import strategy_runtime
from nerya.tools.types import ToolCall
from test_request_scope import FakeGateway, admit, install_fake, manifest
from test_request_scope import env  # noqa: F401

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize('damage', [None, 'actor', 'receipt', 'scope', 'conflict', 'tool_only', 'candidate'])
def test_existing_candidate_validation_recovers_original_scope(env, monkeypatch, damage):
    # No candidate creation or real runtime: use a durable producer receipt.
    admit(env, cid='origin', sid='original-chat', source='user_chat',
          extra={'conversation_kind': 'task'})
    original = scope.ensure_contract(env, gateway=FakeGateway())
    artifact = 'strategy:scope_fixture:proposal:prp_fixture'
    ref = 'evolution/proposals/prp_fixture/after/strategies/scope_fixture'
    delivery = {'artifacts': [{'artifact_id': 'candidate', 'ref': artifact, 'revision': 'source-v1'}],
        'checks': [
            {'check_id': 'source_binding', 'artifact_id': artifact, 'revision': 'source-v1',
             'producer_owned': True, 'status': 'passed', 'evidence_ref': ref},
            {'check_id': 'candidate_scope_match', 'artifact_id': artifact, 'revision': 'source-v1',
             'producer_owned': True, 'status': 'passed',
             'evidence_ref': 'request_scope:origin:' + original['request_scope_hash']}]}
    with closing(connect(env.paths.db)) as con, con:
        con.execute("UPDATE agent_commands SET state='succeeded',delivery_json=? WHERE command_id='origin'",
                    (json.dumps(delivery),))
        if damage == 'actor':
            con.execute("UPDATE agent_commands SET actor_id='foreign' WHERE command_id='origin'")
        elif damage == 'receipt':
            delivery['checks'][1]['producer_owned'] = False
            con.execute("UPDATE agent_commands SET delivery_json=? WHERE command_id='origin'",
                        (json.dumps(delivery),))
        elif damage == 'scope':
            changed = deepcopy(original)
            changed['constraints']['quote'] = 'USDC'
            con.execute("UPDATE agent_commands SET context_json=? WHERE command_id='origin'",
                        (json.dumps({'request_scope': changed}),))
    text = '继续同一候选 prp_fixture：现在只执行 strategy_validate，不创建新候选。'
    if damage == 'tool_only':
        text = '继续当前任务。'
    elif damage == 'conflict':
        text += ' change quote to USDC'
    admit(env, cid='recovery', sid='recovery-chat', created=1792903600, source='user_chat',
          text=text, extra={'conversation_kind': 'task'})
    # Missing original evidence produces exactly the observed ambiguous_scope.
    ambiguous = deepcopy(original['normalized_evidence'])
    ambiguous['status'] = 'ambiguous'
    gateway = FakeGateway(lambda docs: deepcopy(original['normalized_evidence'])
                          if len(docs) == 2 else ambiguous)
    install_fake(monkeypatch, gateway)
    call = ToolCall(name='strategy_validate', arguments={'proposal_id': 'prp_fixture'})
    monkeypatch.setattr(scope, 'target_manifest', lambda *args: manifest(
        'BINANCE:ETHUSDC' if damage == 'candidate' else 'BINANCE:ETHUSDT'))
    validations = []
    def validate(*args, **kwargs):
        validations.append(kwargs)
        return {'ok': True}
    monkeypatch.setattr('nerya.strategies.validation_receipt.validate_proposal_receipt', validate)
    result = strategy_runtime._strategy_validate_handler(call, config=env)
    if damage:
        assert result.is_error
        assert not validations
        assert result.error.recovery_hint['reason'] == (
            'request_scope_mismatch' if damage == 'candidate' else 'request_scope_unverified')
        with closing(connect_readonly(env.paths.db)) as con:
            saved = json.loads(con.execute(
                "SELECT context_json FROM agent_commands WHERE command_id='recovery'").fetchone()[0])
        if damage != 'candidate':
            assert 'request_scope' not in saved
    else:
        assert not result.is_error, result.error
        assert validations == [{'strategy_id': ''}]
        recovered = scope.current_contract(env)
        assert recovered['binding']['original_command_id'] == 'origin'
        assert recovered['constraints'] == original['constraints']
        assert recovered['extractions'][:-1] == original['extractions']
        assert recovered['normalized_evidence'] == original['normalized_evidence']
        assert scope.current_contract(env, command_id='origin') == original
        assert len(gateway.calls) == 1
        assert json.loads(gateway.calls[0]['messages'][0]['content']) == recovered['binding']['documents']
