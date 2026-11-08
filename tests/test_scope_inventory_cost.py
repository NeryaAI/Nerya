"""Count real discovery work, never replace it with a cheap inventory fake."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import json

import pytest

from nerya.db.sqlite import connect
from nerya.strategies import request_scope as scope
from nerya.trading import components
from test_request_scope import FakeGateway, assistant_chain, spans_output
from test_request_scope import env  # noqa: F401
from test_scope_extraction_budget import BudgetTransport, chain, offline  # noqa: F401

pytestmark = pytest.mark.smoke
INITIAL = 'Uniswap v3 ETH/USDC strategy, last 30 days; no trading.'


def extracted(docs):
    return spans_output(docs[0], protocol='Uniswap v3', base='ETH',
                        quote='USDC', window_days='30 days')


def test_canonical_reuses_real_inventory_for_coverage_venue_and_protocol(env):
    snapshot = scope._snapshot(env)
    snapshot['binding']['documents'][0]['text'] = INITIAL
    output = extracted(snapshot['binding']['documents'])
    # The same native name has two roles; both still use native discovery.
    output['fields']['venue'] = deepcopy(output['fields']['protocol'])
    output['fields']['venue']['text'] = 'Uniswap'
    output['fields']['venue']['end'] = len('Uniswap')
    with patch.object(components, '_builtin_components', wraps=components._builtin_components) as built:
        contract = scope._canonical(output, snapshot)
    assert contract['constraints']['protocol']['component_id'] == 'builtin:uniswap'
    assert contract['constraints']['venue'] == 'uniswap'
    assert built.call_count == 1


def test_ensure_and_saved_chain_each_build_once_without_skipping_revalidation(env):
    assistant_chain(env, 'Continue the original task.', initial_text=INITIAL, initial_choose=extracted)
    with patch.object(components, '_builtin_components', wraps=components._builtin_components) as built:
        contract = scope.ensure_contract(env, gateway=FakeGateway(extracted))
        assert built.call_count == 1
    for verify in (lambda: scope.current_contract(env), lambda: scope.ensure_contract(env, gateway=FakeGateway(extracted))):
        with patch.object(components, '_builtin_components', wraps=components._builtin_components) as built, \
                patch.object(scope, '_canonical', wraps=scope._canonical) as canonical:
            assert verify() == contract
        assert built.call_count == 1
        assert canonical.call_count >= 2  # Every durable extraction is revalidated.
    with closing(connect(env.paths.db)) as con, con:
        row = con.execute("SELECT context_json FROM agent_commands WHERE command_id='child-create'").fetchone()
        context = json.loads(row[0])
        context['request_scope']['extractions'][0]['fields']['base']['text'] = 'BTC'
        con.execute("UPDATE agent_commands SET context_json=? WHERE command_id='child-create'", (json.dumps(context),))
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=FakeGateway(extracted))


def test_real_gateway_17_command_chain_has_one_inventory_per_verification(offline):
    config, install = offline
    original = chain(config)
    transport = BudgetTransport()
    install(transport)
    with patch.object(components, '_builtin_components', wraps=components._builtin_components) as built:
        contract = scope.ensure_contract(config)
    assert built.call_count == 1
    assert len(transport.calls) == 16
    assert all(call['max_tokens'] == 16384 for call in transport.calls)
    assert len(contract['extractions']) == 17
    assert contract['revision'] == 2 and contract['constraints']['base'] == 'WETH'
    expected = deepcopy(original['constraints'])
    expected['base'] = expected['resources'][0]['base'] = 'WETH'
    assert contract['constraints'] == expected
    for verify in (lambda: scope.current_contract(config), lambda: scope.ensure_contract(config)):
        with patch.object(components, '_builtin_components', wraps=components._builtin_components) as built, \
                patch.object(scope, '_canonical', wraps=scope._canonical) as canonical:
            assert verify() == contract
        assert built.call_count == 1
        assert canonical.call_count >= 17
    assert len(transport.calls) == 16


@pytest.mark.parametrize('changed', ['source', 'dependency'])
def test_separate_verifications_refresh_real_source_and_dependency_revisions(env, monkeypatch, changed):
    assistant_chain(env, 'Continue the original task.', initial_text=INITIAL, initial_choose=extracted)
    contract = scope.ensure_contract(env, gateway=FakeGateway(extracted))
    revisions = []
    builtin = components._builtin_components
    def capture():
        result = builtin()
        revisions.append(result['builtin:uniswap'].revision)
        return result
    monkeypatch.setattr(components, '_builtin_components', capture)
    assert scope.current_contract(env) == contract
    if changed == 'source':
        read_bytes = Path.read_bytes
        source = Path(components.__file__)
        monkeypatch.setattr(Path, 'read_bytes', lambda path: read_bytes(path) +
                            (b'\n# changed source\n' if path == source else b''))
    else:
        version = components.importlib.metadata.version
        monkeypatch.setattr(components.importlib.metadata, 'version', lambda name:
                            'changed-version' if name == 'eth-abi' else version(name))
    # A source revision alone does not change requested identity; discovery must
    # nevertheless see it, including when all contracts are already durable.
    assert scope.ensure_contract(env, gateway=FakeGateway(extracted)) == contract
    assert len(revisions) == 2 and revisions[0] != revisions[1]
    assert scope.current_contract(env) == contract
    assert len(revisions) == 3 and revisions[1] == revisions[2]


def test_saved_contract_rechecks_changed_component_semantics_and_recovers(env, monkeypatch):
    assistant_chain(env, 'Continue the original task.', initial_text=INITIAL, initial_choose=extracted)
    contract = scope.ensure_contract(env, gateway=FakeGateway(extracted))
    builtin = components._builtin_components
    def changed():
        result = builtin()
        component = result['builtin:uniswap']
        result[component.id] = replace(component, spec=replace(component.spec, market_types=('lending',)))
        return result
    with monkeypatch.context() as update:
        update.setattr(components, '_builtin_components', changed)
        with pytest.raises(scope.ScopeError):
            scope.current_contract(env)
        fake = FakeGateway(extracted)
        with pytest.raises(scope.ScopeError):
            scope.ensure_contract(env, gateway=fake)
        assert not fake.calls
    # Failure must not retain its inventory for the next operation.
    with patch.object(components, '_builtin_components', wraps=builtin) as built:
        assert scope.current_contract(env) == contract
    assert built.call_count == 1
