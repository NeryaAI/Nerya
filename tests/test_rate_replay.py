"""Host-owned rate replay accounting, synthetic input and isolated candidate."""

import json
from pathlib import Path

import pytest

from nerya.core import yaml_io
from nerya.strategies.package import load_package_from_dir
from nerya.skills.builtin.backtest.scripts.rate_replay import simulate_rates

pytestmark = pytest.mark.smoke


def dataset(rates=(10.0, 20.0, 1.0), times=None):
    times = times or [
        "2026-01-01T00:00:00Z",
        "2026-01-02T00:00:00Z",
        "2026-01-03T00:00:00Z",
    ]
    return {
        "rows": [
            {"pool_id": "fixture-pool", "timestamp": t, "deposit_apy_percent": a}
            for t, a in zip(times, rates)
        ],
        "_envelope": {
            "mock": False,
            "observation_kind": "historical_rate_observations",
            "pool": {
                "pool_id": "fixture-pool",
                "project": "aave-v3",
                "chain": "Ethereum",
                "symbol": "USDC",
            },
            "requested_window": {"start": times[0], "end": times[-1]},
            "sources": [{"sha256": "a" * 64}],
            "coverage": {"completeness": "complete"},
        },
    }


def setup(tmp_path):
    root = tmp_path / "candidate"
    root.mkdir()
    manifest = {
        "id": "rate_test",
        "entrypoint": "main.py:run",
        "mode": "paper",
        "markets": ["AAVE_V3:ethereum-USDC"],
        "accounts": ["paper_main"],
        "schedule": {"type": "none", "enabled": False},
        "params": {
            "rate_source": {
                "pool_id": "fixture-pool",
                "project": "aave-v3",
                "chain": "Ethereum",
                "asset": "USDC",
            },
            "rate_replay": {
                "version": 1,
                "dataset_id": "rates",
                "initial_cash_units": 1000.0,
                "unit_price_usd": 1.0,
                "fee_bps": 10.0,
                "max_gap_seconds": 90000,
            },
        },
    }
    files = {
        "strategy.yml": yaml_io.dumps(manifest),
        "main.py": """from nerya.strategies import StrategyResult
def run(ctx):
    obs=ctx.trigger.get('rate_observation'); pos=ctx.trigger.get('lending_position')
    apy=obs['deposit_apy_percent']
    action='lend_supply' if apy>=10 and pos['supplied_units']==0 else 'lend_withdraw' if apy<2 and pos['supplied_units']>0 else 'hold'
    amount=100. if action=='lend_supply' else pos['supplied_units'] if action=='lend_withdraw' else 0.
    return StrategyResult.hold(metadata={'rate_decision':{'action':action,'amount_units':amount,'observation_timestamp':obs['timestamp'],'apy_percent':apy}})
""",
    }
    for name, content in files.items():
        (root / name).write_text(content)
    return load_package_from_dir(root), files


def test_real_candidate_and_host_accounting_use_previous_rate_and_preserve_limits(
    tmp_path,
):
    package, files = setup(tmp_path)
    result = simulate_rates(package, files, dataset())
    supplied = 100 * (1.1 ** (1 / 365)) * (1.2 ** (1 / 365))
    assert result["equity"][0]["equity"] == pytest.approx(999.9)
    assert result["equity"][-1]["equity"] == pytest.approx(899.9 + supplied * 0.999)
    assert [r["action"] for r in result["decisions"]] == [
        "lend_supply",
        "hold",
        "lend_withdraw",
    ]
    assert len(result["trades"]) == 2
    assert result["isolation"]["network"] == "denied"
    assert result["isolation"]["process_exited"]
    assert not result["performance_evidence"]
    assert result["strategy_execution_verified"]

    from nerya.skills.builtin.backtest.scripts.rate_replay import _inputs
    manifest = yaml_io.loads(files['strategy.yml'])
    policy = manifest['params']['rate_replay'].copy()
    for invalid, missing, unknown, version_type in (
        ({**policy, 'start_utc': '2026-01-01', 'end_utc': '2026-01-03'},
         [], ['end_utc', 'start_utc'], 'int'),
        ({k: v for k, v in policy.items() if k != 'fee_bps'}, ['fee_bps'], [], 'int'),
        ({**policy, 'version': '1'}, [], [], 'str'),
        ({**policy, 'version': True}, [], [], 'bool'),
        ({**policy, 'version': 3}, [], [], 'int'),
        (None, sorted(policy), [], 'NoneType'),
    ):
        manifest['params']['rate_replay'] = invalid
        damaged = {**files, 'strategy.yml': yaml_io.dumps(manifest)}
        with pytest.raises(ValueError, match='Invalid params.rate_replay schema') as failed:
            _inputs(package, damaged, dataset())
        detail = failed.value.receipt
        assert detail['required_fields'] == sorted(policy)
        assert detail['missing_fields'] == missing and detail['unknown_fields'] == unknown
        assert detail['actual_version_type'] == version_type
        assert detail['actual_version'] == (invalid or {}).get('version')
        assert detail['expected_version'] == 1
        assert 'No additional accounting object exists' in str(failed.value)
        assert 'replay_inputs.json dataset args.start/args.end' in str(failed.value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["rows"][1].update(timestamp=d["rows"][0]["timestamp"]),
        lambda d: d["rows"][1].update(deposit_apy_percent=None),
        lambda d: d["rows"][1].update(deposit_apy_percent=float("nan")),
        lambda d: d["rows"][1].update(pool_id="wrong"),
        lambda d: d["_envelope"]["pool"].update(chain="Other"),
        lambda d: d["_envelope"].update(mock=True),
    ],
)
def test_bad_inputs_fail_before_candidate(tmp_path, monkeypatch, mutate):
    from nerya.skills.builtin.backtest.scripts import rate_replay as module

    package, files = setup(tmp_path)
    data = dataset()
    mutate(data)
    monkeypatch.setattr(
        module,
        "IsolatedStrategy",
        lambda *a, **k: pytest.fail("invalid input reached candidate"),
    )
    with pytest.raises(ValueError):
        simulate_rates(package, files, data)


def test_unobserved_gaps_are_not_accrued_or_claimed_complete(tmp_path):
    package, files = setup(tmp_path)
    data = dataset(
        times=["2026-01-01T00:00:00Z", "2026-01-05T00:00:00Z", "2026-01-06T00:00:00Z"]
    )
    result = simulate_rates(package, files, data)
    assert result["gaps"] and result["requested_window_complete"] is False
    assert result["equity"][1]["equity"] == result["equity"][0]["equity"]


def test_candidate_cannot_invoke_financial_or_trade_facade(tmp_path):
    package, files = setup(tmp_path)
    files["main.py"] = (
        'def run(ctx): return ctx.trading.open_position(market="AAVE_V3:ETH-USDC",side="long")'
    )
    with pytest.raises(ValueError, match="rate replay SDK"):
        simulate_rates(package, files, dataset())


def prepared_input(data):
    import hashlib

    blob = json.dumps(data, sort_keys=True).encode()
    return {
        "files": {"rates.json": blob},
        "receipts": [
            {
                "id": "rates",
                "provider": "fixture",
                "action": "rates",
                "sha256": hashlib.sha256(blob).hexdigest(),
                "rows": len(data["rows"]),
                "sources": data["_envelope"]["sources"],
                "snapshot_file": "prepared_inputs/rates.json",
                "requested_window": data["_envelope"]["requested_window"],
                "requested_window_complete": True,
                "data_kind": "historical_observations",
            }
        ],
    }


def test_persisted_host_run_binds_source_inputs_and_preserves_nonlive_evidence(
    tmp_path, monkeypatch
):
    from nerya.skills.builtin.backtest.scripts import replay_inputs
    from nerya.skills.builtin.backtest.scripts.rate_replay import run_rate_backtest

    package, files = setup(tmp_path)
    # Use the actual installed-strategy loader without touching runtime state.
    target = tmp_path / "strategies" / "rate_test"
    target.parent.mkdir()
    package.root.rename(target)
    monkeypatch.setattr(
        replay_inputs, "prepare_inputs", lambda *a, **k: prepared_input(dataset())
    )
    result = run_rate_backtest(workspace=tmp_path, strategy_id="rate_test")
    assert result["ok"] and result["engine"] == "rates"
    assert result["replay"]["strategy_execution_verified"]
    assert result["performance_evidence"] is False
    assert result["provenance"]["data_kind"] == "historical_observations"
    assert result["provenance"]["source_changed_during_run"] is False
    from nerya.task_delivery import _safe_ref
    from nerya.strategies.tool_verification import _output_path
    from nerya.core.config import Config
    from nerya.core.paths import WorkspacePaths

    assert not Path(result["run_receipt"]).is_absolute()
    assert _safe_ref(result["run_receipt"]) == result["run_receipt"]
    receipt_path = _output_path(Config(WorkspacePaths(tmp_path)), result["run_receipt"])
    assert receipt_path == Path(result["out_dir"]) / "run.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "completed"
    metrics = json.loads(Path(result["metrics_path"]).read_text())
    assert metrics["provenance"] == result["provenance"]
    assert (
        "not Aave reserve-index reconstruction"
        in (tmp_path / result["report_path"]).read_text()
    )
    assert (
        "sources"
        in json.loads(
            (Path(result["out_dir"]) / "prepared_inputs/rates.json").read_text()
        )["_envelope"]
    )
    chart = json.loads(Path(result["chart_path"]).read_text())
    assert chart["meta"]["kind"] == "rate_policy_replay"
    assert chart["tables"][-1]["id"] == "decisions"
    assert result["metrics"]["limitations"] == result["limitations"]
    assert result["next_required_action"]["operation_completed"] is True
    assert result["next_required_action"]["repeat_unchanged_operation"] is False
    assert result["limitation_codes"] == [
        "apy_interval_approximation", "simulated_allocation_constant_price",
        "historical_pool_identity_unattested",
    ]
    assert len(result["equity_preview"]) == 3
    assert chart["meta"]["metrics"] == result["metrics"]

    from nerya.strategies.tool_verification import (
        attach_saved_replay,
        attach_strategy_checks,
        source_binding,
    )
    from nerya.tools.types import ToolCall, ToolResult

    config = Config(WorkspacePaths(tmp_path), {})
    binding = source_binding(
        config,
        ToolCall(name="strategy_backtest", arguments={"strategy_id": "rate_test"}),
    )

    def checks():
        verified = attach_strategy_checks(
            ToolResult.from_json(
                tool_use_id="rate", name="strategy_backtest", data=result
            ),
            before=binding,
            after=binding,
            kind="backtest",
            config=config,
        )
        return {
            row["check_id"]: row["status"]
            for row in verified.metadata["verification"]["checks"]
        }

    assert checks()["historical_data"] == "passed"
    assert checks()["historical_performance"] == "missing"
    recovered = attach_saved_replay(ToolResult.from_json(tool_use_id="read-saved",
        name="strategy_validate", data={"ok": True}), config=config, binding=binding)
    saved_checks = {row["check_id"]: row["status"] for row in recovered.metadata["verification"]["checks"]}
    assert saved_checks["historical_data"] == "passed"
    assert saved_checks["historical_performance"] == "missing"
    snapshot = Path(result["out_dir"]) / "prepared_inputs/rates.json"
    snapshot.write_text(snapshot.read_text() + " ")
    assert checks()["historical_data"] == "missing"


def test_unknown_coverage_remains_unknown(tmp_path):
    package, files = setup(tmp_path)
    data = dataset()
    data["_envelope"].pop("coverage")
    assert simulate_rates(package, files, data)["requested_window_complete"] is False


def bracketed_dataset():
    data = dataset(rates=(10., 10., 10.))
    data['rows'] = data['rows'][1:]
    data['_envelope']['requested_window'] = {
        'start': '2026-01-01T12:00:00Z', 'end': '2026-01-03T12:00:00Z'}
    data['_envelope']['coverage'] = {
        'completeness': 'not_asserted', 'missing_utc_days': ['2026-01-01'],
        'gaps_over_24h': [], 'null_counts': {'deposit_base_apy_percent': 2}}
    data['_envelope']['boundary_observations'] = {
        'before': {'pool_id': 'fixture-pool', 'timestamp': '2026-01-01T00:00:00Z',
                   'deposit_apy_percent': 10.},
        'after': {'pool_id': 'fixture-pool', 'timestamp': '2026-01-04T00:00:00Z',
                  'deposit_apy_percent': 999999.}}
    return data


def test_bracketed_model_covers_window_without_future_decisions_or_apy(tmp_path):
    from copy import deepcopy
    package, files = setup(tmp_path)
    data = bracketed_dataset()
    original = deepcopy(data)
    result = simulate_rates(package, files, data)
    assert data == original
    assert result['requested_window_complete'] is True
    assert result['modeled_coverage']['covered_seconds'] == 2 * 86400
    assert result['observation_coverage'] == original['_envelope']['coverage']
    assert len(result['decisions']) == 2
    assert [d['apy_percent'] for d in result['decisions']] == [10., 10.]
    assert result['equity'][-1]['ts'] - result['equity'][0]['ts'] == 2 * 86400
    assert result['equity'][-1]['equity'] == pytest.approx(899.9 + 100 * 1.1 ** (1.5 / 365))
    assert result['performance_evidence'] is False
    data['_envelope']['boundary_observations']['after']['deposit_apy_percent'] = 0.
    changed = simulate_rates(package, files, data)
    assert changed['decisions'] == result['decisions']
    assert changed['equity'] == result['equity']


def test_declared_gap_bound_controls_model_while_raw_cadence_is_preserved(tmp_path, monkeypatch):
    from copy import deepcopy

    package, files = setup(tmp_path)
    data = bracketed_dataset()
    data["rows"] = [
        {"pool_id": "fixture-pool", "timestamp": "2026-01-01T12:00:00Z", "deposit_apy_percent": 10.0},
        {"pool_id": "fixture-pool", "timestamp": "2026-01-02T12:01:40Z", "deposit_apy_percent": 10.0},
        {"pool_id": "fixture-pool", "timestamp": "2026-01-03T12:03:20Z", "deposit_apy_percent": 10.0},
    ]
    data["_envelope"]["requested_window"] = {
        "start": "2026-01-01T12:00:00Z", "end": "2026-01-03T12:03:20Z"
    }
    data["_envelope"]["coverage"] = {
        "completeness": "not_asserted",
        "missing_utc_days": [],
        "gaps_over_24h": [{"start": data["rows"][0]["timestamp"],
                            "end": data["rows"][1]["timestamp"], "seconds": 86500.0}],
        "null_counts": {"deposit_apy_percent": 0},
    }
    data["_envelope"]["boundary_observations"] = {
        "before": {"pool_id": "fixture-pool", "timestamp": "2026-01-01T00:00:00Z",
                    "deposit_apy_percent": 10.0},
        "after": {"pool_id": "fixture-pool", "timestamp": "2026-01-04T00:00:00Z",
                   "deposit_apy_percent": 999999.0},
    }
    original = deepcopy(data)
    result = simulate_rates(package, files, data)
    assert result["requested_window_complete"] is True
    assert result["observation_coverage"] == original["_envelope"]["coverage"]
    assert [row["apy_percent"] for row in result["decisions"]] == [10.0, 10.0, 10.0]
    assert data == original

    too_wide = deepcopy(data)
    too_wide["rows"][1]["timestamp"] = "2026-01-02T13:01:41Z"
    too_wide["_envelope"]["requested_window"] = {
        "start": "2026-01-01T06:00:00Z", "end": "2026-01-03T18:03:20Z"
    }
    result = simulate_rates(package, files, too_wide)
    assert result["requested_window_complete"] is False
    assert result["gaps"] == [{
        "start": "2026-01-01T12:00:00Z",
        "end": "2026-01-02T13:01:41Z",
        "seconds": 90101.0,
    }]

    from nerya.skills.builtin.backtest.scripts import replay_inputs
    from nerya.skills.builtin.backtest.scripts.rate_replay import run_rate_backtest

    target = tmp_path / "strategies" / "rate_test"
    target.parent.mkdir()
    package.root.rename(target)
    monkeypatch.setattr(replay_inputs, "prepare_inputs", lambda *a, **k: prepared_input(too_wide))
    saved = run_rate_backtest(workspace=tmp_path, strategy_id="rate_test")
    report = (tmp_path / saved["report_path"]).read_text()
    metrics = json.loads(Path(saved["metrics_path"]).read_text())
    window = too_wide["_envelope"]["requested_window"]
    assert "\n".join([
        f"Requested window: {window['start']} to {window['end']}",
        f"Observed in-window range: {too_wide['rows'][0]['timestamp']} to {too_wide['rows'][-1]['timestamp']}",
        f"Modeled equity range (may include source-bracketed boundary-supported endpoints): {metrics['start_utc']} to {metrics['end_utc']}",
    ]) in report and "\n".join([
        f"Modeled interval coverage: {json.dumps(result['modeled_coverage'], sort_keys=True)}",
        "Uncovered modeled boundary offsets (seconds): leading=0; trailing=0",
        f"Excluded modeled intervals (source-bracket start/end/seconds; max_gap_seconds={result['assumptions']['max_gap_seconds']}): {json.dumps(result['gaps'], sort_keys=True)}",
        "Only the requested-window overlap of these excluded source brackets is omitted from accrual.",
        "",
        "## Raw acquisition summary",
        "Raw observation edge gaps and cadence diagnostics are acquisition facts, not additional excluded modeled intervals; boundary support and the configured threshold determine modeled coverage above.",
        f"Raw in-window observation boundary offsets (seconds): leading={result['leading_gap_seconds']}; trailing={result['trailing_gap_seconds']}",
        f"Raw observation coverage (unchanged): {json.dumps(result['observation_coverage'], sort_keys=True)}",
    ]) in report


@pytest.mark.parametrize('gap', ['before', 'after', 'internal', 'truncated'])
def test_bracketed_model_preserves_missing_or_oversized_intervals(tmp_path, gap):
    package, files = setup(tmp_path)
    data = bracketed_dataset()
    if gap in ('before', 'after'):
        data['_envelope']['boundary_observations'][gap] = None
    elif gap == 'internal':
        data['rows'][1]['timestamp'] = '2026-01-03T03:00:00Z'
    else:
        data['_envelope']['provider_truncated'] = True
    assert simulate_rates(package, files, data)['requested_window_complete'] is False


def test_source_edit_during_preparation_retains_failed_receipt_without_execution(
    tmp_path, monkeypatch
):
    from nerya.skills.builtin.backtest.scripts import replay_inputs, rate_replay

    package, _ = setup(tmp_path)
    target = tmp_path / "strategies" / "rate_test"
    target.parent.mkdir()
    package.root.rename(target)

    def prepare(*args, **kwargs):
        path = target / "main.py"
        path.write_text(path.read_text() + "\n# changed while preparing\n")
        return prepared_input(dataset())

    monkeypatch.setattr(replay_inputs, "prepare_inputs", prepare)
    monkeypatch.setattr(
        rate_replay,
        "simulate_rates",
        lambda *a, **k: pytest.fail("stale source executed"),
    )
    with pytest.raises(ValueError, match="source changed during data preparation"):
        rate_replay.run_rate_backtest(workspace=tmp_path, strategy_id="rate_test")
    runs = list((target / "backtests").iterdir())
    assert len(runs) == 1
    receipt = json.loads((runs[0] / "run.json").read_text())
    assert receipt["status"] == "failed" and receipt["phase"] == "preparing_data"
    assert not (runs[0] / "result.json").exists()


def test_real_rate_assets_generated_from_explicit_manifest_execute_via_native_producer(
    tmp_path, monkeypatch
):
    from nerya.core.config import Config
    from nerya.core.paths import WorkspacePaths
    from nerya.skills.builtin.backtest.scripts import replay_inputs
    from nerya.tools.native.strategy_runtime import (
        strategy_backtest_handler,
        strategy_draft_proposal_handler,
    )
    from nerya.tools.result_contracts import tool_json_data
    from nerya.tools.types import ToolCall

    _, files = setup(tmp_path)
    manifest = yaml_io.loads(files["strategy.yml"])
    manifest["params"]["rate_policy"] = {
        "entry_apy_percent": 10,
        "exit_apy_percent": 2,
        "max_age_seconds": 60,
        "allocation_units": 1000,
    }
    declaration = {
        "version": 1,
        "datasets": [
            {
                "id": "rates",
                "provider": "fixture",
                "action": "rates",
                "args": {},
            }
        ],
    }
    draft = strategy_draft_proposal_handler(
        ToolCall(
            name="strategy_draft_proposal",
            arguments={
                "strategy_id": "rate_test",
                "execution_mode": "script",
                "markets": manifest["markets"],
                "accounts": manifest["accounts"],
                "create_tuning": False,
                "files": {
                    "strategy.yml": yaml_io.dumps(manifest),
                    "replay_inputs.json": json.dumps(declaration),
                    "tests/test_contract.py": """from aave_rate_policy import decide_aave_rate
def test_actual_policy_entry_exit():
    from datetime import datetime, timezone
    policy = dict(entry_apy_percent=5, exit_apy_percent=2, max_age_seconds=60, allocation_units=10)
    source = dict(pool_id='fixture', chain='synthetic', asset='TEST', project='fixture')
    obs = dict(source, timestamp='2026-01-01T00:00:00Z', deposit_apy_percent=6)
    decision = decide_aave_rate(policy=policy, source=source, observation=obs,
        position=dict(supplied_units=0, cash_units=5), as_of=datetime(2026,1,1,tzinfo=timezone.utc))
    assert decision['action'] == 'lend_supply' and decision['amount_units'] == 5
""",
                },
            },
        ),
        config=Config(WorkspacePaths(tmp_path), {}),
    )
    assert not draft.is_error, draft.error
    authored = tool_json_data(draft)
    assert authored["implementation_status"] == "rate_policy_simulation"
    assert authored["validation"]["ok"], authored["validation"]
    assert authored["validation"]["behavioral"]["status"] == "passed"
    assert "aave_rate_policy.py" in authored["files"]

    class Registry:
        def call_replay_input(self, provider, action, args, *, cancel_token=None):
            assert (provider, action, args) == ("fixture", "rates", {})
            data = dataset()
            data["rows"][1]["timestamp"] = "2026-01-02T00:00:00.000Z"
            data["_envelope"]["coverage"]["completeness"] = "not_asserted"
            return data

    monkeypatch.setattr(replay_inputs, "build_data_api_registry", Registry)
    result = strategy_backtest_handler(
        ToolCall(
            name="strategy_backtest",
            arguments={
                "proposal_id": authored["proposal_id"],
            },
        ),
        config=Config(WorkspacePaths(tmp_path), {}),
    )
    assert not result.is_error, result.error
    data = tool_json_data(result)
    assert data["replay"]["strategy_execution_verified"] is True
    assert data["replay"]["decisions"] == 3
    assert data["replay"]["simulated_trades"] == 2
    assert data["performance_evidence"] is False
    assert data["final_equity_usd"] > 0
    assert data["requested_window_complete"] is True
    assert data["coverage_reason"] == "modeled_intervals_covered"
    assert data["observation_coverage"]["completeness"] == "not_asserted"
    assert data["provenance"]["datasets"][0]["requested_window_complete"] is None
    assert "observation_coverage_gap" not in data["limitation_codes"]
    checks = {
        row["check_id"]: row["status"]
        for row in result.metadata["verification"]["checks"]
    }
    assert checks["historical_data"] == "passed"
    assert checks["execution_integrity"] == "passed"
    assert checks["data_coverage"] == "passed"
    assert checks["historical_performance"] == "missing"


@pytest.mark.parametrize("engine", ["auto", "rates"])
def test_native_tool_selects_rate_contract_before_candle_or_freeform(
    tmp_path, monkeypatch, engine
):
    from nerya.core.config import Config
    from nerya.core.paths import WorkspacePaths
    from nerya.tools.native.strategy_runtime import strategy_backtest_handler
    from nerya.tools.types import ToolCall
    from nerya.tools.result_contracts import tool_json_data
    from nerya.skills.builtin.backtest.scripts import rate_replay

    package, _ = setup(tmp_path)
    target = tmp_path / "strategies" / "rate_test"
    target.parent.mkdir()
    package.root.rename(target)
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "performance_evidence": False,
            "replay": {"strategy_execution_verified": True},
        }

    monkeypatch.setattr(rate_replay, "run_rate_backtest", run)
    result = strategy_backtest_handler(
        ToolCall(
            name="strategy_backtest",
            arguments={"strategy_id": "rate_test", "engine": engine},
        ),
        config=Config(WorkspacePaths(tmp_path), {}),
    )
    assert not result.is_error and len(calls) == 1
    assert tool_json_data(result)["engine"] == "rates"


def test_v2_actual_apr_policy_consumes_exact_index_grid_and_rejects_incomplete_inputs(tmp_path, monkeypatch):
    from copy import deepcopy
    from datetime import datetime, timezone

    from nerya.skills.builtin.backtest.scripts import rate_replay, replay_inputs

    package, files = setup(tmp_path)
    manifest = yaml_io.loads(files['strategy.yml'])
    params = manifest['params']
    params['rate_source']['rate_metric'] = 'base_supply_rate_percent'
    params['rate_replay'].update(version=2, accrual_basis='aave_normalized_income', fee_bps=100)
    params['rate_policy'] = dict(entry_apr_percent=4, exit_apr_percent=2,
                                 max_age_seconds=12, allocation_units=100)
    files['strategy.yml'] = yaml_io.dumps(manifest)
    templates = Path(__file__).parents[1] / 'nerya/skills/builtin/strategy_author/templates'
    for template, name in (('aave_rate_strategy', 'main'), ('aave_rate_policy', 'aave_rate_policy'),
                           ('aave_supply', 'aave_supply')):
        files[name + '.py'] = (templates / (template + '.py.j2')).read_text()
    # Exercise the copied entrypoint, with an independent assertion of both clocks
    # and the absence of any APY alias in the candidate's actual input.
    files['main.py'] += '''
_rate_run = run
def run(ctx):
    obs = ctx.trigger['rate_observation']
    from datetime import datetime
    assert ctx.clock.now().timestamp() - datetime.fromisoformat(obs['timestamp'].replace('Z', '+00:00')).timestamp() == 12
    assert 'deposit_apy_percent' not in obs
    assert isinstance(obs['base_supply_rate_percent'], (int, float))
    return _rate_run(ctx)
'''
    for name, text in files.items():
        (package.root / name).write_text(text)
    package = load_package_from_dir(package.root)
    data = dataset()
    data['rows'].append({'timestamp': '2026-01-03T12:00:00Z'})
    env = data['_envelope']
    env.update(
        provider='aave', action='reserve_index_history', provider_truncated=False,
        observation_kind='historical_reserve_index_observations',
        replay_scope='historical_reserve_index_policy_simulation',
        observation_interval_seconds=86400, replay_input=True, point_in_time_ready=False,
        performance_evidence=False, financial_execution_allowed=False,
        availability_scope='unverified',
        simulation_assumption='Canonical-floor-block state with instantaneous observation; confirmation latency ignored.',
        official_identity=dict(pool_id='fixture-pool', chain_id=1, market='0x' + '1' * 40,
                               underlying_token='0x' + '2' * 40, a_token='0x' + '3' * 40),
        coverage=dict(scope='requested_observation_grid', expected_count=4, observations=4,
                      complete=True, missing_utc_days=[],
                      null_counts=dict(normalized_income_ray=0, base_supply_rate_percent=0)),
        point_sources=[],
    )
    env['requested_window']['end'] = data['rows'][-1]['timestamp']
    for i, (row, index, apr) in enumerate(zip(data['rows'], (10**27, 11 * 10**26, 121 * 10**25, 1331 * 10**24), (5, 3, 1, 6))):
        stamp = int(datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')).timestamp())
        row.pop('deposit_apy_percent', None)
        row.update(pool_id='fixture-pool', source_timestamp=datetime.fromtimestamp(stamp - 12, timezone.utc).isoformat().replace('+00:00', 'Z'),
                   block_timestamp=stamp - 12, block_number=100 + i, block_hash='0x' + str(i) * 64,
                   normalized_income_ray=str(index), liquidity_index=str(10**27),
                   current_liquidity_rate_ray=str(apr * 10**25), base_supply_rate_percent=str(apr),
                   a_token=env['official_identity']['a_token'], source_refs=[0])
        env['point_sources'].append(dict(
            requested_at_utc=row['timestamp'], identity_source_refs=[0], chain_source_ref=0,
            resolver_proof=dict(policy='floor_timestamp_lte_target_lt_next_timestamp',
                                requested_timestamp=stamp, source_refs=[0],
                                floor=dict(block_number=row['block_number'], block_hash=row['block_hash'], timestamp=stamp - 12),
                                next=dict(block_number=row['block_number'] + 1, block_hash='0x' + 'f' * 64, timestamp=stamp + 1))))
    original = deepcopy(data)
    result = simulate_rates(package, files, data)
    assert data == original
    assert [d['action'] for d in result['decisions']] == ['lend_supply', 'hold', 'lend_withdraw', 'lend_supply']
    assert [d['base_supply_rate_percent'] for d in result['decisions']] == [5, 3, 1, 6]
    assert [d['observation_timestamp'] for d in result['decisions']] == [r['source_timestamp'] for r in data['rows']]
    assert result['decisions'][1]['supplied_units'] == pytest.approx(110)
    assert result['decisions'][2]['amount_units'] == pytest.approx(121)
    assert result['equity'][-1]['equity'] == pytest.approx(1017.79)
    assert result['requested_window_complete'] is True
    assert result['modeled_coverage']['covered_seconds'] == 2.5 * 86400
    assert result['observation_coverage'] == env['coverage']
    assert result['isolation']['network'] == 'denied' and result['isolation']['process_exited']
    assert result['performance_evidence'] is False and result['financial_execution_allowed'] is False

    # Preserve the max-gap gate even on a fully acquired observation grid.
    limited = deepcopy(manifest)
    limited['params']['rate_replay']['max_gap_seconds'] = 50000
    partial = simulate_rates(package, {**files, 'strategy.yml': yaml_io.dumps(limited)}, data)
    assert partial['requested_window_complete'] is False and len(partial['gaps']) == 2
    assert partial['decisions'][2]['amount_units'] == 100

    # Metric selection is explicit; old APY fields cannot authorize an APR policy.
    bad_policy = deepcopy(manifest)
    bad_policy['params']['rate_policy']['entry_apy_percent'] = 4
    with pytest.raises(ValueError, match='candidate failed'):
        simulate_rates(package, {**files, 'strategy.yml': yaml_io.dumps(bad_policy)}, data)
    stale = deepcopy(manifest)
    stale['params']['rate_policy']['max_age_seconds'] = 11
    with pytest.raises(ValueError, match='candidate failed'):
        simulate_rates(package, {**files, 'strategy.yml': yaml_io.dumps(stale)}, data)
    for spoof in ("d['observation_timestamp'] = ctx.clock.now().isoformat().replace('+00:00', 'Z')",
                  "d['base_supply_rate_percent'] = 9", "d['apy_percent'] = d.pop('base_supply_rate_percent')"):
        forged = files['main.py'] + "\n_actual_run = run\ndef run(ctx):\n    result = _actual_run(ctx)\n    d = result.metadata['rate_decision']\n    " + spoof + "\n    return result\n"
        with pytest.raises(ValueError, match='decision contract mismatch'):
            simulate_rates(package, {**files, 'main.py': forged}, data)

    target = tmp_path / 'strategies' / 'rate_test'
    target.parent.mkdir()
    package.root.rename(target)
    monkeypatch.setattr(replay_inputs, 'prepare_inputs', lambda *a, **k: prepared_input(data))
    saved = rate_replay.run_rate_backtest(workspace=tmp_path, strategy_id='rate_test')
    assert saved['backtest_kind'] == 'rate_policy_replay'
    assert saved['replay']['scope'] == 'historical_rate_policy_simulation'
    assert saved['replay']['accrual_basis'] == 'aave_normalized_income'
    assert saved['replay']['rate_metric'] == 'base_supply_rate_percent'
    chart = json.loads(Path(saved['chart_path']).read_text())
    for output in (saved, saved['metrics'], saved['replay'], saved['provenance'], chart['meta']):
        assert output['accrual_basis'] == 'aave_normalized_income'
        assert output['rate_metric'] == 'base_supply_rate_percent'
        assert output['input_scope'] == env['replay_scope']
    assert saved['provenance']['input_provenance']['point_sources'] == env['point_sources']
    assert saved['limitation_codes'] == ['normalized_income_counterfactual', 'simulated_allocation_constant_price',
                                         'historical_pool_identity_unattested', 'instantaneous_observation_assumption']
    report = (tmp_path / saved['report_path']).read_text()
    assert 'aave_normalized_income' in report and 'base_supply_rate_percent' in report
    assert 'not Aave reserve-index reconstruction' not in report
    assert 'not actual realized yield' in report and 'confirmation latency' in report
    assert saved['performance_evidence'] is False and saved['point_in_time_ready'] is False

    monkeypatch.setattr(rate_replay, 'IsolatedStrategy', lambda *a, **k: pytest.fail('Invalid grid reached candidate'))
    for mutate in (
        lambda d: d['rows'].pop(1),
        lambda d: d['rows'][1].update(timestamp=d['rows'][0]['timestamp']),
        lambda d: d['rows'][1].pop('normalized_income_ray'),
        lambda d: d['rows'][1].update(normalized_income_ray=1e27),
        lambda d: d['rows'][1].update(normalized_income_ray='0'),
        lambda d: d['rows'][2].update(normalized_income_ray=str(10**27)),
        lambda d: d['rows'][1].update(base_supply_rate_percent=None),
        lambda d: d['rows'][1].update(base_supply_rate_percent='NaN'),
        lambda d: d['rows'][1].update(base_supply_rate_percent='30'),
        lambda d: d['rows'][1].update(pool_id='other'),
        lambda d: d['rows'][1].update(a_token='0x' + '4' * 40),
        lambda d: d['rows'][1].update(source_timestamp='2026-01-02T00:00:01Z'),
        lambda d: d['rows'][1].update(block_timestamp=0),
        lambda d: d['rows'][1].update(source_refs=[100]),
        lambda d: d['_envelope']['point_sources'].pop(),
        lambda d: d['_envelope']['point_sources'][1]['resolver_proof']['next'].update(timestamp=0),
        lambda d: d['_envelope'].update(observation_kind='historical_rate_observations'),
        lambda d: d['_envelope'].update(replay_scope='live'),
        lambda d: d['_envelope'].update(provider_truncated=True),
        lambda d: d['_envelope']['official_identity'].update(pool_id='other'),
        lambda d: d['_envelope']['pool'].update(symbol='OTHER'),
        lambda d: d['_envelope']['coverage'].update(complete=False),
        lambda d: d['_envelope']['coverage'].update(expected_count=3),
        lambda d: d['_envelope']['coverage']['null_counts'].update(normalized_income_ray=1),
    ):
        damaged = deepcopy(data)
        mutate(damaged)
        with pytest.raises(ValueError):
            simulate_rates(package, files, damaged)
    for mutate in (
        lambda p: p['rate_source'].pop('rate_metric'),
        lambda p: p['rate_source'].update(rate_metric='deposit_apy_percent'),
        lambda p: p['rate_replay'].pop('accrual_basis'),
        lambda p: p['rate_replay'].update(accrual_basis='apy'),
        lambda p: p['rate_replay'].update(version=1),
    ):
        damaged = deepcopy(manifest)
        mutate(damaged['params'])
        with pytest.raises(ValueError):
            simulate_rates(package, {**files, 'strategy.yml': yaml_io.dumps(damaged)}, data)
