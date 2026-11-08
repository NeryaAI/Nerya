"""Synthetic fixtures only: no historical performance or live acceptance."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

import pytest

from nerya.core import yaml_io
from nerya.skills.builtin.backtest.scripts.freeform_sandbox import execute_freeform
from nerya.skills.builtin.backtest.scripts.lp_accounting import Q128
from nerya.skills.builtin.backtest.scripts.lp_replay import _inputs, simulate_lp_policy
from nerya.strategies.package import load_package_from_dir

pytestmark = pytest.mark.smoke
macos = pytest.mark.skipif(sys.platform != "darwin", reason="actual freeform Seatbelt required")
TEMPLATES = Path(__file__).parents[1] / "nerya/skills/builtin/strategy_author/templates"
POOL, TOKEN0, TOKEN1 = ("0x" + c * 40 for c in ("3", "1", "2"))


def manifest():
    binding = {"id": "builtin:uniswap", "revision": "synthetic", "abi_version": 1}
    source = dict(source_id="lp_fixture", wallet_id="synthetic-wallet", chain="Ethereum",
        chain_id=1, protocol="uniswap_v3", pool_id=POOL, token_id=None,
        token0=TOKEN0, token1=TOKEN1, decimals=[6, 6], fee=500, tick_spacing=10,
        tick_lower=-100, tick_upper=100, component_binding=binding)
    source["account_request"] = {"kind": "lp_add", "wallet_id": source["wallet_id"], "chain": source["chain"],
        "protocol": source["protocol"], "component_id": binding["id"],
        "parameters": {"pool_id": POOL}}
    return {"id": "lp_fixture", "mode": "paper", "entrypoint": "main.py:run",
        "markets": ["UNISWAP_V3:TEST0/TEST1"], "accounts": ["paper_fixture"],
        "schedule": {"type": "none", "enabled": False},
        "params": {"lp_source": source,
            "lp_replay": {"version": 1, "dataset_id": "lp_history",
                "initial_token0_units": "1000", "initial_token1_units": "1000",
                "action_cost_token1": "0.25", "max_gap_seconds": 60},
            "lp_policy": {"max_age_seconds": 60, "persistence_seconds": 10,
                "cooldown_seconds": 100, "cost_multiple": 2, "enter_when_empty": True,
                "exit_below_principal_usd": 0, "collect_min_fees_usd": 1},
            "lp_execution": {"operation_key": "synthetic-cycle",
                "requests": {"lp_add": deepcopy(source["account_request"])}}}}


def row_hashes(data):
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    for row in data["rows"]:
        row.pop("row_sha256", None)
        row["row_sha256"] = digest(row)
    data["_envelope"]["rows_sha256"] = digest(data["rows"])
    data["_envelope"]["returned_rows_sha256"] = digest(data["rows"])
    return data


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


def dataset(times=(1700000000, 1700000012, 1700000048), *, utc=False, offsets=(5, 7)):
    identity = dict(chain_id=1, pool_address=POOL, token0=TOKEN0, token1=TOKEN1,
                    decimals=[6, 6], fee=500, tick_spacing=10)
    rows = []
    for i, stamp in enumerate(times):
        row = {**identity, "block_number": 100 + i, "block_hash": "0x" + f"{i + 1:064x}",
            "timestamp": stamp, "sqrt_price_x96": str(2**96), "tick": 0,
            "liquidity": "1000000000000", "tick_lower": -100, "tick_upper": 100,
            "tick_lower_initialized": True, "tick_upper_initialized": True}
        for j in (0, 1):
            row[f"fee_growth_global{j}_x128"] = str(i * Q128)
            for side in ("lower", "upper"):
                row[f"tick_{side}_fee_growth_outside{j}_x128"] = "0"
        rows.append(row)
    start, end = rows[0]["block_number"], rows[-1]["block_number"]
    window = dict(start_block=start, end_block=end, step_blocks=1)
    resolution = {}
    if utc:
        window = dict(start=iso(times[0] + offsets[0]), end=iso(times[-1] + offsets[1]), step_blocks=1)
        resolution = dict(policy="floor_block_timestamp_lte_requested_utc",
            start_resolver_reads=4, end_resolver_reads=3, start_block=start, end_block=end,
            start_timestamp=times[0], end_timestamp=times[-1],
            start_gap_seconds=offsets[0], end_gap_seconds=offsets[1],
            exact_start=offsets[0] == 0, exact_end=offsets[1] == 0)
    return row_hashes({"rows": rows, "_envelope": {"provider": "uniswap", "action": "pool_history",
        "mock": False, "chain_id": 1, "row_contract": "uniswap_v3_pool_history.v1",
        "observation_kind": "historical_lp_pool_state", "pool": identity,
        "requested_window": window, "window_resolution": resolution,
        "sampled_blocks": [r["block_number"] for r in rows], "endpoint_policy": "inclusive_end_once",
        "coverage": {"completeness": "not_asserted"},
        "sources": [{"sha256": "a" * 64}]}})


def sandbox_replay(tmp_path, *, saved=None, data=None, entry=None, policy=None, checks="", sampled=False):
    """Only import/execute saved source in the actual existing OS sandbox."""
    root = tmp_path / "candidate"
    root.mkdir()
    saved, data = saved or manifest(), data or dataset()
    files = {"strategy.yml": yaml_io.dumps(saved)}
    for name in ("lp_range_strategy", "lp_range_policy", "risk_rules", "lp_rebalance"):
        files["main.py" if name == "lp_range_strategy" else name + ".py"] = (
            TEMPLATES / (name + ".py.j2")).read_text()
    if entry is not None:
        files["main.py"] = entry
    if policy is not None:
        files["lp_range_policy.py"] = policy
    if sampled:
        files["main.py"] = (TEMPLATES / "lp_sampled_strategy.py.j2").read_text()
        files["lp_sampled_policy.py"] = (TEMPLATES / "lp_sampled_policy.py.j2").read_text()
    files["backtests/custom_backtest.py"] = (
        "import json\nfrom nerya.strategies import research\n"
        "result = research.replay_sampled_lp()\n"
        + checks + "\nresearch.write_json('result.json', result)\n")
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    package = load_package_from_dir(root)
    execution, blobs, safety = execute_freeform(package, root / "backtests/custom_backtest.py",
        artifact_names=("result.json",), timeout_seconds=15,
        prepared_inputs={"lp_history.json": json.dumps(data).encode()})
    return blobs, safety, execution


# This is a deliberately explicit caller-owned policy forecast, not fabricated
# by the consumer and not evidence of historical profitability. The real saved
# lp_range_strategy entrypoint and all its other helpers still execute.
ENTRY_POLICY = '''
from decimal import Decimal
def decide_lp(*, policy, source, snapshot, account, state, now):
    assert snapshot['economics']['expected_incremental_fees_usd'] is None
    assert snapshot['economics']['total_execution_cost_usd'] is None
    assert snapshot['economics']['expected_incremental_fees_token1'] is None
    assert snapshot['pool']['timestamp'] == now
    assert account['counterfactual'] and snapshot['counterfactual']
    if account['positions']:
        p = account['positions'][0]
        assert 'principal_value_usd' not in p and 'owed_value_usd' not in p
        return {'action':'hold', 'reason':'already_allocated'}, {'seen': now}
    forecast = policy['caller_forecast_token1']
    assert forecast['source'] == 'explicit_synthetic_caller_assumption'
    action = 'lp_add' if Decimal(forecast['fees']) > Decimal(snapshot['economics']['total_execution_cost_token1']) else 'hold'
    return {'action':action, 'reason':'explicit_caller_forecast'}, {'seen': now}
'''


@macos
def test_actual_saved_template_unknown_usd_economics_holds_without_fake_forecast(tmp_path):
    blobs, safety, execution = sandbox_replay(tmp_path)
    assert execution.returncode == 0, execution.stderr
    result = json.loads(blobs["result.json"])
    assert [d["lp_decision"] for d in result["decisions"]] == [
        {"action": "hold", "reason": "fee_economics_unknown"}] * 3
    assert result["outcome"] == "no_action" and not result["allocation_modeled"]
    assert "no_allocation_actual_strategy_use_incomplete" in result["limitations"]
    assert result["accounting_currency"] == "token1" and result["quote_token"] == TOKEN1
    assert not result["performance_evidence"] and not result["requested_window_complete"]
    assert all(row["modeled_value_token1"] == "2000" for row in result["modeled_curve"])
    assert "trades" not in result and "equity_curve" not in result
    assert safety["network"] == "denied" and safety["process_exited"]


@macos
def test_saved_entrypoint_with_explicit_caller_policy_routes_real_decision_to_ledger(tmp_path):
    saved = manifest()
    saved["params"]["lp_policy"]["caller_forecast_token1"] = {
        "source": "explicit_synthetic_caller_assumption", "fees": "1"}
    blobs, _, execution = sandbox_replay(tmp_path, saved=saved, policy=ENTRY_POLICY)
    assert execution.returncode == 0, execution.stderr
    result = json.loads(blobs["result.json"])
    curve = result["modeled_curve"]
    size = int(curve[0]["liquidity"])
    assert size > 0 and result["allocation_modeled"]
    assert [d["lp_decision"]["action"] for d in result["decisions"]] == ["lp_add", "hold", "hold"]
    assert [r["elapsed_seconds"] for r in curve] == [0, 12, 36]
    assert curve[0]["owed_base"] == ["0", "0"]
    assert list(map(int, curve[-1]["owed_base"])) == [2 * size, 2 * size]
    assert Decimal(curve[0]["modeled_value_token1"]) == Decimal("1999.75")
    assert not result["financial_execution_allowed"]


@macos
def test_max_gap_omits_growth_and_next_interval_uses_new_baseline(tmp_path):
    saved = manifest()
    saved["params"]["lp_policy"]["caller_forecast_token1"] = {
        "source": "explicit_synthetic_caller_assumption", "fees": "1"}
    blobs, _, execution = sandbox_replay(tmp_path, saved=saved, policy=ENTRY_POLICY,
        data=dataset((1700000000, 1700001000, 1700001012)))
    assert execution.returncode == 0, execution.stderr
    result = json.loads(blobs["result.json"])
    curve = result["modeled_curve"]
    assert curve[1]["owed_base"] == ["0", "0"]
    assert list(map(int, curve[2]["owed_base"])) == [int(curve[0]["liquidity"])] * 2
    assert result["gaps"] == [{"start": 1700000000, "end": 1700001000,
                               "seconds": 1000, "reason": "max_gap_seconds"}]


@macos
@pytest.mark.parametrize("entry, message", [
    ("def run(ctx): return ctx.financial.account_state({})", "methods unavailable"),
    ("def run(ctx): return ctx.state.compare_and_set('x',None,1)", "unsupported state hook"),
    ("def run(ctx): ctx.state.set('x','a'*33000)", "JSON exceeds bounds"),
    ("def run(ctx): return ctx.result.hold()", "result.metadata.lp_decision"),
    ("def run(ctx): return ctx.result.hold(metadata={'lp_decision':{'action':'lp_rebalance','reason':'x'}})", "unsupported decision"),
    ("def run(ctx): return ctx.result.hold(metadata={'lp_decision':{'action':'lp_add','reason':'x','tick_lower':-200}})", "changes fixed range"),
    ("def run(ctx): return ctx.result.hold(metadata={'funds_sent':True})", "claimed financial execution"),
])
def test_candidate_unsupported_capabilities_fail_closed_inside_sandbox(tmp_path, entry, message):
    blobs, _, execution = sandbox_replay(tmp_path, entry=entry)
    assert execution.returncode != 0 and message in execution.stderr
    assert not blobs


@macos
def test_context_state_is_bounded_isolated_and_only_current_sample_visible(tmp_path):
    entry = '''
def run(ctx):
    assert type(ctx.inputs).__name__ == 'StrategyInputContext'
    assert type(ctx.config).__name__ == 'StrategyConfig' and type(ctx.clock).__name__ == 'StrategyClock'
    assert type(ctx.result).__name__ == 'ResultBuilder' and ctx.mode == ctx.runmode == 'backtest'
    obs = ctx.inputs.source('lp_fixture')
    assert set(ctx.inputs.snapshot()) == {'source:lp_fixture'}
    assert 'rows' not in obs and obs['pool']['timestamp'] == ctx.clock.now().timestamp()
    state = ctx.state.get('history', [])
    assert not state or state[-1] < ctx.clock.now().timestamp()
    state.append(ctx.clock.now().timestamp())
    ctx.state.set('history', state)
    state.append('mutation')
    assert 'mutation' not in ctx.state.get('history')
    return ctx.result.hold(metadata={'lp_decision':{'action':'hold','reason':'bounded_state'}})
'''
    blobs, _, execution = sandbox_replay(tmp_path, entry=entry)
    assert execution.returncode == 0, execution.stderr
    assert len(json.loads(blobs["result.json"])["decisions"]) == 3


@macos
def test_uninitialized_boundary_after_entry_aborts_without_accrual(tmp_path):
    saved, data = manifest(), dataset()
    saved["params"]["lp_policy"]["caller_forecast_token1"] = {
        "source": "explicit_synthetic_caller_assumption", "fees": "1"}
    data["rows"][1]["tick_lower_initialized"] = False
    row_hashes(data)
    blobs, _, execution = sandbox_replay(tmp_path, saved=saved, data=data, policy=ENTRY_POLICY)
    assert execution.returncode != 0 and "not initialized" in execution.stderr
    assert not blobs


@macos
@pytest.mark.parametrize("floor,threshold,expected", [
    ("0", "999999999", ["lp_add", "hold", "hold"]),
    ("0", "1", ["lp_add", "lp_collect", "lp_collect"]),
    ("2001", "1", ["lp_add", "lp_remove", "lp_add"]),
])
def test_actual_sampled_assets_quote_unit_lifecycle(tmp_path, floor, threshold, expected):
    saved = manifest()
    saved["params"]["lp_sampled_policy"] = {"enter_when_empty": True,
        "exit_below_principal_token1": floor, "collect_min_fees_token1": threshold}
    blobs, _, execution = sandbox_replay(tmp_path, saved=saved, sampled=True)
    assert execution.returncode == 0, execution.stderr
    result = json.loads(blobs["result.json"])
    assert [d["lp_decision"]["action"] for d in result["decisions"]] == expected
    assert result["actions_modeled"] == sum(a != "hold" for a in expected)
    assert result["action_counts"] == {a: expected.count(a) for a in ("lp_add", "lp_remove", "lp_collect")}
    assert all(a["action_cost_token1"] == "0.25" for a in result["actions"])
    curve = result["modeled_curve"]
    if "lp_collect" in expected:
        assert curve[1]["owed_base"] == curve[2]["owed_base"] == ["0", "0"]
        assert Decimal(curve[1]["cash_units"][1]) > Decimal(curve[0]["cash_units"][1])
    if "lp_remove" in expected:
        assert curve[1]["liquidity"] == "0"
    assert result["accounting_currency"] == "token1" and not result["performance_evidence"]


@macos
@pytest.mark.parametrize("action", ["lp_collect", "lp_remove"])
def test_saved_caller_policy_collect_remove_are_ledger_actions(tmp_path, action):
    policy = '''
def decide_lp(*, policy, source, snapshot, account, state, now):
    n = state.get('n', 0)
    action = ['lp_add', ACTION, 'hold'][n]
    return {'action':action, 'reason':'explicit_synthetic_lifecycle'}, {'n':n+1}
'''.replace("ACTION", repr(action))
    blobs, _, execution = sandbox_replay(tmp_path, policy=policy)
    assert execution.returncode == 0, execution.stderr
    result = json.loads(blobs["result.json"])
    assert [a["action"] for a in result["actions"]] == ["lp_add", action]
    assert result["actions_modeled"] == 2 and result["action_counts"][action] == 1
    before, after = result["modeled_curve"][:2]
    assert after["owed_base"] == ["0", "0"]
    if action == "lp_remove":
        assert after["liquidity"] == "0"
    else:
        assert after["liquidity"] == before["liquidity"]
    assert Decimal(after["cash_units"][1]) > Decimal(before["cash_units"][1])


@pytest.mark.parametrize("mutate", [
    lambda s, d: d["rows"][1].update(chain_id=2),
    lambda s, d: d["rows"][1].update(pool_address=TOKEN0),
    lambda s, d: d["rows"][1].update(token0=TOKEN1),
    lambda s, d: d["rows"][1].update(decimals=[18, 6]),
    lambda s, d: d["rows"][1].update(tick_lower=-200),
    lambda s, d: d["rows"][1].update(tick_lower_initialized=None),
    lambda s, d: d["rows"][1].update(timestamp=d["rows"][0]["timestamp"]),
    lambda s, d: d["rows"][1].update(block_number=True),
    lambda s, d: d["rows"][1].update(block_hash="unknown"),
    lambda s, d: d["rows"][1].update(fee_growth_global0_x128="-1"),
    lambda s, d: d["_envelope"].update(mock=True),
    lambda s, d: d["_envelope"]["pool"].update(pool_address=TOKEN0),
    lambda s, d: d.update(rows=d["rows"] * 22),
    lambda s, d: s["params"]["lp_replay"].update(max_gap_seconds=0),
    lambda s, d: s["params"]["lp_replay"].update(initial_token0_units="NaN"),
    lambda s, d: s["params"]["lp_replay"].update(initial_token0_units=1.0),
    lambda s, d: s["params"]["lp_source"].update(token_id="existing-nft"),
])
def test_invalid_prepared_inputs_rejected_before_any_candidate_execution(mutate):
    saved, data = manifest(), dataset()
    mutate(saved, data)
    # Reach the semantic guard too: a matching digest cannot validate bad rows.
    row_hashes(data)
    def forbidden(ctx):
        pytest.fail("invalid inputs executed candidate")
    with pytest.raises(ValueError):
        simulate_lp_policy(forbidden, saved, data)


def test_input_validation_does_not_mutate_supplied_objects():
    saved, data = manifest(), dataset()
    before = deepcopy((saved, data))
    # Invalid callable is rejected before any candidate or IO operation.
    with pytest.raises(ValueError, match="callable"):
        simulate_lp_policy(None, saved, data)
    assert (saved, data) == before


@pytest.mark.parametrize("window", [None, {}, {"start_block": 100},
    {"start": "2023-11-14", "end_block": 102, "step_blocks": 1},
    dict(start_block=100, end_block=102, step_blocks=1, start="2023-11-14", end="2023-11-15"),
    dict(start_block=True, end_block=102, step_blocks=1),
    dict(start_block=-1, end_block=102, step_blocks=1),
    dict(start_block=100, end_block=2**64, step_blocks=1),
    dict(start_block=102, end_block=100, step_blocks=1),
    dict(start_block=0, end_block=2**64-1, step_blocks=1),
    dict(start_block=100, end_block=102, step_blocks=0),
    dict(start_block=100, end_block=102, step_blocks=True),
    dict(start_block=100, end_block=102, step_blocks=2),
    dict(start_block=101, end_block=102, step_blocks=1),
    dict(start_block=100, end_block=101, step_blocks=1),
    dict(start_block=99, end_block=102, step_blocks=1),
    dict(start="2023-11-15", end="2023-11-14", step_blocks=1),
    dict(start="2023-11-14T00:00:00+08:00", end="2023-11-15", step_blocks=1),
    dict(start="2023-11-14T00:00:00", end="2023-11-15", step_blocks=1),
    dict(start="2023-11-14T00:00:00.001Z", end="2023-11-15", step_blocks=1),
    dict(start="1969-12-31", end="2023-11-15", step_blocks=1),
    dict(start="2023-02-30", end="2023-11-15", step_blocks=1),
])
def test_window_contract_rejected_before_candidate(window):
    data = dataset()
    data["_envelope"]["requested_window"] = window
    with pytest.raises(ValueError, match="window|block|sample|integer"):
        simulate_lp_policy(lambda ctx: pytest.fail("invalid window executed candidate"), manifest(), data)


@pytest.mark.parametrize("key,value", [
    ("rows_sha256", None), ("rows_sha256", "f" * 64), ("returned_rows_sha256", "f" * 64),
    ("sampled_blocks", None), ("sampled_blocks", [100, 102]),
    ("sampled_blocks", [100, 101, 103]), ("sampled_blocks", [100.0, 101, 102]),
    ("endpoint_policy", None), ("endpoint_policy", "exclusive_end"),
    ("window_resolution", {"start_block": 99, "end_block": 102}),
])
def test_producer_sampling_and_hash_contract_rejected(key, value):
    data = dataset()
    if value is None:
        data["_envelope"].pop(key)
    else:
        data["_envelope"][key] = value
    with pytest.raises(ValueError):
        simulate_lp_policy(lambda ctx: pytest.fail("invalid producer contract executed candidate"), manifest(), data)


@pytest.mark.parametrize("change", [
    {"policy": "nearest_block"}, {"start_block": 99}, {"end_block": 103},
    {"start_timestamp": 1699999999}, {"end_timestamp": 1700000047},
    {"start_gap_seconds": 0}, {"end_gap_seconds": -1},
    {"exact_start": True}, {"exact_end": True}, {"exact_start": 0},
    {"start_resolver_reads": 33}, {"end_resolver_reads": True},
])
def test_utc_floor_requires_matching_producer_resolution(change):
    data = dataset(utc=True)
    data["_envelope"]["window_resolution"].update(change)
    with pytest.raises(ValueError):
        simulate_lp_policy(lambda ctx: pytest.fail("bad UTC resolution executed candidate"), manifest(), data)


@pytest.mark.parametrize("key", list(dataset(utc=True)["_envelope"]["window_resolution"]))
def test_utc_floor_requires_each_documented_resolution_field(key):
    data = dataset(utc=True)
    del data["_envelope"]["window_resolution"][key]
    with pytest.raises(ValueError):
        _inputs(manifest(), data)


def test_rehashed_rows_cannot_escape_resolved_blocks_or_utc_end():
    for key, value in (("block_number", 103), ("timestamp", 1700000056)):
        data = dataset(utc=True)
        data["rows"][-1][key] = value
        row_hashes(data)
        with pytest.raises(ValueError):
            _inputs(manifest(), data)


def test_tampered_rows_rejected_without_optional_returned_hash():
    data = dataset()
    del data["_envelope"]["returned_rows_sha256"]
    data["rows"][1]["liquidity"] = "1"
    with pytest.raises(ValueError, match="hash"):
        _inputs(manifest(), data)
    row_hashes(data)
    del data["_envelope"]["returned_rows_sha256"]
    assert len(_inputs(manifest(), data)[3]) == 3


@pytest.mark.parametrize("utc,offsets", [(False, (0, 0)), (True, (0, 0)), (True, (5, 7))])
def test_valid_windows_preserve_floor_offsets_and_gaps_without_mutation(utc, offsets):
    data = dataset(utc=utc, offsets=offsets)
    before = deepcopy(data)
    observations = _inputs(manifest(), data)[3]
    admitted = data["rows"][1:] if utc and offsets[0] else data["rows"]
    assert [stamp for stamp, _ in observations] == [r["timestamp"] for r in admitted]
    assert [row for _, row in observations] == admitted
    assert data == before


def test_rows_before_requested_start_cannot_contradict_floor_evidence():
    with pytest.raises(ValueError, match="floor window"):
        _inputs(manifest(), dataset(utc=True, offsets=(50, 7)))


def test_inclusive_end_once_with_non_dividing_step():
    data = dataset()
    for row, block in zip(data["rows"], (100, 106, 110)):
        row["block_number"] = block
    data["_envelope"]["requested_window"].update(end_block=110, step_blocks=6)
    data["_envelope"]["sampled_blocks"] = [100, 106, 110]
    assert len(_inputs(manifest(), row_hashes(data))[3]) == 3


@pytest.mark.parametrize("flag", [None, False, True])
def test_two_point_utc_floor_admits_only_inside_window_without_new_flag(flag):
    data = dataset((1700000000, 1700000012), utc=True)
    if flag is not None:
        data["_envelope"]["window_resolution"]["start_sample_may_precede_requested_window"] = flag
    observations = _inputs(manifest(), data)[3]
    assert [stamp for stamp, _ in observations] == [1700000012]


@macos
@pytest.mark.parametrize("times,offsets", [((1700000000, 1700000012), (5, 7)),
    ((1700000000, 1700000012, 1700000048), (5, 7)),
    ((1790553599, 1793145599), (1, 1))])
def test_utc_floor_baseline_is_not_a_decision_or_fee_interval(tmp_path, times, offsets):
    saved = manifest()
    saved["params"]["lp_sampled_policy"] = {"enter_when_empty": True,
        "exit_below_principal_token1": "0", "collect_min_fees_token1": "1"}
    data = dataset(times, utc=True, offsets=offsets)
    blobs, _, execution = sandbox_replay(tmp_path, saved=saved, data=data, sampled=True)
    assert execution.returncode == 0, execution.stderr
    result = json.loads(blobs["result.json"])
    assert [d["timestamp"] for d in result["decisions"]] == list(times[1:])
    assert [a["action"] for a in result["actions"]] == ["lp_add"] + ["lp_collect"] * (len(times) - 2)
    assert result["modeled_curve"][0]["elapsed_seconds"] == 0
    assert result["modeled_curve"][0]["owed_base"] == ["0", "0"]
    assert result["requested_window"] == data["_envelope"]["requested_window"]
    assert result["window_resolution"] == data["_envelope"]["window_resolution"]
    assert [r["timestamp"] for r in result["observations"]] == list(times)
    assert [r["admitted"] for r in result["observations"]] == [False] + [True] * (len(times) - 1)
    assert not result["requested_window_complete"] and not result["performance_evidence"]
    if len(times) == 3:
        assert [r["elapsed_seconds"] for r in result["modeled_curve"]] == [0, 36]
        size = int(result["modeled_curve"][0]["liquidity"])
        first, last = result["modeled_curve"]
        assert Decimal(last["cash_units"][0]) - Decimal(first["cash_units"][0]) == Decimal(size) / 10**6


@pytest.mark.parametrize("utc", [False, True])
def test_actual_offline_producer_envelope_matches_consumer(utc):
    from test_uniswap_history_data import ARGS, CatalogFake, Fake
    from nerya.data_api.uniswap_history import UniswapHistoryProvider
    from nerya.data_api.types import DataApiContext

    args = deepcopy(ARGS)
    if utc:
        args.pop("start_block")
        args.pop("end_block")
        args.update(start=iso(1600001205), end=iso(1600001327))
    data = UniswapHistoryProvider(Fake(), catalog_http=CatalogFake()).pool_history(DataApiContext(), args)
    saved = manifest()
    identity = data["_envelope"]["pool"]
    saved["params"]["lp_source"].update(identity, pool_id=identity["pool_address"],
        tick_lower=args["tick_lower"], tick_upper=args["tick_upper"])
    observations = _inputs(saved, data)[3]
    assert [row["block_number"] for _, row in observations] == ([106, 110] if utc else [100, 106, 110])


@macos
def test_local_typed_identities_keep_other_snapshot_values_redacted(tmp_path):
    saved = manifest()
    saved["params"]["lp_source"]["wallet_id"] = "sk-" + "s" * 40
    entry = '''
def run(ctx):
    obs = ctx.inputs.source('lp_fixture')
    assert obs['wallet_id'] == '***REDACTED***'
    assert obs['pool_id'] == ctx.config.params['lp_source']['pool_id']
    assert len(obs['pool']['block_hash']) == 66
    return ctx.result.hold(metadata={'lp_decision':{'action':'hold','reason':'typed_public'}})
'''
    _, _, execution = sandbox_replay(tmp_path, saved=saved, entry=entry)
    assert execution.returncode == 0, execution.stderr


def test_date_only_and_plus_zero_utc_windows_need_no_caller_clock_cutoff():
    # Historical snapshots are deterministic even if replayed under an earlier caller clock.
    start = int(datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp())
    data = dataset((start, start + 12, start + 48), utc=True, offsets=(0, 0))
    data["_envelope"]["requested_window"]["start"] = "2099-01-01"
    data["_envelope"]["requested_window"]["end"] = "2099-01-01T00:00:48+00:00"
    assert len(_inputs(manifest(), data)[3]) == 3


def test_report_redaction_retains_only_host_verified_public_denomination():
    from nerya.skills.builtin.backtest.scripts.freeform_run import _redact_artifact, REPORT_NAMES
    from nerya.skills.builtin.backtest.scripts.replay_inputs import json_bytes

    blob = json_bytes(dataset())
    prepared = {'files': {'lp_history.json': blob}, 'receipts': [{
        'id': 'lp_history', 'provider': 'uniswap', 'action': 'pool_history',
        'sha256': hashlib.sha256(blob).hexdigest()}]}
    secrets = ['sk-' + 's' * 40, '0x' + 'a' * 64,
        '0x' + '4' * 40, 'sk-' + TOKEN1, TOKEN1 + 'abcd', 'prefix/' + TOKEN1]
    text = ('Quote token: ' + TOKEN1 + '\nAccounting currency: token1:' + TOKEN1
        + '\nSecrets: ' + ' '.join(secrets))
    for name in REPORT_NAMES:
        clean = _redact_artifact(name, text, prepared)
        assert 'Quote token: ' + TOKEN1 in clean
        assert 'Accounting currency: token1:' + TOKEN1 in clean
        assert all(secret not in clean for secret in secrets)
        assert '***REDACTED***' in clean
    for invalid in ({'files': {}, 'receipts': []},
                    {**prepared, 'files': {'lp_history.json': blob + b' '}},
                    {**prepared, 'receipts': [{**prepared['receipts'][0], 'provider': 'other'}]}):
        assert TOKEN1 not in _redact_artifact('report.md', text, invalid)
    assert TOKEN1 not in _redact_artifact('decisions.csv', text, prepared)
    assert 'token0:' + TOKEN1 not in _redact_artifact('report.md', 'token0:' + TOKEN1, prepared)


@macos
@pytest.mark.parametrize('sampled_with_disabled_cron', [False, True])
def test_three_actual_assets_serialize_artifacts_through_freeform_run(tmp_path, monkeypatch, sampled_with_disabled_cron):
    from nerya.skills.builtin.backtest.scripts import freeform_run, replay_inputs
    from nerya.strategies.verification import source_revision
    from nerya.strategies.workflow_service import _read_files

    root = tmp_path / "strategies" / "lp_fixture"
    (root / "scripts").mkdir(parents=True)
    saved = manifest()
    if sampled_with_disabled_cron:
        saved['schedule'] = {'type': 'cron', 'cron': '0 0 * * *', 'timezone': 'UTC', 'enabled': False}
        saved['params']['lp_replay']['mode'] = 'sampled'
    saved['strategy_id'] = saved.pop('id')  # Match the actual candidate's canonical schema.
    saved["params"]["lp_sampled_policy"] = {"enter_when_empty": True,
        "exit_below_principal_token1": "0", "collect_min_fees_token1": "1"}
    files = {"strategy.yml": yaml_io.dumps(saved), **{
        target: (TEMPLATES / asset).read_text() for target, asset in (
            ("main.py", "lp_sampled_strategy.py.j2"),
            ("lp_sampled_policy.py", "lp_sampled_policy.py.j2"),
            ("scripts/custom_replay.py", "lp_sampled_replay.py.j2"))}}
    for name, content in files.items():
        (root / name).write_text(content)
    if sampled_with_disabled_cron:
        (root / "replay_inputs.json").write_text(json.dumps({"version": 1, "datasets": [{
            "id": "lp_history", "provider": "uniswap", "action": "pool_history",
            "args": {"pool_address": POOL, "start": "2026-10-01", "end": "2026-10-31"},
            "producer_ref": "replay://" + "a" * 64,
        }]}))
    revision = source_revision(_read_files(root)[0])
    data = dataset()
    data["_envelope"]["coverage"]["requested_window_complete"] = False
    blob = replay_inputs.json_bytes(data)
    receipt = {"id": "lp_history", "provider": "uniswap", "action": "pool_history",
        "sha256": hashlib.sha256(blob).hexdigest(), "rows": 3,
        "sources": data["_envelope"]["sources"], "requested_window": data["_envelope"]["requested_window"],
        "requested_window_complete": False, "data_kind": "historical_observations",
        "snapshot_file": "prepared_inputs/lp_history.json"}
    # Only the external data producer is synthetic. All three authored assets,
    # the public facade, OS boundary and host artifact/metrics/chart path are real.
    monkeypatch.setattr(replay_inputs, "prepare_inputs", lambda *a, **k: {
        "files": {"lp_history.json": blob}, "receipts": [receipt], "manifest": None})
    captured = {}
    def capture(*args, **kwargs):
        proc, artifacts, isolation = execute_freeform(*args, **kwargs)
        captured.update(artifacts)
        return proc, artifacts, isolation
    monkeypatch.setattr(freeform_run, "execute_freeform", capture)
    result = freeform_run.run_freeform_backtest(strategy_id="lp_fixture", workspace=tmp_path)

    assert {"result.json", "equity.csv", "trades.csv", "decisions.csv", "report.md"} <= captured.keys()
    raw = json.loads(captured["result.json"])
    assert raw["accounting_currency"] == "token1:" + TOKEN1
    assert raw["action_counts"] == {"lp_add": 1, "lp_collect": 2, "lp_remove": 0}
    assert [r["action"] for r in csv.DictReader(io.StringIO(captured["decisions.csv"].decode()))] == [
        "lp_add", "lp_collect", "lp_collect"]
    equity = list(csv.DictReader(io.StringIO(captured["equity.csv"].decode())))
    assert [Decimal(r["equity"]) for r in equity] == [
        Decimal(r["modeled_value_token1"]) for r in raw["modeled_curve"]]
    assert list(csv.DictReader(io.StringIO(captured["trades.csv"].decode()))) == []
    assert "Modeled actions: 3" in captured["report.md"].decode()
    assert TOKEN1 in captured["report.md"].decode()
    assert 'Quote token: ' + TOKEN1 in (tmp_path / result['report_path']).read_text()
    for name, content in files.items():
        assert result["isolation"]["snapshot_files"][name] == hashlib.sha256(content.encode()).hexdigest()
        assert (root / name).read_text() == content
    assert result["provenance"]["source_revision"] == revision
    assert result["provenance"]["source_changed_during_run"] is False
    assert result["isolation"]["prepared_inputs"][receipt["snapshot_file"]] == receipt["sha256"]
    assert result["isolation"]["process_exited"] and result["isolation"]["network"] == "denied"
    assert (Path(result["out_dir"]) / receipt["snapshot_file"]).read_bytes() == blob
    assert (Path(result["out_dir"]) / "decisions.csv").is_file()
    metrics = json.loads(Path(result["metrics_path"]).read_text())
    chart = json.loads(Path(result["chart_path"]).read_text())
    assert chart["meta"]["recorded_decision_rows"] == 3
    decisions_table = next(table for table in chart["tables"] if table["id"] == "decisions")
    assert len(decisions_table["rows"]) == 3
    assert [dict(zip(decisions_table["columns"], row)) for row in decisions_table["rows"]] == list(
        csv.DictReader((Path(result["out_dir"]) / "decisions.csv").read_text().splitlines()))
    assert metrics["accounting_currency"] == chart["meta"]["accounting_currency"] == result["accounting_currency"]
    for key in ("initial_capital_usd", "final_equity_usd", "total_return_usd", "total_return_pct"):
        assert metrics.get(key) is None
    assert chart["meta"]["initial_capital_usd"] is None and not chart.get("chart_blocks")
    assert "Modeled value (" in chart["panels"][0]["title"]
    assert chart["panels"][0]["series"][0]["name"] == "modeled_value"
    assert not result["performance_evidence"] and not result["requested_window_complete"]
    assert result["replay"]["strategy_execution_verified"] is True
    assert result["replay"]["scope"] == "sampled_fixed_range_counterfactual"
    assert json.loads(Path(result["run_receipt"]).read_text())["status"] == "completed"
    assert result["replay"]["action_counts"] == raw["action_counts"]
    assert result["replay"]["accounting_verified"] is True
    assert result["replay"]["input_observations"] == 3
    assert result["replay"]["prepared_input_sha256"] == receipt["sha256"]
    assert result["replay"]["rows_sha256"] == data["_envelope"]["rows_sha256"]
    assert result["replay"]["entrypoint"] == saved["entrypoint"]
    assert metrics["replay"] == chart["meta"]["replay"] == result["replay"]
    assert all("Changing step_blocks, max_samples, or endpoint alignment alone cannot establish full LP coverage." in item["coverage_message"] for item in (result, metrics, chart["meta"]))
    from nerya.core.config import Config
    from nerya.core.paths import WorkspacePaths
    from nerya.strategies.tool_verification import source_binding, attach_strategy_checks
    from nerya.tools.types import ToolCall, ToolResult
    config = Config(WorkspacePaths(tmp_path), {})
    binding = source_binding(config, ToolCall(name='strategy_backtest', arguments={'strategy_id': 'lp_fixture'}))
    def checks(payload):
        verified = attach_strategy_checks(ToolResult.from_json(tool_use_id='lp-check', name='strategy_backtest',
            data=payload), before=binding, after=binding, kind='backtest', config=config)
        return {c['check_id']: c for c in verified.content[0].data['verification']['checks']}
    observed = checks(result)
    assert observed['historical_data']['status'] == 'passed'
    assert observed['data_coverage']['status'] == 'failed'
    assert observed['data_coverage']['required'] is True
    assert observed['data_coverage']['message'] == result['coverage_message']
    assert observed['historical_performance']['status'] == 'missing'
    from nerya.task_delivery import _completion
    completion = _completion({
        'workflow_contract': {'required_checks': ['historical_data', 'data_coverage'],
                              'required_artifacts': ['report']},
        'stages': [{'id': 'replay', 'state': 'completed'}],
        'artifacts': [{'artifact_id': 'report', 'ref': binding['artifact_id']}],
        'checks': [{**row, **binding, 'producer_owned': True} for row in observed.values()],
    })
    assert completion['allowed'] is False and completion['status'] == 'incomplete'
    assert completion['reasons'] == ['check_failed:data_coverage']
    for flags, expected in [({}, 'missing'),
                            ({'coverage_ok': None, 'requested_window_complete': None}, 'missing'),
                            ({'coverage_ok': False}, 'failed'),
                            ({'requested_window_complete': False}, 'failed'),
                            ({'coverage_ok': False, 'requested_window_complete': True}, 'failed')]:
        payload = {k: v for k, v in result.items() if k not in {'coverage_ok', 'requested_window_complete'}}
        assert checks({**payload, **flags})['data_coverage']['status'] == expected
    snapshot_path = Path(result['out_dir']) / receipt['snapshot_file']
    snapshot_path.write_bytes(blob + b' ')
    assert checks(result)['historical_data']['status'] == 'missing'
    snapshot_path.write_bytes(blob)
    claimed = deepcopy(result)
    claimed['replay']['accounting_verified'] = False
    assert checks(claimed)['historical_data']['status'] == 'missing'

    # Candidate-written claims cannot select the host producer or grant proof.
    (root / "scripts/custom_replay.py").write_text(
        "from nerya.strategies.research import write_json\n"
        "write_json('result.json', {'model':'sampled_fixed_range', "
        "'strategy_execution_verified':True, 'accounting_verified':True, "
        "'allocation_modeled':True, 'lp_execution':{'actions_modeled':99}, "
        "'equity_curve':[{'ts':1,'equity':100},{'ts':2,'equity':101}], 'trades':[]})\n")
    forged = freeform_run.run_freeform_backtest(strategy_id="lp_fixture", workspace=tmp_path)
    assert forged["replay"]["strategy_execution_verified"] is False
    assert forged["replay"]["scope"] == "isolated_custom_calculation"
    assert "accounting_verified" not in forged["replay"]
    assert not forged["performance_evidence"] and not forged["requested_window_complete"]
    assert checks(forged)['historical_data']['status'] == 'missing'

    # Boundary gaps describe unmodeled time, without admitting the floor row.
    partial = dataset((1700000000, 1700000012, 1700000048), utc=True, offsets=(1, 1))
    from nerya.strategies.result import StrategyResult
    model = simulate_lp_policy(lambda ctx: StrategyResult.hold(
        metadata={"lp_decision":{"action":"hold", "reason":"fixture"}}), saved, partial)
    assert model["input_observations"] == 3 and model["admitted_observations"] == 2
    assert model["excluded_observations"] == 1
    assert model["leading_gap_seconds"] == 11 and model["trailing_gap_seconds"] == 1
    assert model["gaps"] == [] and not model["requested_window_complete"]
    assert not model["performance_evidence"] and not model["financial_execution_allowed"]
