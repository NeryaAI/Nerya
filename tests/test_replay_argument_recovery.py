"""Actionable schema corrections and failure diagnostics from the taskchat incident."""
import json

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.llm.tool_compaction import compact_tool_result
from nerya.tools.native import strategy_runtime as tools
from nerya.tools.types import ToolCall, ToolErrorKind

pytestmark = pytest.mark.smoke


def test_corrected_replay_source_reaches_producer_once_without_relaxing_dedup(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from nerya.agent.loop_state import LoopRunState
    from nerya.agent.tool_phase import ToolBatchPhase, ToolBatchPolicy
    from nerya.strategies import request_scope as scope
    from nerya.strategies.tool_verification import source_binding
    from nerya.tools.orchestrator import BatchResult
    from nerya.tools.registry import ToolRegistry
    from nerya.tools.types import PermissionScope, RiskLevel, ToolDescriptor
    import nerya.skills.builtin.backtest.scripts.rate_replay as runner

    assert 'expected_source_revision' in tools.STRATEGY_BACKTEST_SCHEMA['properties']
    config = Config(paths=WorkspacePaths(root=tmp_path), data={'runtime': {'command_id': 'offline'}})
    root = config.paths.strategy('rate_fixture')
    root.mkdir(parents=True)
    (root / 'strategy.yml').write_text('strategy_id: rate_fixture\nparams:\n  rate_replay: {}\n')
    inputs = root / 'replay_inputs.json'
    declaration = {'version': 1, 'datasets': [{'id': 'rates', 'provider': 'defillama',
        'action': 'yield_history', 'args': {'pool_id': '747c1d2a-c668-4682-b9f9-296708a3dd90',
        'start': '2026-09-30T16:00:00Z', 'end': '2026-10-30T00:00:00Z'}}]}
    inputs.write_text(json.dumps(declaration))
    pinned = {'start_utc': '2026-09-30T00:00:00Z', 'end_utc': '2026-10-30T00:00:00Z'}
    monkeypatch.setattr(scope, 'guard', lambda *a, **k: (None, pinned))
    monkeypatch.setattr(scope, 'current_contract', lambda *a, **k: {'offline': True})
    monkeypatch.setattr(scope, 'compare', lambda *a, **k: pinned)
    monkeypatch.setattr('nerya.strategies.tool_verification.attach_strategy_checks', lambda result, **k: result)
    runs = []
    monkeypatch.setattr(runner, 'run_rate_backtest', lambda **k: runs.append(k) or {
        'ok': True, 'strategy_id': 'rate_fixture', 'kind': 'rate_policy_replay'})
    registry = ToolRegistry()
    registry.register(ToolDescriptor(name='strategy_backtest', description='offline replay',
        input_schema=tools.STRATEGY_BACKTEST_SCHEMA, handler=lambda c: tools.strategy_backtest_handler(c, config=config),
        risk=RiskLevel.WRITE, permission_scope=PermissionScope.WORKSPACE, read_only=False))
    phase = ToolBatchPhase(registry=registry, orchestrator=SimpleNamespace(run_batch=lambda calls:
        BatchResult(results=[tools.strategy_backtest_handler(c, config=config) for c in calls])))
    policy = ToolBatchPolicy(frozenset({'strategy_backtest'}), frozenset({'strategy_backtest'}))
    state = LoopRunState(turn_id='offline', message_id='offline', required_next_tool_names=set(),
        attempted_tool_names=set(), successful_tool_names=set(), completed_tool_results=[],
        tool_result_by_fingerprint={}, recent_tool_fingerprints=[], deduped_counts_by_fingerprint={})
    args = {'strategy_id': 'rate_fixture', 'engine': 'rates', 'allow_mock': False}
    def revision():
        return source_binding(config, ToolCall(name='strategy_backtest', arguments=args))['revision']
    def run(identifier, arguments):
        return phase.run([ToolCall(id=identifier, name='strategy_backtest', arguments=arguments)],
            state=state, policy=policy).batch.results[0]

    old = {**args, 'expected_source_revision': revision()}
    failed = run('original', old)
    assert failed.is_error and failed.error.detail['reason'] == 'request_scope_mismatch'
    assert failed.error.recovery_hint['corrected_retry']['revision_from'] == 'strategy_validate.verification.revision'
    assert not runs
    declaration['datasets'][0]['args']['start'] = pinned['start_utc']
    inputs.write_text(json.dumps(declaration))
    corrected = {**args, 'expected_source_revision': revision()}
    assert corrected != old
    assert run('unchanged', old).error.kind == ToolErrorKind.DEDUPED
    stale = tools.strategy_backtest_handler(ToolCall(name='strategy_backtest', arguments=old), config=config)
    assert stale.is_error and stale.error.recovery_hint['reason'] == 'source_revision_conflict'
    assert not run('corrected', corrected).is_error and len(runs) == 1
    assert run('duplicate-corrected', corrected).error.kind == ToolErrorKind.DEDUPED
    # A fresh source hash is not permission to change the admitted window.
    declaration['datasets'][0]['args']['start'] = '2026-10-01T00:00:00Z'
    inputs.write_text(json.dumps(declaration))
    mismatch = run('changed-but-wrong', {**args, 'expected_source_revision': revision()})
    assert mismatch.is_error and mismatch.error.detail['reason'] == 'request_scope_mismatch'
    assert len(runs) == 1 and not config.paths.db.exists()


@pytest.mark.parametrize("engine", ["auto", "freeform"])
@pytest.mark.parametrize("extra", [
    {"data_mode": "download"}, {"settings": {"window_days": 30}},
    {"preflight_only": True}, {"config_path": "custom.yml"},
    {"data_mode": "download", "settings": {"window_days": 30}},
])
def test_custom_replay_rejects_native_arguments_before_any_execution(tmp_path, monkeypatch, engine, extra):
    monkeypatch.setattr(tools, "_target_has_freeform_backtest_script", lambda *a, **k: True)
    monkeypatch.setattr(tools, "_target_backtest_defaults", lambda *a, **k: {"window_days": 30})
    import nerya.skills.builtin.backtest.scripts.freeform_run as runner
    monkeypatch.setattr(runner, "run_freeform_backtest", lambda **k: pytest.fail("invalid request executed"))
    args = {"proposal_id": "prp_saved", "engine": engine, **extra}
    result = tools._strategy_backtest_handler(ToolCall(name="strategy_backtest", arguments=args),
        config=Config(paths=WorkspacePaths(root=tmp_path)))
    assert result.is_error and result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
    hint = result.error.recovery_hint
    assert hint["suggested_arguments"] == {"proposal_id": "prp_saved", "engine": "freeform", "allow_mock": False}
    assert set(hint["remove_fields"]) == set(extra)
    assert hint["side_effects"] == "none" and hint["preserve_requested_constraints"]
    assert "replay_inputs.json" in result.error.message
    assert "Do not switch" in result.error.message
    assert args == {"proposal_id": "prp_saved", "engine": engine, **extra}


def test_validation_compaction_preserves_exception_tail_and_result_state():
    failure = "tests/test_contract.py:38: fixture setup\n" + "context line\n" * 400 + "TypeError: StateStore requires namespace"
    result = compact_tool_result("strategy_validate", {
        "ok": False, "delivery_revision": 6, "source_revision": "saved-sha",
        "blockers": [{"code": "candidate_test_failed", "where": "tests/test_contract.py::test_exit", "message": failure}],
        "behavioral": {"status": "failed", "summary": {"passed": 1, "failed": 1},
            "failures": [{"nodeid": "tests/test_contract.py::test_exit", "message": failure}],
            "stdout": "too much output" * 10000},
    })
    assert result.rule_id == "strategy.validation"
    assert result.kept["ok"] is False and result.kept["delivery_revision"] == 6
    for issue in (result.kept["blockers"][0], result.kept["behavioral"]["failures"][0]):
        assert "tests/test_contract.py:38" in issue["message"]
        assert "TypeError: StateStore requires namespace" in issue["message"]
        assert len(issue["message"]) <= 1800
    assert "stdout" not in result.kept["behavioral"]
    assert len(json.dumps(result.kept)) < 6000
