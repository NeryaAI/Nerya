"""Pure sampled LP consumer; call only inside the existing freeform sandbox.

The caller supplies research.entrypoint(), saved manifest and immutable prepared
data. This module never loads candidate code or fetches data. Decisions belong
to the saved callable; counterfactual balances belong to lp_accounting. v1 has
no swaps, NFT ownership, USD conversions or forward fee projections.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from types import SimpleNamespace

from .....core.redaction import redact_public_record
from .....strategies.config_view import StrategyConfig
from .....strategies.context import StrategyClock
from .....strategies.input_context import StrategyInputContext
from .....strategies.result import ResultBuilder, StrategyResult
from .lp_accounting import ConcentratedLiquidityLedger


def _json(value, limit=4_000_000):
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()
    if len(blob) > limit:
        raise ValueError("LP replay JSON exceeds bounds")
    return blob


class _State:
    """Bounded run-local JSON state; no hooks beyond get/set."""

    def __init__(self):
        self._values = {}

    @staticmethod
    def _key(key):
        if not isinstance(key, str) or not 0 < len(key) <= 200 or key.startswith("__"):
            raise ValueError("LP replay state key rejected")

    def get(self, key, default=None):
        self._key(key)
        return deepcopy(self._values.get(key, default))

    def set(self, key, value):
        self._key(key)
        if key not in self._values and len(self._values) >= 256:
            raise ValueError("LP replay state key limit")
        _json(value, 32_000)
        _json({**self._values, key: value}, 256_000)
        self._values[key] = deepcopy(value)

    def __getattr__(self, name):
        raise ValueError("LP replay unsupported state hook: " + name)


class _Unavailable:
    def __getattr__(self, name):
        raise ValueError("LP replay financial/market methods unavailable in backtest")


def _integer(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError("LP replay integer outside bounds")
    return value


def _uint(value, bits):
    if (not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,78}", value)
            or int(value) >= 2**bits):
        raise ValueError("LP replay raw uint must be a bounded decimal string")
    return int(value)


def _address(value):
    if (not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value)
            or int(value, 16) == 0):
        raise ValueError("LP replay requires exact contract identity")
    return value.lower()


def _stamp(value):
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("LP replay timestamp must be UTC")
        value = parsed.timestamp()
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 253402300799:
        raise ValueError("LP replay timestamp must be UTC epoch seconds")
    if value * 1000 != int(value * 1000):
        raise ValueError("LP replay timestamp exceeds StrategyClock precision")
    return value


def _window_utc(value):
    # Producer grammar, without its fetch-time wall-clock cutoff: replay must
    # validate the saved observation, not the caller's current clock.
    if not isinstance(value, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+]00:00))?", value):
        raise ValueError("LP replay window requires UTC dates or whole seconds")
    return _stamp(datetime.fromisoformat(value.replace("Z", "+00:00")).replace(
        tzinfo=timezone.utc).timestamp())


def _window(envelope, observations):
    """Bind every row to the producer's original request and sampling plan."""
    window, resolution = envelope.get("requested_window"), envelope.get("window_resolution")
    if not isinstance(window, dict) or set(window) not in (
            {"start_block", "end_block", "step_blocks"}, {"start", "end", "step_blocks"}):
        raise ValueError("LP replay requires paired UTC or block requested_window")
    if not isinstance(resolution, dict):
        raise ValueError("LP replay requires producer window_resolution")
    utc = "start" in window
    bounds = resolution if utc else window
    start = _integer(bounds.get("start_block"), 0, 2**64 - 1)
    end = _integer(bounds.get("end_block"), 0, 2**64 - 1)
    step = _integer(window["step_blocks"], 1, 2**64 - 1)
    if start > end:
        raise ValueError("LP replay reversed resolved block window")
    count = (end - start) // step + 1 + bool((end - start) % step)
    if not 2 <= count <= 64:
        raise ValueError("LP replay requested sample count outside bounds")
    blocks = list(range(start, end + 1, step))
    if blocks[-1] != end:
        blocks.append(end)
    sampled = envelope.get("sampled_blocks")
    if (envelope.get("endpoint_policy") != "inclusive_end_once"
            or not isinstance(sampled, list) or any(type(b) is not int for b in sampled)
            or sampled != blocks or [row["block_number"] for _, row in observations] != blocks):
        raise ValueError("LP replay rows do not match requested sampled blocks")
    if not utc:
        if resolution:
            raise ValueError("LP replay block window must have empty window_resolution")
        return observations
    requested_start, requested_end = _window_utc(window["start"]), _window_utc(window["end"])
    if requested_start > requested_end:
        raise ValueError("LP replay reversed UTC window")
    if resolution.get("policy") != "floor_block_timestamp_lte_requested_utc":
        raise ValueError("LP replay requires producer UTC floor window policy")
    if any(stamp <= requested_start or stamp > requested_end for stamp, _ in observations[1:]):
        raise ValueError("LP replay sample contradicts UTC floor window")
    for side, requested, observed in (("start", requested_start, observations[0][0]),
                                      ("end", requested_end, observations[-1][0])):
        _integer(resolution.get(side + "_resolver_reads"), 0, 32)
        stamp = _integer(resolution.get(side + "_timestamp"), 0, 253402300799)
        gap = _integer(resolution.get(side + "_gap_seconds"), 0, 253402300799)
        if (stamp != observed or requested - stamp != gap
                or type(resolution.get("exact_" + side)) is not bool
                or resolution["exact_" + side] != (gap == 0)):
            raise ValueError("LP replay UTC window resolution endpoint mismatch")
    # The floor sample proves the resolved boundary, but is not a tradable
    # observation. Do not publish it, enter there, or accrue fees from it.
    admitted = [(stamp, row) for stamp, row in observations if requested_start <= stamp <= requested_end]
    if not admitted:
        raise ValueError("LP replay UTC window has no admitted observations")
    return admitted


def _configuration(saved):
    _json(saved)
    if not isinstance(saved, dict):
        raise ValueError("LP replay requires saved manifest")
    params = saved.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("LP replay requires manifest params")
    source, replay = params.get("lp_source"), params.get("lp_replay")
    fields = {"version", "dataset_id", "initial_token0_units", "initial_token1_units",
              "action_cost_token1", "max_gap_seconds"}
    if (not isinstance(replay, dict) or set(replay) - {'mode'} != fields
            or replay.get('mode', 'sampled') not in {'sampled', 'cadence'}
            or type(replay["version"]) is not int or replay["version"] != 1
            or not isinstance(replay["dataset_id"], str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", replay["dataset_id"])):
        raise ValueError("LP replay v1 requires explicit allocations, token1 cost and dataset_id")
    gap = replay["max_gap_seconds"]
    if type(gap) not in (int, float) or not math.isfinite(gap) or not 0 < gap <= 366 * 86400:
        raise ValueError("LP replay max_gap_seconds outside bounds")
    if (not isinstance(source, dict) or source.get("chain") not in {"Ethereum", "ethereum", "1"}
            or source.get("protocol") != "uniswap_v3"
            or type(source.get("chain_id", 1)) is not int or source.get("chain_id", 1) != 1
            or not isinstance(source.get("source_id"), str) or not 0 < len(source["source_id"]) <= 200
            or "token_id" not in source or source["token_id"] is not None):
        raise ValueError("LP replay requires Ethereum-1 source and empty counterfactual position")
    identity = {"chain_id": 1, "pool_address": _address(source.get("pool_id")),
                "token0": _address(source.get("token0")), "token1": _address(source.get("token1")),
                "decimals": source.get("decimals"), "fee": source.get("fee"),
                "tick_spacing": source.get("tick_spacing")}
    if (int(identity["token0"], 16) >= int(identity["token1"], 16)
            or _address(source.get("pool_address", source["pool_id"])) != identity["pool_address"]):
        raise ValueError("LP replay pool/token orientation mismatch")
    _integer(identity["fee"], 1, 999999)
    spacing = _integer(identity["tick_spacing"], 1, 32767)
    lower = _integer(source.get("tick_lower"), -887272, 887272)
    upper = _integer(source.get("tick_upper"), -887272, 887272)
    if lower >= upper or lower % spacing or upper % spacing:
        raise ValueError("LP replay fixed range must align to spacing")
    ledger = ConcentratedLiquidityLedger(token0_units=replay["initial_token0_units"],
        token1_units=replay["initial_token1_units"], decimals=identity["decimals"],
        lower=lower, upper=upper, action_cost_token1=replay["action_cost_token1"])
    return source, replay, ledger, identity, lower, upper


def _inputs(saved, data):
    _json(data)
    if not isinstance(data, dict):
        raise ValueError("LP replay requires prepared data")
    source, replay, ledger, identity, lower, upper = _configuration(saved)
    envelope = data.get("_envelope")
    if (not isinstance(envelope, dict) or envelope.get("mock") is not False
            or envelope.get("provider") != "uniswap" or envelope.get("action") != "pool_history"
            or envelope.get("row_contract") != "uniswap_v3_pool_history.v1"
            or type(envelope.get("chain_id")) is not int or envelope["chain_id"] != 1
            or not isinstance(envelope.get("pool"), dict)
            or any(envelope["pool"].get(k) != v for k, v in identity.items())):
        raise ValueError("LP replay prepared provider identity mismatch")
    rows = data.get("rows")
    if not isinstance(rows, list) or not 2 <= len(rows) <= 64:
        raise ValueError("LP replay requires 2 to 64 historical rows")
    digest = hashlib.sha256(_json(rows)).hexdigest()
    if envelope.get("rows_sha256") != digest:
        raise ValueError("LP replay producer rows_sha256 hash missing or mismatched")
    if "returned_rows_sha256" in envelope and envelope["returned_rows_sha256"] != digest:
        raise ValueError("LP replay prepared rows hash mismatch")
    observations, previous = [], None
    for row in rows:
        if (not isinstance(row, dict) or any(row.get(k) != v for k, v in identity.items())
                or type(row.get("chain_id")) is not int
                or type(row.get("tick_lower")) is not int or row["tick_lower"] != lower
                or type(row.get("tick_upper")) is not int or row["tick_upper"] != upper):
            raise ValueError("LP replay row identity/range mismatch")
        block = _integer(row.get("block_number"), 0, 2**64 - 1)
        if (not isinstance(row.get("block_hash"), str)
                or not re.fullmatch(r"0x[0-9a-fA-F]{64}", row["block_hash"])
                or int(row["block_hash"], 16) == 0):
            raise ValueError("LP replay block hash required")
        stamp = _stamp(row.get("timestamp"))
        if previous and (block <= previous[0] or stamp <= previous[1]):
            raise ValueError("LP replay requires increasing blocks and timestamps")
        _integer(row.get("tick"), -887272, 887272)
        sqrt = _uint(row.get("sqrt_price_x96"), 160)
        if not 4295128739 <= sqrt < 1461446703485210103287273052203988822378723970342:
            raise ValueError("LP replay invalid initialized pool price")
        _uint(row.get("liquidity"), 128)
        for prefix in ("fee_growth_global", "tick_lower_fee_growth_outside", "tick_upper_fee_growth_outside"):
            for index in (0, 1):
                _uint(row.get(f"{prefix}{index}_x128"), 256)
        if any(type(row.get(f"tick_{side}_initialized")) is not bool for side in ("lower", "upper")):
            raise ValueError("LP replay requires explicit boundary initialization")
        observations.append((stamp, deepcopy(row)))
        previous = block, stamp
    return source, replay, ledger, _window(envelope, observations)


def cadence_receipt(cadence):
    """Compact durable binding; complete checkpoints remain in verified archives."""
    digest = hashlib.sha256()
    for part in json.JSONEncoder(sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, allow_nan=False).iterencode(cadence):
        digest.update(part.encode())
    return {**{k: cadence[k] for k in ("protocol", "source_revision", "requested_window",
        "schedule", "pool", "tick_lower", "tick_upper", "evidence")},
        "prepared_sha256": digest.hexdigest(), "decision_count": len(cadence["decision_timestamps"]),
        "observation_count": len(cadence["observations"]), "interval_count": len(cadence["intervals"])}


def _cadence_inputs(saved, cadence):
    # Only the host's prepare_lp_cadence result reaches this path, never a
    # candidate's prepared JSON or optimistic completeness/protocol flags.
    source, replay, ledger, identity, lower, upper = _configuration(saved)
    if (cadence["schedule"] != saved["schedule"] or cadence["pool"] != identity
            or cadence["tick_lower"] != lower or cadence["tick_upper"] != upper):
        raise ValueError("LP cadence source binding mismatch")
    observations = []
    for observation in cadence["observations"]:
        cp, proof = observation["checkpoint"], observation["floor_proof"]
        row = {**identity, "timestamp": proof["block_timestamp"],
            **{k: cp[k] for k in ("block_number", "block_hash", "liquidity",
                                  "fee_growth_global0_x128", "fee_growth_global1_x128")},
            **{k: cp["slot0"][k] for k in ("sqrt_price_x96", "tick")},
            "tick_lower": lower, "tick_upper": upper}
        for side in ("lower", "upper"):
            for key in ("initialized", "fee_growth_outside0_x128", "fee_growth_outside1_x128"):
                row[f"tick_{side}_{key}"] = cp["range"][side][key]
        observations.append((observation["timestamp"], row))
    return source, replay, ledger, observations


def _snapshot(source, row, stamp, balances, cost, scope="sampled_fixed_range_counterfactual"):
    common = {"health": "ok", "source": scope, "as_of": stamp}
    pool = {**deepcopy(row), "pool_id": source["pool_id"],
            "underlying_tokens": [source["token0"], source["token1"]]}
    positions = []
    if int(balances["liquidity"]):
        positions = [{"token_id": "counterfactual", "owner": None, "pool": pool,
            "tick_lower": row["tick_lower"], "tick_upper": row["tick_upper"],
            "liquidity": balances["liquidity"], "principal_units": balances["principal_units"],
            "tokens_owed_base": balances["owed_base"],
            "principal_value_token1": balances["principal_value_token1"],
            "owed_value_token1": balances["fees_value_token1"]}]
    return {**common, "schema": "lp.snapshot.v1",
        **{k: deepcopy(source.get(k)) for k in ("wallet_id", "chain", "protocol", "pool_id")},
        "pool": pool, "counterfactual": True, "accounting_currency": "token1",
        "account": {**common, "component_binding": deepcopy(source.get("component_binding")),
            # Empty modeled inventory is complete for this counterfactual,
            # not a claim of real wallet/NFT inventory coverage or ownership.
            "coverage": ["lp_nft_inventory", "liquidity", "ranges", "principal"],
            "fees_coverage": "fee_growth_inside", "positions": positions,
            "counterfactual": True, **deepcopy(balances)},
        "economics": {**common, "pool_id": source["pool_id"], "currency": "USD",
            # Typed unknowns let the original empty-position policy hold.
            # Token1 amounts are never assigned to any *_usd field.
            "expected_incremental_fees_usd": None, "total_execution_cost_usd": None,
            "quote_token": source["token1"], "total_execution_cost_token1": str(cost),
            "expected_incremental_fees_token1": None, "forecast_source": None}}


def simulate_lp_policy(run, saved_manifest, data, *, trusted_cadence=None):
    """Execute the supplied saved entrypoint, account only its LP decisions.

    This pure consumer relies on the caller's existing freeform Seatbelt
    boundary. Only the current sample is published. No future rows or fee
    forecasts are exposed. An explicit forecast in a caller's saved policy
    remains that policy's assumption, never provider-attested economics.
    """
    if not callable(run):
        raise ValueError("LP replay requires the saved callable entrypoint")
    saved = deepcopy(saved_manifest)
    cadence = trusted_cadence
    source, replay, ledger, observations = (_cadence_inputs(saved, cadence) if cadence is not None else _inputs(saved, data))
    scope = "cadence_fixed_range_counterfactual" if cadence is not None else "sampled_fixed_range_counterfactual"
    decision_times = set(cadence["decision_timestamps"]) if cadence is not None else None
    unavailable, state = _Unavailable(), _State()
    config = StrategyConfig(strategy_id=saved.get("id", "lp-replay"), mode="backtest",
        markets=tuple(saved.get("markets") or ()), accounts=tuple(saved.get("accounts") or ()), extras=saved)
    curve, decisions, gaps, actions = [], [], [], []
    action_counts = {"lp_add": 0, "lp_remove": 0, "lp_collect": 0}
    previous, previous_initialized = None, False
    for stamp, row in observations:
        initialized = [row[f"tick_{side}_initialized"] for side in ("lower", "upper")]
        elapsed = 0 if previous is None else stamp - previous
        gap = previous is not None and elapsed > replay["max_gap_seconds"]
        if gap or previous is not None and not previous_initialized:
            # Public ledger baseline field: retain inventory/owed fees while
            # omitting unavailable growth over the missing interval.
            ledger.last_growth = None
        if gap:
            gaps.append({"start": previous, "end": stamp, "seconds": elapsed,
                         "reason": "max_gap_seconds"})
        balances = ledger.snapshot() if row["block_number"] == ledger.last_block else ledger.observe(
            block_number=row["block_number"], sqrt_price_x96=row["sqrt_price_x96"],
            tick=row["tick"], global_growth=[row[f"fee_growth_global{i}_x128"] for i in (0, 1)],
            lower_outside=[row[f"tick_lower_fee_growth_outside{i}_x128"] for i in (0, 1)],
            upper_outside=[row[f"tick_upper_fee_growth_outside{i}_x128"] for i in (0, 1)], initialized=initialized)
        if decision_times is not None and stamp not in decision_times:
            curve.append({"timestamp": stamp, "elapsed_seconds": elapsed, **balances})
            previous, previous_initialized = stamp, all(initialized)
            continue
        clock = StrategyClock()
        iso = datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")
        clock.freeze(iso=iso, ms=int(stamp * 1000))
        snapshot = _snapshot(source, row, stamp, balances, ledger.cost, scope)
        inputs = StrategyInputContext(sources=[], market=unavailable, news=unavailable,
            markets=config.markets, run_id="lp-replay", clock=clock)
        inputs.publish("source:" + source["source_id"], snapshot, data_as_of=iso)
        # The public facade has no typed publish hook. Restore only validated
        # LP identity fields locally; keep redaction for all other payloads.
        public_snapshot = redact_public_record(snapshot, identity_fields=frozenset({
            "pool_id", "pool_address", "token0", "token1", "quote_token",
            "underlying_tokens", "block_hash", "row_sha256", "source_response_sha256",
        }))
        # Ledger-generated exact decimals and validated uints can also resemble
        # long secrets. Restore only these typed numeric fields, not raw input.
        public_snapshot["account"].update(deepcopy(balances))
        public_snapshot["economics"]["total_execution_cost_token1"] = str(ledger.cost)
        pools = [public_snapshot["pool"]]
        for position in public_snapshot["account"]["positions"]:
            pools.append(position["pool"])
            position.update(principal_units=deepcopy(balances["principal_units"]),
                tokens_owed_base=deepcopy(balances["owed_base"]),
                principal_value_token1=balances["principal_value_token1"],
                owed_value_token1=balances["fees_value_token1"])
        for pool in pools:
            numeric_fields = {"liquidity", "sqrt_price_x96"} | {
                f"{prefix}{i}_x128" for prefix in ("fee_growth_global",
                    "tick_lower_fee_growth_outside", "tick_upper_fee_growth_outside") for i in (0, 1)}
            pool.update({key: row[key] for key in numeric_fields})
        with inputs._lock:
            inputs._values["source:" + source["source_id"]]["value"] = public_snapshot
        ctx = SimpleNamespace(strategy_id=config.strategy_id, mode="backtest", runmode="backtest",
            run_id="lp-replay", config=config, clock=clock, inputs=inputs, state=state,
            result=ResultBuilder(), financial=unavailable, trading=unavailable,
            market=unavailable, portfolio=unavailable)
        result = run(ctx)
        if not isinstance(result, StrategyResult):
            raise ValueError("LP replay requires an actual StrategyResult")
        if result.status.value not in {"hold", "ok"}:
            raise ValueError("LP replay saved policy failed: " + str(result.error_kind)
                + "; USD-only position economics/NFT ownership or unsupported state hooks require a compatible saved policy")
        if (result.intent or result.order or result.order_id or result.approval_id
                or result.metadata.get("financial_execution_allowed")
                or result.metadata.get("funds_sent") or result.metadata.get("financial_action")):
            raise ValueError("LP replay candidate claimed financial execution")
        decision = result.metadata.get("lp_decision")
        if not isinstance(decision, dict) or not isinstance(decision.get("reason"), str):
            raise ValueError("LP replay requires result.metadata.lp_decision")
        _json(decision, 32_000)
        action = decision.get("action")
        if not isinstance(action, str):
            raise ValueError("LP replay decision action must be a string")
        if set(decision) - {"action", "reason", "tick_lower", "tick_upper", "pool_id", "token0", "token1"}:
            raise ValueError("LP replay unsupported decision fields")
        for key in ("tick_lower", "tick_upper", "pool_id", "token0", "token1"):
            if key in decision and decision[key] != source[key]:
                raise ValueError("LP replay decision changes fixed range/identity")
        if action in action_counts:
            balances = {"lp_add": ledger.enter, "lp_remove": ledger.remove,
                        "lp_collect": ledger.collect}[action]()
            action_counts[action] += 1
            actions.append({"timestamp": stamp, "block_number": row["block_number"],
                "action": action, "lp_decision": deepcopy(decision),
                "action_cost_token1": str(ledger.cost), "balances": deepcopy(balances)})
        elif action != "hold":
            raise ValueError("LP replay v1 unsupported decision: " + str(action))
        decisions.append({"timestamp": stamp, "block_number": row["block_number"],
                          "block_hash": row["block_hash"], "lp_decision": deepcopy(decision)})
        curve.append({"timestamp": stamp, "elapsed_seconds": elapsed, **balances})
        previous, previous_initialized = stamp, all(initialized)
    entered = action_counts["lp_add"] > 0
    window = cadence["requested_window"] if cadence is not None else data["_envelope"]["requested_window"]
    input_rows = [row for _, row in observations] if cadence is not None else data["rows"]
    leading = observations[0][0] - _window_utc(window["start"]) if "start" in window else 0
    trailing = _window_utc(window["end"]) - observations[-1][0] if "end" in window else 0
    return {"model": "cadence_fixed_range" if cadence is not None else "sampled_fixed_range",
        "scope": scope, "accounting_currency": "token1",
        "quote_token": source["token1"], "dataset_id": replay["dataset_id"],
        # Retain requested offsets verbatim; these are validated metadata,
        # never an attestation of source authenticity or window completeness.
        "requested_window": deepcopy(window),
        "window_resolution": ({"policy": "floor_block_timestamp_lte_saved_schedule"}
                              if cadence is not None else deepcopy(data["_envelope"]["window_resolution"])),
        "observations": [{"timestamp": stamp, "block_timestamp": _stamp(row["timestamp"]),
            "block_number": row["block_number"], "block_hash": row["block_hash"],
            "admitted": row["block_number"] >= observations[0][1]["block_number"]}
            for stamp, row in (observations if cadence is not None else [(_stamp(r["timestamp"]), r) for r in input_rows])],
        "modeled_curve": curve, "decisions": decisions, "gaps": gaps,
        "input_observations": len(input_rows), "admitted_observations": len(observations),
        "excluded_observations": len(input_rows) - len(observations),
        "decision_count": len(decisions), "interval_count": len(observations) - 1,
        "leading_gap_seconds": leading, "trailing_gap_seconds": trailing,
        "actions": actions, "action_counts": action_counts,
        "actions_modeled": len(actions), "allocation_modeled": entered,
        "outcome": "fixed_range_entry_modeled" if entered else "no_action",
        "requested_window_complete": cadence is not None and not gaps, "performance_evidence": False,
        "financial_execution_allowed": False, "funds_sent": False,
        "limitations": ["cadence_fixed_range_counterfactual_only" if cadence is not None else "sampled_fixed_range_counterfactual_only",
            "historical_actual_fee_growth_ignores_dilution_and_price_impact",
            *([] if cadence is not None else ["no_tick_path_between_samples"]), "quote_token_units_not_USD",
            "unknown_USD_economics_no_forward_fee_projection",
            "no_trades_fills_live_or_profit_evidence",
            *([] if entered else ["no_allocation_actual_strategy_use_incomplete"])]}
