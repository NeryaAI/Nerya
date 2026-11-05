"""Native delivery must enforce the installed workflow's evidence floor."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from nerya.evolution.patch_proposal import create_proposal
from nerya.skills.registry import SkillRegistry
from nerya.strategies.tool_verification import source_binding
from nerya.task_delivery import read_delivery, record_delivery_verification, start_delivery
from nerya.tools.native import delivery as native_delivery
from nerya.tools.native.skill import SkillIndex
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall

from test_task_delivery import _command, _deps, _receipt, _runtime


pytestmark = pytest.mark.smoke
MISSING_CHECKS = {
    "request_scope_match", "strategy_contract", "behavioral_tests", "historical_run",
    "data_coverage", "historical_data", "replay_source", "execution_integrity",
}


@pytest.fixture
def delivery_env(tmp_path):
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    deps = _deps(config, command)
    registry = SkillRegistry.load_builtin()
    deps.skill_index = SkillIndex([], registry_provider=lambda: registry)
    assert deps.skill_index.get("strategy_author").source == "builtin"
    return config, runtime, command, deps


def _start_args(skill_id="strategy_author", workflow_id="strategy_authoring"):
    return {
        "skill_id": skill_id, "workflow_id": workflow_id,
        "requested_scope": {"summary": "ETH/USDT EMA, 1h, historical 30 days; no live trading"},
        "stages": [{"id": "deliver", "state": "completed",
                    "required_checks": ["source_binding"],
                    "required_artifacts": ["candidate"]}],
    }


def _call(deps, name, arguments, **metadata):
    return getattr(native_delivery, name + "_handler")(
        ToolCall(name=name, arguments=arguments, metadata=metadata), deps=deps)


def _data(result):
    assert not result.is_error, result.text()
    return tool_json_data(result)


def test_unknown_strategy_author_workflow_is_repairable_and_points_to_canonical(delivery_env):
    _, runtime, command, deps = delivery_env
    result = _call(deps, "delivery_start", {
        "skill_id": "strategy_author",
        "workflow_id": "binance_btcusdt_trend_paper_backtest",
        "requested_scope": {"summary": "script Agent trend; paper backtest; no trading"},
        "stages": [{"id": "scope", "state": "in_progress"}],
    })
    assert result.is_error
    assert result.error.kind.value == "schema_validation"
    assert result.error.retryable is True
    assert result.error.recovery_hint["canonical_workflow_id"] == "strategy_authoring"
    assert result.error.recovery_hint["available_workflows"] == ["strategy_authoring"]
    assert runtime.store.snapshot(command["session_id"])["commands"][0].get("delivery") is None


def test_strategy_author_canonical_retry_starts_the_same_requested_delivery(delivery_env):
    _, runtime, command, deps = delivery_env
    args = {
        "skill_id": "strategy_author",
        "workflow_id": "strategy_authoring",
        "requested_scope": {"summary": "script Agent trend; paper backtest; no trading"},
        "stages": [{"id": "scope", "state": "in_progress"}],
    }
    delivery = _data(_call(deps, "delivery_start", args))
    assert delivery["skill_id"] == "strategy_author"
    assert delivery["workflow_id"] == "strategy_authoring"
    assert delivery["workflow_contract"]["workflow_id"] == "strategy_authoring"
    assert runtime.store.snapshot(command["session_id"])["commands"][0]["delivery"] == delivery


def _source_only_candidate(env):
    config, _, command, _ = env
    sid = "eth_ema_trend_1h"
    proposal = create_proposal(
        config.paths, kind="strategy_package_proposal", summary="candidate",
        rationale="test", test_plan="test", rollback="test", target=f"strategies/{sid}",
        extra_files={
            f"after/strategies/{sid}/main.py": "def run(ctx): return ctx.result.hold()\n",
            f"after/strategies/{sid}/strategy.yml": f"id: {sid}\nmode: paper\n",
        },
    )
    binding = source_binding(config, ToolCall(name="strategy_validate", arguments={
        "strategy_id": sid, "proposal_id": proposal.id,
    }))
    assert binding["revision"] != "unbound"
    delivery = record_delivery_verification(
        config.paths, command_id=command["command_id"],
        receipt=_receipt("source_binding", **binding),
    )
    assert delivery is not None
    source = next(row for row in delivery["checks"] if row["check_id"] == "source_binding")
    assert source["producer_owned"] is True and source["status"] == "passed"
    return delivery, {"artifact_id": "candidate", "ref": binding["artifact_id"],
                      "revision": binding["revision"]}


def _assert_floor(delivery):
    completion = delivery["completion"]
    assert completion["allowed"] is False, completion
    # Check missing identities without coupling to error wording or order.
    missing = {reason.rsplit(":", 1)[-1] for reason in completion["reasons"]}
    assert MISSING_CHECKS | {"report", "run_receipt"} <= missing, completion
    assert "source_binding" not in missing and "candidate" not in missing, completion


def test_minimal_eth_30_day_delivery_cannot_omit_workflow_evidence(delivery_env):
    _, _, _, deps = delivery_env
    _data(_call(deps, "delivery_start", _start_args()))
    delivery, candidate = _source_only_candidate(delivery_env)
    updated = _data(_call(deps, "delivery_update", {
        "expected_revision": delivery["delivery_revision"], "artifacts": [candidate],
    }))
    result = _call(deps, "delivery_finalize", {"expected_revision": updated["delivery_revision"]})
    final = _data(result)
    _assert_floor(final)
    assert result.semantic_success is False


def _read(env):
    config, _, command, _ = env
    return read_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'])


@pytest.mark.parametrize('mode', ['missing_index', 'shadow', 'alias', 'outside_path', 'wrong_workflow', 'unknown', 'denied', 'malformed_grant', 'model_contract'])
def test_untrusted_start_cannot_create_delivery(delivery_env, mode):
    _, runtime, command, deps = delivery_env
    args, metadata = _start_args(), {}
    if mode == 'missing_index':
        del deps.skill_index
    elif mode == 'shadow':
        deps.skill_index.get('strategy_author').source = 'workspace'
    elif mode == 'alias':
        args['skill_id'] = 'STRATEGY_AUTHOR'
        assert deps.skill_index.get(args['skill_id']) is not None
    elif mode == 'outside_path':
        deps.skill_index.get('strategy_author').path = '/tmp/strategy_author/SKILL.md'
    elif mode == 'wrong_workflow':
        args['workflow_id'] = 'strategy_replay'
    elif mode == 'unknown':
        args.update(skill_id='unknown', workflow_id='unknown')
    elif mode == 'denied':
        metadata['allowed_skills'] = ['research']
    elif mode == 'malformed_grant':
        metadata['allowed_skills'] = 'strategy_author'
    else:
        args['workflow_contract'] = {'required_checks': []}
    result = _call(deps, 'delivery_start', args, **metadata)
    assert result.is_error
    assert runtime.store.snapshot(command['session_id'])['commands'][0]['delivery'] is None


@pytest.mark.parametrize('tool', ['delivery_update', 'delivery_finalize'])
def test_missing_index_rejects_mutation_and_persists_invalidation(delivery_env, tool):
    _, _, _, deps = delivery_env
    before = _data(_call(deps, 'delivery_start', _start_args()))
    del deps.skill_index
    args = {'expected_revision': before['delivery_revision']}
    if tool == 'delivery_update':
        args['artifacts'] = ['model-added']
    result = _call(deps, tool, args)
    row = _data(result)
    assert result.semantic_success is False
    assert not row['completion']['allowed']
    assert row['artifacts'] == before['artifacts']
    assert row['workflow_contract'] == before['workflow_contract']
    assert row['workflow_contract_error'] == 'trusted_delivery_contract_missing'
    persisted = _read(delivery_env)
    assert persisted == {key: row[key] for key in persisted}


@pytest.mark.parametrize('restriction', ['Draft only; do not run tests', 'No historical replay or tests'])
def test_user_prohibition_keeps_partial_without_executing(delivery_env, monkeypatch, restriction):
    from nerya.tools.native import strategy_runtime
    def forbidden(*args, **kwargs):
        pytest.fail('Delivery must not execute prohibited work')
    monkeypatch.setattr(strategy_runtime, 'strategy_backtest_handler', forbidden)
    _, _, _, deps = delivery_env
    args = _start_args()
    args['requested_scope']['limitations'] = [restriction]
    _data(_call(deps, 'delivery_start', args))
    row, candidate = _source_only_candidate(delivery_env)
    row = _data(_call(deps, 'delivery_update', {'expected_revision': row['delivery_revision'],
        'artifacts': [candidate], 'stages': [{'id': 'deliver', 'required_checks': [], 'required_artifacts': []}]}))
    row = _data(_call(deps, 'delivery_finalize', {'expected_revision': row['delivery_revision']}))
    _assert_floor(row)
    assert row['completion']['status'] == 'partial'


def test_binding_reopens_and_rejects_workflow_switch_and_stale_cas(delivery_env):
    config, _, command, deps = delivery_env
    first = _data(_call(deps, 'delivery_start', _start_args()))
    # Recreate dependency/index instances against the same durable command.
    reopened = _deps(config, command)
    assert _data(_call(reopened, 'delivery_start', _start_args())) == first
    wrong = _call(reopened, 'delivery_start', _start_args('backtest', 'strategy_replay'))
    assert wrong.is_error and 'binding_conflict' in wrong.error.message
    assert _read(delivery_env) == first
    row = _data(_call(reopened, 'delivery_update', {'expected_revision': first['delivery_revision'], 'limitations': ['pending']}))
    rejected = _call(reopened, 'delivery_finalize', {'expected_revision': first['delivery_revision']})
    assert rejected.is_error and 'revision_conflict' in rejected.error.message
    assert _read(delivery_env) == row


@pytest.mark.parametrize('damage', ['changed', 'invalid', 'missing', 'source'])
def test_contract_invalidation_is_durable_and_cannot_rebind(delivery_env, monkeypatch, damage):
    config, _, command, deps = delivery_env
    first = _data(_call(deps, 'delivery_start', _start_args()))
    target = Path(deps.skill_index.get('strategy_author').path).parent / 'references/delivery-contract.json'
    original = target.read_bytes()
    open_file = Path.open
    with monkeypatch.context() as patch:
        if damage == 'source':
            patch.setattr(deps.skill_index.get('strategy_author'), 'source', 'workspace')
        else:
            def changed_open(path, *args, **kwargs):
                if path == target:
                    if damage == 'missing':
                        raise FileNotFoundError()
                    return io.BytesIO(original + b' ' if damage == 'changed' else b'{}')
                return open_file(path, *args, **kwargs)
            patch.setattr(Path, 'open', changed_open)
        result = _call(deps, 'delivery_update', {'expected_revision': first['delivery_revision'],
            'artifacts': ['must-not-save']})
        invalid = _data(result)
        assert result.semantic_success is False and not invalid['completion']['allowed']
        assert invalid['workflow_contract'] == first['workflow_contract']
        assert invalid['artifacts'] == []
        assert invalid['workflow_contract_error'] == ('delivery_contract_stale' if damage == 'changed'
            else 'trusted_delivery_contract_missing' if damage == 'source' else 'trusted_delivery_contract_invalid')
    reopened = _deps(config, command)
    final = _data(_call(reopened, 'delivery_finalize', {'expected_revision': invalid['delivery_revision']}))
    assert final['workflow_contract_error'] == invalid['workflow_contract_error']
    assert not final['completion']['allowed']
    assert _read(delivery_env)['workflow_contract'] == first['workflow_contract']
    again = _data(_call(reopened, 'delivery_start', _start_args()))
    assert again['workflow_contract_error'] == invalid['workflow_contract_error']


@pytest.mark.parametrize('known', [True, False])
def test_legacy_read_is_lossless_and_native_reconciles_safely(delivery_env, known):
    config, runtime, command, deps = delivery_env
    args = _start_args() if known else _start_args('legacy', 'legacy')
    legacy = start_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'], **args)
    legacy['completion'] = {'allowed': True, 'status': 'complete', 'reasons': [], 'computed_at': 1}
    with runtime.store.transaction() as con:
        con.execute('UPDATE agent_commands SET delivery_json=? WHERE command_id=?',
            (json.dumps(legacy), command['command_id']))
    assert _read(delivery_env) == legacy
    row = _data(_call(deps, 'delivery_finalize', {'expected_revision': legacy['delivery_revision']}))
    assert not row['completion']['allowed']
    assert bool(row.get('workflow_contract')) is known
    assert bool(row.get('workflow_contract_error')) is not known
    assert row['delivery_revision'] == legacy['delivery_revision'] + 1


def test_research_missing_producers_are_not_invented(delivery_env):
    _, _, _, deps = delivery_env
    args = _start_args('research', 'research_brief')
    args['stages'][0]['required_checks'] = ['source_coverage']
    row = _data(_call(deps, 'delivery_start', args))
    row = _data(_call(deps, 'delivery_finalize', {'expected_revision': row['delivery_revision']}))
    assert not row['completion']['allowed'] and row['checks'] == []
    assert {'check_missing:source_coverage', 'check_missing:freshness', 'check_missing:report_readback'} <= set(row['completion']['reasons'])


@pytest.mark.parametrize('tool', ['delivery_update', 'delivery_finalize'])
def test_permissions_apply_to_existing_binding_without_mutation(delivery_env, tool):
    _, _, _, deps = delivery_env
    first = _data(_call(deps, 'delivery_start', _start_args()))
    result = _call(deps, tool, {'expected_revision': first['delivery_revision']}, allowed_skills=['research'])
    assert result.is_error and 'access_denied' in result.error.message
    assert _read(delivery_env) == first


def test_returned_floor_is_a_copy_and_model_cannot_replace_it(delivery_env):
    _, _, _, deps = delivery_env
    first = _data(_call(deps, 'delivery_start', _start_args()))
    first['workflow_contract']['required_checks'].clear()
    assert _read(delivery_env)['workflow_contract']['required_checks']
    result = _call(deps, 'delivery_update', {'expected_revision': first['delivery_revision'],
        'workflow_contract': first['workflow_contract']})
    assert result.is_error
    assert _read(delivery_env)['workflow_contract']['required_checks']


@pytest.mark.parametrize('entry', ['start', 'update_stage', 'update_check', 'update_default_required'])
def test_native_rejects_unknown_required_check_atomically(delivery_env, entry):
    _, runtime, command, deps = delivery_env
    args = _start_args()
    if entry == 'start':
        args['stages'][0]['required_checks'].append('reported')
        name = 'delivery_start'
    else:
        first = _data(_call(deps, 'delivery_start', args))
        args = {'expected_revision': first['delivery_revision'], 'limitations': ['must-not-save']}
        if entry == 'update_stage':
            args['stages'] = [{'id': 'deliver', 'required_checks': ['reported']}]
        else:
            args['checks'] = [{'check_id': 'reported', 'status': 'pending'}]
            if entry == 'update_check':
                args['checks'][0]['required'] = True
        name = 'delivery_update'
    before = runtime.store.snapshot(command['session_id'])['commands'][0].get('delivery')
    result = _call(deps, name, args)
    assert result.is_error and result.error.kind.value == 'schema_validation'
    if entry in {'start', 'update_stage'}:
        contract = deps.skill_index.delivery_contract(ToolCall(name=name), 'strategy_author', 'strategy_authoring')
        assert result.error.message == ('delivery_unknown_required_checks; availableIDs: '
                                        + ','.join(sorted(contract['required_checks'])))
    else:
        assert result.error.detail['code'].startswith('invalid_delivery_input:arguments:additionalProperties')
    assert runtime.store.snapshot(command['session_id'])['commands'][0].get('delivery') == before


def test_native_accepts_only_this_workflows_floor_as_required(delivery_env):
    _, _, _, deps = delivery_env
    args = _start_args()
    contract = deps.skill_index.delivery_contract(ToolCall(name='delivery_start'), 'strategy_author', 'strategy_authoring')
    args['stages'][0]['required_checks'] = contract['required_checks']
    first = _data(_call(deps, 'delivery_start', args))
    rejected = _call(deps, 'delivery_update', {'expected_revision': first['delivery_revision'],
        'checks': [{'check_id': check, 'status': 'pending'} for check in contract['required_checks']]})
    assert rejected.is_error and _read(delivery_env) == first


@pytest.mark.parametrize('check_id', ['historical_performance', 'custom_user_note'])
def test_optional_checks_retained_but_cannot_be_promoted(delivery_env, check_id):
    _, _, _, deps = delivery_env
    args = _start_args()
    args['requested_scope'] = {'summary': 'Draft only; explain decisions; do not run tests',
                               'limitations': ['No tests authorized']}
    args['stages'].append({'id': 'custom_explanation'})
    first = _data(_call(deps, 'delivery_start', args))
    rejected = _call(deps, 'delivery_update', {'expected_revision': first['delivery_revision'],
        'checks': [{'check_id': check_id, 'status': 'pending', 'required': False}]})
    assert rejected.is_error and _read(delivery_env) == first


def test_legacy_reported_remains_required_and_blocked(delivery_env):
    from nerya.task_delivery import update_delivery

    config, _, command, deps = delivery_env
    args = _start_args()
    first = _data(_call(deps, 'delivery_start', args))
    # Recreate the persisted pre-admission state without weakening native tools.
    legacy = update_delivery(config.paths, command_id=command['command_id'], session_id=command['session_id'],
        expected_revision=first['delivery_revision'], checks=[{'check_id': 'reported', 'status': 'pending'}])
    args['stages'][0]['required_checks'].append('reported')
    reopened = _data(_call(deps, 'delivery_start', args))
    assert reopened == legacy
    row = _data(_call(deps, 'delivery_update', {'expected_revision': legacy['delivery_revision'],
        'limitations': ['legacy producer check remains pending']}))
    final = _data(_call(deps, 'delivery_finalize', {'expected_revision': row['delivery_revision']}))
    assert not final['completion']['allowed']
    assert 'check_pending:reported' in final['completion']['reasons']
    check = next(c for c in final['checks'] if c['check_id'] == 'reported')
    assert check['required'] is True and check['status'] == 'pending'
    assert not check.get('producer_owned')
