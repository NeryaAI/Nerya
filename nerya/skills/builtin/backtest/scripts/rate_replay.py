"""Host-owned rate-policy simulation over public, attributable observations.

Candidate code runs in the existing Seatbelt worker with one historical
observation and current simulated position. The host owns accrual and balances;
candidate assertions cannot attest execution, coverage, or realized yield.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import math
import re

from .....tools.native.paths import to_workspace_relative
from .isolated_strategy import IsolatedStrategy


def _number(value, *, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError("rate replay requires finite numbers within bounds")
    return float(value)


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("rate replay requires UTC timestamps")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset().total_seconds() != 0:
        raise ValueError("rate replay requires UTC timestamps")
    return dt.timestamp()


def _index_observations(data, identity, window):
    """Validate the frozen reserve grid, never infer coverage from admission flags."""
    envelope, rows = data['_envelope'], data.get('rows')
    start, end = window
    interval = envelope.get('observation_interval_seconds')
    if (identity.get('rate_metric') != 'base_supply_rate_percent'
            or envelope.get('provider') != 'aave'
            or envelope.get('action') != 'reserve_index_history'
            or envelope.get('replay_scope') != 'historical_reserve_index_policy_simulation'
            or envelope.get('point_in_time_ready') is not False
            or envelope.get('performance_evidence') is not False
            or envelope.get('financial_execution_allowed') is not False
            or envelope.get('provider_truncated') is not False
            or envelope.get('boundary_observations')
            or type(interval) is not int or interval <= 0
            or start != int(start) or end != int(end)):
        raise ValueError('rate replay v2 requires an explicit historical reserve-index grid and APR metric')
    count = int(end - start) // interval + 1 + bool(int(end - start) % interval)
    coverage = envelope.get('coverage')
    if (not isinstance(rows, list) or not 2 <= count <= 500 or len(rows) != count
            or not isinstance(coverage, dict)
            or coverage.get('scope') != 'requested_observation_grid'
            or coverage.get('complete') is not True
            or type(coverage.get('expected_count')) is not int or coverage['expected_count'] != count
            or type(coverage.get('observations')) is not int or coverage['observations'] != count
            or coverage.get('missing_utc_days') != []
            or not isinstance(coverage.get('null_counts'), dict)
            or any(type(coverage['null_counts'].get(k)) is not int or coverage['null_counts'][k] != 0
                   for k in ('normalized_income_ray', 'base_supply_rate_percent'))):
        raise ValueError('rate replay v2 requires complete observed grid coverage')
    official = envelope.get('official_identity')
    if (not isinstance(official, dict) or official.get('pool_id') != identity['pool_id']
            or type(official.get('chain_id')) is not int or official['chain_id'] != 1
            or identity['chain'] != 'Ethereum' or identity['project'] != 'aave-v3'
            or any(not isinstance(official.get(k), str)
                   or not re.fullmatch(r'0x[0-9a-fA-F]{40}', official[k])
                   for k in ('market', 'underlying_token', 'a_token'))
            or any(k in identity and identity[k] != official[k]
                   for k in ('chain_id', 'market', 'underlying_token', 'a_token'))):
        raise ValueError('rate replay v2 requires exact official reserve identity')
    sources, points = envelope.get('sources'), envelope.get('point_sources')
    if (not isinstance(sources, list) or not sources
            or any(not isinstance(s, dict) or not isinstance(s.get('sha256'), str)
                   or not re.fullmatch(r'[0-9a-f]{64}', s['sha256']) for s in sources)
            or not isinstance(points, list) or len(points) != count):
        raise ValueError('rate replay v2 requires attributable point sources')

    def refs(value):
        if (not isinstance(value, list) or not value
                or any(type(ref) is not int or not 0 <= ref < len(sources) for ref in value)):
            raise ValueError('rate replay v2 source reference is invalid')

    def uint(value, *, positive=False):
        if (not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]{0,77}', value)
                or int(value) >= 2**256 or positive and int(value) == 0):
            raise ValueError('rate replay v2 requires exact reserve uint strings')
        return Decimal(value)

    observations, previous_index, previous_block = [], None, None
    for i, (row, point) in enumerate(zip(rows, points)):
        if not isinstance(row, dict) or not isinstance(point, dict):
            raise ValueError('rate replay v2 requires reserve observations and point proofs')
        stamp = _stamp(row.get('timestamp'))
        source_stamp = _stamp(row.get('source_timestamp'))
        block_stamp, number = row.get('block_timestamp'), row.get('block_number')
        if (stamp != min(start + i * interval, end)
                or type(block_stamp) is not int or block_stamp != source_stamp or not 0 <= source_stamp <= stamp
                or type(number) is not int or not 0 <= number < 2**64
                or previous_block is not None and number < previous_block
                or row.get('pool_id') != identity['pool_id'] or row.get('a_token') != official['a_token']
                or any(k in row and row[k] != identity[k] for k in ('chain', 'asset', 'project'))
                or not isinstance(row.get('block_hash'), str)
                or not re.fullmatch(r'0x[0-9a-fA-F]{64}', row['block_hash'])):
            raise ValueError('rate replay v2 observation identity, grid or source timestamp mismatch')
        index = uint(row.get('normalized_income_ray'), positive=True)
        stored = uint(row.get('liquidity_index'), positive=True)
        rate_ray = uint(row.get('current_liquidity_rate_ray'))
        rate_text = row.get('base_supply_rate_percent')
        if (not isinstance(rate_text, str) or len(rate_text) > 110
                or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', rate_text)):
            raise ValueError('rate replay v2 requires an exact decimal base supply APR')
        with localcontext() as precision:
            precision.prec = 80
            rate = Decimal(rate_text)
            if (rate != rate_ray / Decimal(10**25) or index < stored
                    or previous_index is not None and index < previous_index):
                raise ValueError('rate replay v2 reserve indices or APR are inconsistent')
        proof = point.get('resolver_proof')
        if not isinstance(proof, dict):
            raise ValueError('rate replay v2 requires an adjacent floor-block proof')
        floor, next_block = proof.get('floor'), proof.get('next')
        if (point.get('requested_at_utc') != row['timestamp']
                or proof.get('policy') != 'floor_timestamp_lte_target_lt_next_timestamp'
                or type(proof.get('requested_timestamp')) is not int or proof['requested_timestamp'] != stamp
                or floor != {'block_number': number, 'block_hash': row['block_hash'], 'timestamp': block_stamp}
                or not isinstance(next_block, dict) or type(next_block.get('block_number')) is not int
                or next_block['block_number'] != number + 1
                or type(next_block.get('timestamp')) is not int or not stamp < next_block['timestamp']
                or not isinstance(next_block.get('block_hash'), str)
                or not re.fullmatch(r'0x[0-9a-fA-F]{64}', next_block['block_hash'])):
            raise ValueError('rate replay v2 floor proof does not bind the observation clock')
        refs(row.get('source_refs'))
        refs(point.get('identity_source_refs'))
        refs([point.get('chain_source_ref')])
        refs(proof.get('source_refs'))
        observations.append((stamp, {
            **identity,
            'timestamp': datetime.fromtimestamp(source_stamp, timezone.utc).isoformat().replace('+00:00', 'Z'),
            'base_supply_rate_percent': _number(float(rate)),
            'normalized_income_ray': str(index),
        }))
        previous_index, previous_block = index, number
    return observations


def _inputs(package, files, data):
    from .....core import yaml_io

    saved = yaml_io.loads(files["strategy.yml"])
    if not isinstance(saved, dict):
        raise ValueError("rate replay requires a saved manifest")
    params = saved.get("params") or {}
    if not isinstance(params, dict) or not isinstance(data, dict):
        raise ValueError("rate replay requires structured params and input")
    policy, identity = params.get("rate_replay"), params.get("rate_source")
    fields = {
        "version",
        "dataset_id",
        "initial_cash_units",
        "unit_price_usd",
        "fee_bps",
        "max_gap_seconds",
    }
    v2 = isinstance(policy, dict) and type(policy.get('version')) is int and policy['version'] == 2
    if v2:
        fields.add('accrual_basis')
    if (
        not isinstance(policy, dict)
        or set(policy) != fields
        or type(policy["version"]) is not int
        or policy["version"] not in (1, 2)
        or v2 and policy.get('accrual_basis') != 'aave_normalized_income'
    ):
        actual = policy if isinstance(policy, dict) else {}
        from .....core.redaction import redact_display_dict

        receipt = redact_display_dict({
            'reason': 'invalid_rate_replay_schema', 'field': 'params.rate_replay',
            'required_fields': sorted(fields),
            'missing_fields': sorted(fields - set(actual)),
            'unknown_fields': sorted(set(actual) - fields, key=str),
            'expected_version': 2 if v2 else 1, 'actual_version': actual.get('version'),
            'actual_version_type': type(actual.get('version')).__name__,
            'actual_type': type(policy).__name__,
            'correction': 'Use the six v1 fields, or version integer 2 plus accrual_basis=aave_normalized_income. '
                'No additional accounting object exists. Move start_utc/end_utc to backtest scope '
                'or replay_inputs.json dataset args.start/args.end, preserving the admitted window.',
        })
        error = ValueError('Invalid params.rate_replay schema: ' + json.dumps(receipt, default=str))
        error.receipt = receipt
        raise error
    if not isinstance(identity, dict) or not all(
        isinstance(identity.get(k), str) and identity[k]
        for k in ("pool_id", "project", "chain", "asset")
    ):
        raise ValueError("rate replay requires exact source identity")
    cash = _number(policy["initial_cash_units"])
    price = _number(policy["unit_price_usd"])
    fee = _number(policy["fee_bps"]) / 10000
    gap = _number(policy["max_gap_seconds"])
    if cash <= 0 or price <= 0 or fee >= 1 or not 0 < gap <= 7 * 86400:
        raise ValueError("rate replay accounting assumptions out of bounds")
    envelope = data.get("_envelope") or {}
    if not isinstance(envelope, dict):
        raise ValueError("rate replay requires a provenance envelope")
    pool = envelope.get("pool") or {}
    if not isinstance(pool, dict):
        raise ValueError("rate replay requires source identity")
    if (
        envelope.get("mock") is not False
        or envelope.get("observation_kind") != (
            "historical_reserve_index_observations" if v2 else "historical_rate_observations")
        or any(
            pool.get("symbol" if k == "asset" else k) != identity[k]
            for k in ("pool_id", "project", "chain", "asset")
        )
    ):
        raise ValueError("rate replay source identity is unverified or mismatched")
    window = envelope.get("requested_window") or {}
    if not isinstance(window, dict):
        raise ValueError("rate replay requires a requested window")
    start, end = _stamp(window.get("start")), _stamp(window.get("end"))
    if not start < end or end - start > 366 * 86400:
        raise ValueError("rate replay window must be bounded and ordered")
    if v2:
        observations = _index_observations(data, identity, (start, end))
        return saved, policy, observations, (start, end), cash, price, fee, gap
    if identity.get('rate_metric', 'deposit_apy_percent') != 'deposit_apy_percent':
        raise ValueError('rate replay v1 requires the deposit APY metric')
    rows = data.get("rows")
    if not isinstance(rows, list) or not 2 <= len(rows) <= 500:
        raise ValueError("rate replay requires 2 to 500 observations")
    boundaries = envelope.get("boundary_observations") or {}
    before, after = boundaries.get("before"), boundaries.get("after")
    for row, bound, is_before in ((before, start, True), (after, end, False)):
        if row is not None and (not isinstance(row, dict) or
                not (_stamp(row.get("timestamp")) < bound if is_before else
                     _stamp(row.get("timestamp")) > bound)):
            raise ValueError("rate replay boundary observation is misplaced")
    rows = ([before] if before is not None else []) + rows + ([after] if after is not None else [])
    observations, prior = [], None
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("rate replay observations must be objects")
        stamp = _stamp(row.get("timestamp"))
        apy = _number(row.get("deposit_apy_percent"), minimum=-99.999)
        if (
            row.get("pool_id") != identity["pool_id"]
            or not (start <= stamp <= end or row is before or row is after)
            or prior is not None
            and stamp <= prior
            or apy > 1000000
        ):
            raise ValueError(
                "rate replay observations must have exact identity and ordered timestamps"
            )
        observations.append(
            (
                stamp,
                {
                    **identity,
                    "timestamp": datetime.fromisoformat(
                        row["timestamp"].replace("Z", "+00:00")
                    )
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "deposit_apy_percent": apy,
                },
            )
        )
        prior = stamp
    return saved, policy, observations, (start, end), cash, price, fee, gap


def simulate_rates(package, files, data, *, cancel_token=None, check_cancel=None):
    """Execute the saved entrypoint, then account only its validated decisions."""
    from .....strategies.verification import source_revision

    saved, policy, observations, window, cash, price, fee, max_gap = _inputs(
        package, files, data
    )
    v2 = policy['version'] == 2
    rate_metric = 'base_supply_rate_percent' if v2 else 'deposit_apy_percent'
    decision_metric = rate_metric if v2 else 'apy_percent'
    accrual_basis = 'aave_normalized_income' if v2 else 'previous_observation_apy'
    supplied, previous = 0.0, None
    equity, trades, decisions, gaps = [], [], [], []
    covered_seconds = 0.0
    state = {}

    def dispatch(method, args, kwargs):
        # Candidate state is independent of host balances and observations.
        if (
            method not in {"state.get", "state.set"}
            or not args
            or not isinstance(args[0], str)
        ):
            raise ValueError(
                "rate replay SDK permits only bounded candidate state get/set"
            )
        key = args[0]
        if len(key) > 200 or key.startswith("__"):
            raise ValueError("rate replay state key rejected")
        if method == "state.get":
            if len(args) > 2 or set(kwargs) - {"default"}:
                raise ValueError("rate replay state get arguments")
            return state.get(key, args[1] if len(args) == 2 else kwargs.get("default"))
        if len(args) != 2 or kwargs or len(state) >= 256 and key not in state:
            raise ValueError("rate replay state set arguments")
        import json

        if len(json.dumps(args[1], allow_nan=False).encode()) > 32000:
            raise ValueError("rate replay state too large")
        state[key] = args[1]
        return None

    worker = IsolatedStrategy(
        package,
        frozen_files=files,
        cancel_token=cancel_token,
        lifecycle_timeout=120,
        max_total_calls=2000,
    )
    with worker:
        if worker.safety.get("source_revision") != source_revision(files):
            raise ValueError("rate replay executed source mismatch")
        for stamp, observation in observations:
            if check_cancel:
                check_cancel()
            if previous:
                dt = max(0, min(stamp, window[1]) - max(previous[0], window[0]))
                if dt and stamp - previous[0] > max_gap:
                    gaps.append(
                        {
                            "start": datetime.fromtimestamp(previous[0], timezone.utc).isoformat().replace('+00:00', 'Z'),
                            "end": datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace('+00:00', 'Z'),
                            "seconds": stamp - previous[0],
                        }
                    )
                elif dt:
                    covered_seconds += dt
                    if v2:
                        with localcontext() as precision:
                            precision.prec = 80
                            ratio = Decimal(observation['normalized_income_ray']) / Decimal(previous[1]['normalized_income_ray'])
                        supplied *= float(ratio)
                    else:
                        supplied *= (1 + previous[1]["deposit_apy_percent"] / 100) ** (
                            dt / (365 * 86400)
                        )
            if not math.isfinite(supplied):
                raise ValueError('rate replay accrued balance exceeds numeric bounds')
            if stamp < window[0]:
                previous = stamp, observation
                continue
            if not equity and stamp > window[0]:
                equity.append({"ts": window[0], "equity": cash * price})
            if stamp > window[1]:
                equity.append({"ts": window[1], "equity": (cash + supplied) * price})
                break
            snapshot = {
                "strategy_id": package.strategy_id,
                "run_id": "rate-replay",
                "session_id": None,
                "mode": "backtest",
                "runmode": "backtest",
                "symbol": saved["markets"][0],
                "timeframe": saved.get("timeframe", "1d"),
                "clock": {"ts": stamp},
                "config": saved,
                "policy": saved.get("policy", {}),
                "trigger": {
                    "rate_observation": observation,
                    "lending_position": {
                        "supplied_units": supplied,
                        "cash_units": cash,
                    },
                },
            }
            result = worker.run(snapshot, dispatch)
            if not isinstance(result, dict) or result.get("status") not in {
                "hold",
                "ok",
            }:
                raise ValueError(
                    "rate replay candidate failed or claimed actual execution"
                )
            decision = (result.get("metadata") or {}).get("rate_decision")
            fields = {"action", "amount_units", "observation_timestamp", decision_metric}
            if (
                not isinstance(decision, dict)
                or set(decision) != fields
                or decision["observation_timestamp"] != observation["timestamp"]
                or _number(decision[decision_metric], minimum=0 if v2 else -99.999) != observation[rate_metric]
            ):
                raise ValueError("rate replay candidate decision contract mismatch")
            action, amount = decision["action"], _number(decision["amount_units"])
            if action == "hold":
                if amount != 0:
                    raise ValueError("rate replay hold has nonzero amount")
            elif action == "lend_supply":
                if amount <= 0 or amount * (1 + fee) > cash + 1e-9:
                    raise ValueError(
                        "rate replay supply exceeds simulated cash including costs"
                    )
                cash -= amount * (1 + fee)
                supplied += amount
            elif action == "lend_withdraw":
                if amount <= 0 or amount > supplied + 1e-9:
                    raise ValueError(
                        "rate replay withdrawal exceeds owned simulated position"
                    )
                supplied -= amount
                cash += amount * (1 - fee)
            else:
                raise ValueError("rate replay unknown decision")
            if min(cash, supplied) < -1e-9 or not all(
                math.isfinite(v) for v in (cash, supplied)
            ):
                raise ValueError("rate replay accounting invalid")
            nav = (cash + supplied) * price
            if not math.isfinite(nav):
                raise ValueError("rate replay modeled value exceeds numeric bounds")
            row = {
                "ts": stamp,
                **decision,
                "cash_units": cash,
                "supplied_units": supplied,
                "equity": nav,
            }
            decisions.append(row)
            if action != "hold":
                trades.append(
                    {
                        **row,
                        "side": action,
                        "size": amount,
                        "price": price,
                        "fee_units": amount * fee,
                        "execution": "simulated_rate_allocation",
                    }
                )
            equity.append({"ts": stamp, "equity": nav})
            previous = stamp, observation
    # Raw coverage stays untouched; modeled coverage describes interval accounting only.
    in_window = [t for t, _ in observations if window[0] <= t <= window[1]]
    leading, trailing = in_window[0] - window[0], window[1] - in_window[-1]
    coverage = data["_envelope"].get("coverage") or {}
    complete = bool(coverage) and covered_seconds == window[1] - window[0] and not (
        gaps
        or data["_envelope"].get("provider_truncated")
        or any(datetime.fromtimestamp(window[0], timezone.utc).date().isoformat() < day <
               datetime.fromtimestamp(window[1], timezone.utc).date().isoformat()
               for day in coverage.get("missing_utc_days", []))
        or (coverage.get("null_counts") or {}).get(rate_metric)
    )
    return {
        "equity": equity,
        "trades": trades,
        "decisions": decisions,
        "gaps": gaps,
        "leading_gap_seconds": leading,
        "trailing_gap_seconds": trailing,
        "requested_window_complete": complete,
        "accrual_basis": accrual_basis,
        "rate_metric": rate_metric,
        "input_scope": data['_envelope'].get('replay_scope', 'historical_rate_observations'),
        "point_in_time_ready": False,
        "financial_execution_allowed": False,
        "modeled_coverage": {"scope": "observed_normalized_income_ratios" if v2 else "source_bracketed_previous_rate_intervals",
                             "covered_seconds": covered_seconds,
                             "requested_seconds": window[1] - window[0],
                             "complete": complete},
        "observation_coverage": coverage,
        "performance_evidence": False,
        "strategy_execution_verified": True,
        "order_accounting_ok": True,
        "isolation": worker.safety,
        "assumptions": policy,
    }


def run_rate_backtest(
    *,
    workspace,
    proposal_id=None,
    strategy_id=None,
    validate_prepared_inputs=None,
    cancel_token=None,
    progress=None,
    runtime_config=None,
):
    """Existing producer envelope and artifacts, with a fixed host execution model."""
    from .....core.config import load_config
    from .....core.runtime_identity import BUILD_ID, SDK_BUILD_ID
    from .....strategies.workflow_service import _read_files
    from .....strategies.verification import source_revision
    from .....strategies.validator import static_scan_blockers
    from .backtest_run import _load_target_package
    from .replay_inputs import prepare_inputs
    from .run_state import RunTracker, atomic_json
    from .writers import create_run_dir, _write_rows
    from .freeform_run import _build_metrics, _render_chart, _table

    tracker = RunTracker(progress=progress, cancel_token=cancel_token)
    try:
        if bool(proposal_id) == bool(strategy_id):
            raise ValueError("select exactly one rate replay candidate")
        config = load_config(workspace)
        package = _load_target_package(config.paths, strategy_id, proposal_id)
        if not package.root.resolve().is_relative_to(config.paths.root.resolve()):
            raise ValueError("rate replay package outside workspace")
        if any(
            p.is_symlink()
            for p in [package.root, *package.root.parents]
            if p.is_relative_to(config.paths.root)
        ):
            raise ValueError("rate replay package symlink")
        files, omitted = _read_files(package.root)
        if omitted:
            raise ValueError("rate replay source snapshot incomplete")
        blockers = static_scan_blockers(package)
        if blockers:
            raise ValueError(
                "rate replay source validation failed: "
                + ",".join(b.code for b in blockers)
            )
        revision = source_revision(files)
        out = create_run_dir(package.root, kind="rates")
        tracker.bind(out, strategy_id=package.strategy_id, proposal_id=proposal_id)
        tracker.phase("preparing_data")
        prepared = prepare_inputs(package.root, cancel_token=cancel_token, config=runtime_config or config)
        if validate_prepared_inputs:
            validate_prepared_inputs(prepared)
        policy = package.manifest.extras.get("params", {}).get("rate_replay", {})
        name = policy.get("dataset_id")
        if not isinstance(name, str) or set(prepared["files"]) != {name + ".json"}:
            raise ValueError("rate replay requires exactly its declared dataset")
        data = json.loads(prepared["files"][name + ".json"])
        if (
            len(prepared["receipts"]) != 1
            or prepared["receipts"][0]["sha256"]
            != hashlib.sha256(prepared["files"][name + ".json"]).hexdigest()
        ):
            raise ValueError("rate replay input receipt mismatch")
        current, skipped = _read_files(package.root)
        if skipped or current != files:
            raise ValueError("rate replay source changed during data preparation")
        (out / "prepared_inputs").mkdir()
        for filename, blob in prepared["files"].items():
            (out / "prepared_inputs" / filename).write_bytes(blob)
        atomic_json(out / "data_manifest.json", {"datasets": prepared["receipts"]})
        tracker.phase("replaying")
        simulation = simulate_rates(
            package,
            files,
            data,
            cancel_token=cancel_token,
            check_cancel=tracker.check_cancel,
        )
        current, skipped = _read_files(package.root)
        if skipped or current != files:
            raise ValueError("rate replay source changed during execution")
        tracker.phase("writing_artifacts")
        for filename, rows in [
            ("equity.csv", simulation["equity"]),
            ("trades.csv", simulation["trades"]),
            ("decisions.csv", simulation["decisions"]),
        ]:
            _write_rows(
                out / filename,
                rows or [{"ts": "", "side": "", "size": "", "price": ""}],
            )
        # The CSV helper requires row fields; an empty trade list is header-only.
        if not simulation["trades"]:
            (out / "trades.csv").write_text("ts,side,size,price\n", encoding="utf-8")
        metrics = _build_metrics(
            payload={},
            equity_rows=simulation["equity"],
            trade_rows=simulation["trades"],
            strategy_id=package.strategy_id,
            proposal_id=proposal_id,
            script_path=package.main_path,
        )
        initial = (
            simulation["assumptions"]["initial_cash_units"]
            * simulation["assumptions"]["unit_price_usd"]
        )
        final = simulation["equity"][-1]["equity"]
        window = data["_envelope"]["requested_window"]
        coverage = simulation["requested_window_complete"]
        limitations = [
            "APY observations with previous-rate constant-interval approximation, not Aave reserve-index reconstruction.",
            "Simulated instant supply/withdraw and constant unit price; no chain fills, gas, depeg, latency or settlement proof.",
            "Current catalog metadata identifies the pool; historical identity changes are not independently attested.",
        ]
        limitation_codes = [
            "apy_interval_approximation",
            "simulated_allocation_constant_price",
            "historical_pool_identity_unattested",
        ]
        v2 = simulation['accrual_basis'] == 'aave_normalized_income'
        accounting = {key: simulation[key] for key in ('accrual_basis', 'rate_metric', 'input_scope')}
        if v2:
            limitations[0] = (
                'Counterfactual normalized-income index accounting over historical reserve observations; '
                'not actual realized yield, strategy ownership or historical performance evidence.'
            )
            limitations.append(
                'Canonical floor-block state is assumed observable instantaneously; confirmation latency '
                'is ignored and actual point-in-time availability remains unverified. Complete grid '
                'and source proofs establish historical simulation inputs only.'
            )
            limitation_codes[0] = 'normalized_income_counterfactual'
            limitation_codes.append('instantaneous_observation_assumption')
        if not coverage:
            limitation_codes.append("observation_coverage_gap")
            limitations.append(
                "Requested window has unobserved intervals or source gaps; no rate accrual was invented for those intervals."
            )
        provenance = {
            **accounting,
            "version": 1,
            "strategy_id": package.strategy_id,
            "proposal_id": proposal_id,
            "source_revision": revision,
            "source_changed_during_run": False,
            "runtime_build_id": BUILD_ID,
            "sdk_build_id": SDK_BUILD_ID,
            "data_kind": "historical_observations",
            "datasets": prepared["receipts"],
            "assumptions": simulation["assumptions"],
            "isolation": simulation["isolation"],
            "scope": "historical_rate_policy_simulation",
            "modeled_coverage": simulation["modeled_coverage"],
            "observation_coverage": simulation["observation_coverage"],
        }
        if v2:
            provenance['input_provenance'] = {key: data['_envelope'].get(key) for key in (
                'official_identity', 'requested_window', 'observation_interval_seconds',
                'pool_identity_scope', 'canonicality', 'simulation_assumption',
                'point_in_time_ready', 'availability_scope', 'sources', 'point_sources',
            )}
        replay = {
            **accounting,
            "scope": "historical_rate_policy_simulation",
            "strategy_execution_verified": True,
            "order_accounting_ok": True,
            "decisions": len(simulation["decisions"]),
            "simulated_trades": len(simulation["trades"]),
            "errors": 0,
            "sdk_errors": 0,
            "financial_execution_allowed": False,
            "point_in_time_ready": False,
        }
        metrics.update(
            **accounting,
            backtest_kind="rate_policy_replay",
            execution_mode="script",
            evaluation_mode="simulation",
            performance_evidence=False,
            financial_execution_allowed=False,
            point_in_time_ready=False,
            initial_capital_usd=initial,
            final_equity_usd=final,
            total_return_usd=final - initial,
            total_return_pct=(final / initial - 1) * 100,
            requested_window_complete=coverage,
            coverage_ok=coverage,
            requested_start_utc=window["start"],
            requested_end_utc=window["end"],
            requested_end_inclusive=True,
            requested_window_days=(_stamp(window["end"]) - _stamp(window["start"]))
            / 86400,
            coverage_message=(
                "Observed normalized-income grid intervals accounted under counterfactual simulation assumptions; availability and performance remain unverified."
                if v2 else
                "Modeled interval coverage complete under source-bracketed previous-rate assumptions; raw observation completeness is unchanged.")
            if coverage
            else "Historical observation gaps remain; partial simulation only.",
            limitations=limitations,
            limitation_codes=limitation_codes,
            coverage_reason="modeled_intervals_covered"
            if coverage
            else "observation_coverage_gap",
            provenance=provenance,
            replay=replay,
            gaps=simulation["gaps"],
            modeled_coverage=simulation["modeled_coverage"],
            observation_coverage=simulation["observation_coverage"],
            leading_gap_seconds=simulation["leading_gap_seconds"],
            trailing_gap_seconds=simulation["trailing_gap_seconds"],
        )
        metrics.pop("script_path", None)
        atomic_json(out / "metrics.json", metrics)
        boundaries = data["_envelope"].get("boundary_observations") or {}
        before, after = boundaries.get("before"), boundaries.get("after")
        max_gap = simulation["assumptions"]["max_gap_seconds"]
        modeled_leading = 0 if before and _stamp(data["rows"][0]["timestamp"]) - _stamp(before["timestamp"]) <= max_gap else simulation["leading_gap_seconds"]
        modeled_trailing = 0 if after and _stamp(after["timestamp"]) - _stamp(data["rows"][-1]["timestamp"]) <= max_gap else simulation["trailing_gap_seconds"]
        report = [
            "# Historical rate-policy simulation",
            "",
            f"Strategy: {package.strategy_id}",
            f"Requested window: {window['start']} to {window['end']}",
            f"Observed in-window range: {data['rows'][0]['timestamp']} to {data['rows'][-1]['timestamp']}",
            f"Modeled equity range (may include source-bracketed boundary-supported endpoints): {metrics['start_utc']} to {metrics['end_utc']}",
            f"Accrual basis: {simulation['accrual_basis']}; rate metric: {simulation['rate_metric']}",
            f"Input scope: {simulation['input_scope']}",
            f"Decisions: {len(simulation['decisions'])}; simulated actions: {len(simulation['trades'])}",
            f"Initial modeled value: {initial:.6f} USD; final: {final:.6f} USD",
            "",
            "The saved strategy entrypoint ran only for in-window observations in an isolated process; boundary observations create no decisions.",
            "The host, not the candidate script, owns the simulated accounting.",
            f"Modeled interval coverage: {json.dumps(simulation['modeled_coverage'], sort_keys=True)}",
            f"Uncovered modeled boundary offsets (seconds): leading={modeled_leading}; trailing={modeled_trailing}",
            f"Excluded modeled intervals (source-bracket start/end/seconds; max_gap_seconds={simulation['assumptions']['max_gap_seconds']}): {json.dumps(simulation['gaps'], sort_keys=True)}",
            "Only the requested-window overlap of these excluded source brackets is omitted from accrual.",
            "",
            "## Raw acquisition summary",
            "Raw observation edge gaps and cadence diagnostics are acquisition facts, not additional excluded modeled intervals; boundary support and the configured threshold determine modeled coverage above.",
            f"Raw in-window observation boundary offsets (seconds): leading={simulation['leading_gap_seconds']}; trailing={simulation['trailing_gap_seconds']}",
            f"Raw observation coverage (unchanged): {json.dumps(simulation['observation_coverage'], sort_keys=True)}",
            "",
            "## Limitations",
            *["- " + item for item in limitations],
        ]
        (out / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
        chart = _render_chart(
            out_dir=out,
            strategy_id=package.strategy_id,
            backtest_ts=out.name,
            equity_rows=simulation["equity"],
            trade_rows=simulation["trades"],
            metrics=metrics,
            workspace_root=config.paths.root,
        )
        chart["meta"].update(
            **accounting,
            metrics=metrics,
            kind="rate_policy_replay",
            execution_mode="script",
            limitations=limitations,
            limitation_codes=limitation_codes,
            coverage_reason=metrics["coverage_reason"],
            gaps=simulation["gaps"],
            leading_gap_seconds=simulation["leading_gap_seconds"],
            trailing_gap_seconds=simulation["trailing_gap_seconds"],
            requested_window_days=metrics["requested_window_days"],
        )
        chart["panels"][0]["title"] = "Modeled rate allocation value"
        chart["tables"] = [
            _table("trades", simulation["trades"]),
            _table("decisions", simulation["decisions"]),
        ]
        chart.pop(
            "chart_blocks", None
        )  # Existing freeform block labels do not describe this host model.
        atomic_json(out / "chart.json", chart)
        result = {
            "next_required_action": {
                "action": "read_saved_rate_report",
                "operation_completed": True,
                "repeat_unchanged_operation": False,
                "coverage_status": "complete" if coverage else "partial",
                "message": "Read the saved report and preserve producer coverage checks. "
                "Repeated validation or the same replay cannot fill unobserved intervals. "
                "Finalize once, report limitations and the missing data; never author a passing coverage check.",
            },
            **metrics,
            "metrics": metrics,
            "equity_preview": [
                {"time": row["ts"], "value": row["equity"]}
                for row in simulation["equity"]
            ],
            "ok": True,
            "result_type": "backtest_result",
            "backtest_status": "completed",
            "strategy_id": package.strategy_id,
            "proposal_id": proposal_id,
            "backtest_ts": out.name,
            "engine": "rates",
            "kind": "rate_policy_replay",
            "run_receipt": to_workspace_relative(out / "run.json", config.paths.root),
            "report_path": str((out / "report.md").relative_to(config.paths.root)),
            "metrics_path": str(out / "metrics.json"),
            "chart_path": str(out / "chart.json"),
            "equity_path": str(out / "equity.csv"),
            "trades_path": str(out / "trades.csv"),
            "out_dir": str(out),
            "isolation": simulation["isolation"],
        }
        atomic_json(out / "result.json", result)
        tracker.complete(source_revision=revision, performance_evidence=False)
        return result
    except BaseException as exc:
        from .....strategies.request_scope import ScopeError, _safe_differences

        if isinstance(exc, ScopeError):
            exc.receipt = {
                'reason': exc.reason, 'differences': _safe_differences(exc),
                'diagnostic_code': exc.diagnostic_code,
                'diagnostic_metadata': exc.diagnostic_metadata,
            }
        tracker.fail(exc)
        raise
