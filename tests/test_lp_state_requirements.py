"""One offline saved-cadence/archived-checkpoint acceptance case."""
import json
import csv
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nerya.core import yaml_io
from nerya.core.paths import WorkspacePaths
from nerya.data_api.uniswap_events import UniswapEventProvider
from nerya.skills.builtin.backtest.scripts.lp_state_requirements import (
    inspect_lp_state_requirements, download_lp_states, prepare_lp_cadence, LPDataIncomplete)
from nerya.skills.builtin.backtest.scripts.uniswap_event_data import event_history_operation
from test_uniswap_history_data import Fake, CatalogFake, POOL, TOKEN0, TOKEN1


def test_disabled_five_minute_cadence_requires_exact_floor_checkpoint(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("network forbidden")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)
    from nerya.data_api.uniswap_history import UniswapHistoryProvider
    catalog = CatalogFake()
    catalog.doc["tokens"][1]["symbol"] = "WETH"
    read_catalog = UniswapHistoryProvider._token_catalog
    def offline_catalog(self, pool, deadline, cancel_token=None):
        self.catalog_http = catalog
        return read_catalog(self, pool, deadline, cancel_token=cancel_token)
    monkeypatch.setattr(UniswapHistoryProvider, "_token_catalog", offline_catalog)
    config = SimpleNamespace(paths=WorkspacePaths(tmp_path))
    root = tmp_path / "strategies" / "lp_fixture"
    root.mkdir(parents=True)
    source = dict(chain="Ethereum", chain_id=1, protocol="uniswap_v3", pool_id=POOL,
                  token0=TOKEN0, token1=TOKEN1, decimals=[6, 18], fee=3000,
                  tick_spacing=60, tick_lower=-120, tick_upper=120, source_id="lp_fixture", token_id=None)
    manifest = dict(id="lp_fixture", mode="paper", entrypoint="main.py:run", timeframe="1h",
        schedule=dict(type="cron", cron="*/5 * * * *", enabled=False),
        params=dict(lp_source=source, lp_policy=None, lp_replay=dict(version=1, dataset_id="lp_history",
            initial_token0_units="1000", initial_token1_units="1", action_cost_token1="0.0001", max_gap_seconds=90000)))
    yaml_io.dump(root / "strategy.yml", manifest)
    (root / "main.py").write_text(
        "def run(ctx):\n"
        "    snapshot = ctx.inputs.source(ctx.config.params['lp_source']['source_id'])\n"
        "    action = 'hold' if snapshot['account']['positions'] else 'lp_add'\n"
        "    return ctx.result.hold(reason='fixture fixed range', metadata={'lp_decision':\n"
        "        {'action': action, 'reason': 'fixture fixed range'}})\n")
    template = Path(__file__).parents[1] / "nerya/skills/builtin/strategy_author/templates/lp_sampled_replay.py.j2"
    (root / "scripts").mkdir()
    (root / "scripts/custom_replay.py").write_bytes(template.read_bytes())
    (root / "replay_inputs.json").write_text(json.dumps({"version": 1, "datasets": [{
        "id": "lp_history", "provider": "uniswap", "action": "pool_history",
        "args": dict(pool_address=POOL, tick_lower=-120, tick_upper=120,
                     start="2026-10-01", end="2026-10-31", step_blocks=4000, max_samples=64)}]}))
    start = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())

    def mutate(req, doc, transport):
        if req["method"] == "eth_getBlockByNumber":
            block = int(req["params"][0], 16)
            offset = (block - 99) * 12 - 2 if block < 109 else 300 + block - 109
            doc["result"].update(timestamp=hex(start + offset), parentHash="0x" + format(block, "064x"))
        return 200, doc

    class Rpc(Fake):
        def request(self, method, url, **kwargs):
            req = kwargs["body"]
            if isinstance(req, list):
                return super().request(method, url, **kwargs)
            if req["method"] == "eth_getLogs":
                return 200, {"jsonrpc": "2.0", "id": req["id"], "result": []}
            return super().request(method, url, **kwargs)

    producer = UniswapEventProvider(Rpc(mutate=mutate))
    for first in (100, 200):
        event_history_operation(config, dict(action="download", pool_address=POOL, rpc_provider="publicnode",
            start_block=first, end_block=first + 9, chunk_blocks=10, max_chunks=1,
            tick_lower=-120, tick_upper=120), provider=producer)
    # An old sampled result at the missing decision must never be admitted.
    (root / "sampled.json").write_text(json.dumps({"rows": [{"timestamp": start + 300}],
        "_envelope": {"provider": "uniswap", "action": "pool_history", "sampled_states_complete": True}}))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    args = dict(action="inspect", dataset_kind="lp_state_requirements", strategy_id="lp_fixture")
    result = inspect_lp_state_requirements(config, args)
    assert result["result_type"] == "historical_data"
    assert result["decision_count"] == 8640 and result["required_state_count"] == 8641
    assert result["matched_state_count"] == 1 and result["missing_state_count"] == 8640
    assert result["interval_count"] == result["pending_state_interval_count"] == 8640
    assert result["verified_interval_count"] == result["missing_interval_count"] == 0
    assert result["next_interval_download_args"] is None
    assert result["matched_decision_count"] == 1 and result["missing_decision_count"] == 8639
    assert result["boundaries"] == {
        "opening": {"timestamp": "2026-10-01T00:00:00Z", "matched": True, "overlaps_decision": True},
        "terminal": {"timestamp": "2026-10-31T00:00:00Z", "matched": False, "overlaps_decision": False}}
    assert result["schedule"] == manifest["schedule"] and result["timezone"] == "UTC"
    assert result["archives_inspected"] == result["chunks_inspected"] == 2
    assert len(result["source_revision"]) == 64 and len(result["preview"]) == 10
    first, second = result["preview"][:2]
    assert first["matched"] and first["block_number"] == 99
    assert first["block_timestamp"] <= start < first["next_block_timestamp"]
    assert second["timestamp"] == "2026-10-01T00:05:00Z" and second["matched"] is False
    assert all(result[k] is False for k in ("coverage_complete", "requested_window_complete", "replay_input",
        "tick_fee_reconstruction_verified", "performance_evidence", "financial_execution_allowed", "range_continuity_verified"))
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    # Explicit sampled mode has no cron plan, including through acquisition callers.
    from nerya.skills.builtin.backtest.scripts import lp_state_job, lp_state_requirements
    from nerya.tools.native.historical_data import historical_data_handler
    from nerya.tools.types import ToolCall

    manifest["params"]["lp_replay"]["mode"] = "sampled"
    yaml_io.dump(root / "strategy.yml", manifest)
    sampled_before = {p: p.read_bytes() for p in tmp_path.rglob("*")
                      if p.is_file() and p.parent != config.paths.state / "trading_locks"}
    with monkeypatch.context() as patch:
        def no_cadence_work(*args, **kwargs):
            pytest.fail("sampled mode must stop before cadence planning or job launch")
        patch.setattr(lp_state_requirements, "_times", no_cadence_work)
        patch.setattr(lp_state_job, "_launch", no_cadence_work)
        for operation in (
            lambda: inspect_lp_state_requirements(config, args),
            lambda: prepare_lp_cadence(config, args),
            lambda: download_lp_states(config, result["next_download_args"]),
            lambda: lp_state_job.operation(config, dict(action="start", strategy_id="lp_fixture",
                request_id="sampled-mode-regression", expected_source_revision=result["source_revision"],
                max_states_total=1, timeout_seconds=30), session_id="fixture", actor_id="fixture"),
        ):
            with pytest.raises(ValueError, match="lp_state_requirements_mode_mismatch"):
                operation()
        rejected = historical_data_handler(ToolCall(name="historical_data", arguments=args), config=config)
        error = rejected.content[0].data
        assert rejected.is_error and rejected.semantic_success is False
        assert error["ok"] is False and "lp_state_requirements_mode_mismatch" in error["message"]
        assert "Preserve the existing sampled report" in error["message"]
        assert not {"required_state_count", "decision_count", "next_download_args",
                    "next_interval_download_args"} & error.keys()
    assert sampled_before == {p: p.read_bytes() for p in tmp_path.rglob("*")
                              if p.is_file() and p.parent != config.paths.state / "trading_locks"}
    manifest["params"]["lp_replay"]["mode"] = "cadence"
    yaml_io.dump(root / "strategy.yml", manifest)
    cadence = inspect_lp_state_requirements(config, args)
    assert all(cadence[k] == result[k] for k in ("decision_count", "required_state_count",
        "matched_state_count", "missing_state_count", "boundaries", "schedule", "window"))
    assert cadence["next_download_args"]["expected_next_timestamp"] == result["next_download_args"]["expected_next_timestamp"]
    manifest["params"]["lp_replay"].pop("mode")
    yaml_io.dump(root / "strategy.yml", manifest)
    with pytest.raises(LPDataIncomplete) as incomplete:
        prepare_lp_cadence(config, args)
    assert incomplete.value.receipt["missing_state_count"] == 8640
    assert incomplete.value.receipt["ready"] is False
    with pytest.raises(ValueError, match="invalid_requirements_args"):
        inspect_lp_state_requirements(config, {**args, "start": "2026-10-02"})
    inputs_path = root / "replay_inputs.json"
    inputs = json.loads(inputs_path.read_text())
    inputs["datasets"][0]["args"]["start"] = "2026-10-01T00:00:01Z"
    inputs_path.write_text(json.dumps(inputs))
    offset = inspect_lp_state_requirements(config, args)
    assert offset["decision_count"] == 8639 and offset["required_state_count"] == 8641
    assert offset["matched_decision_count"] == 0 and offset["missing_decision_count"] == 8639
    assert offset["boundaries"]["opening"] == {"timestamp": "2026-10-01T00:00:01Z",
                                                "matched": True, "overlaps_decision": False}
    assert offset["boundaries"]["terminal"] == result["boundaries"]["terminal"]
    assert offset["preview"][0]["kind"] == "opening" and offset["preview"][0]["matched"] is True
    assert offset["preview"][0]["timestamp"] == "2026-10-01T00:00:01Z"
    assert offset["preview"][1]["timestamp"] == "2026-10-01T00:05:00Z"
    # Fetching is bounded and commits each state before a later interruption.
    from nerya.harness.cancellation import CancelledError
    requests, anchors = [], []

    class AtUtcProvider:
        interrupt = True

        def pool_event_slice(self, context, request, *, anchor=None):
            requests.append(request["at_utc"])
            anchors.append(anchor)
            if self.interrupt and len(requests) == 2:
                raise CancelledError("fixture interruption")
            stamp = int(datetime.fromisoformat(request["at_utc"].replace("Z", "+00:00")).timestamp())
            block = 110 + stamp - (start + 300)
            return producer.pool_event_slice(context, {
                **{k: v for k, v in request.items() if k != "at_utc"},
                "start_block": block, "end_block": block})

    fetcher = AtUtcProvider()
    first_download = {**offset["next_download_args"], "max_states": 2}
    with pytest.raises(ValueError, match="state_download_source_changed"):
        download_lp_states(config, {**first_download, "expected_source_revision": "0" * 64}, provider=fetcher)
    assert requests == []
    with pytest.raises(CancelledError):
        download_lp_states(config, first_download, provider=fetcher)
    resumed = inspect_lp_state_requirements(config, args)
    assert resumed["matched_state_count"] == 2 and resumed["missing_decision_count"] == 8638
    stale = download_lp_states(config, first_download, provider=fetcher)
    assert stale["downloaded_state_count"] == 0 and len(requests) == 2
    fetcher.interrupt = False
    inspections = []
    def inspect_once(*args, **kwargs):
        inspections.append(1)
        return inspect_lp_state_requirements(*args, **kwargs)
    monkeypatch.setattr(lp_state_requirements, "inspect_lp_state_requirements", inspect_once)
    finished = download_lp_states(config, resumed["next_download_args"], provider=fetcher)
    assert len(inspections) == 2  # Read progress before acquisition and after its archive commit.
    assert finished["downloaded_state_count"] == 1 and finished["matched_state_count"] == 3
    assert finished["missing_decision_count"] == 8637
    assert all(finished[k] is False for k in ("replay_input", "coverage_complete", "range_continuity_verified"))
    assert requests == ["2026-10-01T00:05:00Z", "2026-10-01T00:10:00Z", "2026-10-01T00:10:00Z"]
    assert [a["block_number"] for a in anchors] == [99, 109, 109]
    assert anchors[0]["block_timestamp"] < start < anchors[0]["next_block_timestamp"]
    assert finished["verified_interval_count"] == finished["missing_interval_count"] == 1
    assert finished["pending_state_interval_count"] == 8638 and len(finished["interval_preview"]) == 10
    bridge = finished["next_interval_download_args"]
    assert bridge == dict(action="download", dataset_kind="uniswap_pool_events", pool_address=POOL,
        rpc_provider="publicnode", tick_lower=-120, tick_upper=120, start_block=110, end_block=409,
        chunk_blocks=100, max_chunks=1, expected_next_block=110)
    # A saved optimistic flag cannot certify a boundary cleared and reinitialized in the logs.
    from eth_abi import encode
    from eth_utils import keccak
    from nerya.data_api.uniswap_history import _hash

    class ResetBridge:
        def pool_event_slice(self, context, request):
            output = producer.pool_event_slice(context, request)
            if request["start_block"] == 110:
                env, rows = output["_envelope"], output["rows"]
                for index, name in enumerate(("Burn", "Mint")):
                    signature = ("Burn(address,int24,int24,uint128,uint256,uint256)" if name == "Burn" else
                                 "Mint(address,address,int24,int24,uint128,uint256,uint256)")
                    rows.append(dict(block_number=111, block_hash=env["headers"][2]["hash"], transaction_index=index,
                        log_index=index, address=POOL, removed=False, topics=["0x" + keccak(text=signature).hex(),
                        "0x" + encode(["address"], [POOL]).hex(), "0x" + encode(["int24"], [-120]).hex(),
                        "0x" + encode(["int24"], [120]).hex()], data="0x" + encode(
                        ["uint128", "uint256", "uint256"] if name == "Burn" else ["address", "uint128", "uint256", "uint256"],
                        [99, 0, 0] if name == "Burn" else [POOL, 99, 0, 0]).hex()))
                env["range_receipt"]["logs"] = len(rows)
                env["rows_sha256"] = _hash(rows)
                assert env["range_continuity"]["verified"] is True
                env.pop("evidence_sha256")
                env["evidence_sha256"] = _hash(output)
            return output

    event_history_operation(config, {**bridge, "max_chunks": 3}, provider=ResetBridge())
    rejected = inspect_lp_state_requirements(config, args)
    assert rejected["verified_interval_count"] == 1 and rejected["missing_interval_count"] == 1
    assert rejected["next_interval_download_args"] is None  # Fully fetched but invalid; no blind retry.
    inputs["datasets"][0]["args"]["end"] = "2026-10-01T00:10:00Z"
    inputs_path.write_text(json.dumps(inputs))
    with pytest.raises(LPDataIncomplete) as reset:
        prepare_lp_cadence(config, args)
    assert reset.value.receipt["missing_state_count"] == 0
    assert reset.value.receipt["missing_interval_count"] == 1
    assert reset.value.receipt["reasons"] == ["missing_interval_count"]
    from nerya.skills.builtin.backtest.scripts import freeform_run
    executions = []
    with monkeypatch.context() as patch:
        def forbidden_execution(*args, **kwargs):
            executions.append(True)
            pytest.fail("candidate must not run before cadence admission")
        patch.setattr(freeform_run, "execute_freeform", forbidden_execution)
        with pytest.raises(LPDataIncomplete) as blocked:
            freeform_run.run_freeform_backtest(strategy_id="lp_fixture", workspace=tmp_path)
        assert blocked.value.receipt["missing_interval_count"] == 1 and executions == [] and catalog.calls == []
    inputs["datasets"][0]["args"]["end"] = "2026-10-31"
    inputs_path.write_text(json.dumps(inputs))
    event_history_operation(config, {**bridge, "start_block": 111, "expected_next_block": 111,
                                   "max_chunks": 3}, provider=producer)
    bridged = inspect_lp_state_requirements(config, args)
    assert bridged["verified_interval_count"] == 2 and bridged["missing_interval_count"] == 0
    assert bridged["pending_state_interval_count"] == 8638 and bridged["range_continuity_verified"] is False
    inputs["datasets"][0]["args"]["end"] = "2026-10-01T00:10:00Z"
    inputs["datasets"][0]["args"]["start"] = "2026-10-01"
    inputs_path.write_text(json.dumps(inputs))
    complete = inspect_lp_state_requirements(config, args)
    assert complete["interval_count"] == complete["verified_interval_count"] == 2
    assert complete["range_continuity_verified"] is True
    assert all(complete[k] is False for k in ("replay_input", "coverage_complete", "requested_window_complete"))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    prepared = prepare_lp_cadence(config, args)
    assert prepared["protocol"] == "nerya.lp_cadence.v1" and prepared["source_revision"] == complete["source_revision"]
    assert prepared["decision_timestamps"] == [start, start + 300]
    assert [r["timestamp"] for r in prepared["observations"]] == [start, start + 300, start + 600]
    assert [r["decision"] for r in prepared["observations"]] == [True, True, False]
    assert len(prepared["intervals"]) == 2 and all(r["verified"] for r in prepared["intervals"])
    for observation in prepared["observations"]:
        assert _hash(observation["checkpoint"]) == observation["floor_proof"]["checkpoint_sha256"]
    for interval, a, b in zip(prepared["intervals"], prepared["observations"], prepared["observations"][1:]):
        assert interval["start_checkpoint_sha256"] == _hash(a["checkpoint"])
        assert interval["end_checkpoint_sha256"] == _hash(b["checkpoint"])
    assert prepared["evidence"] and all((tmp_path / ref["archive"]).is_file() for ref in prepared["evidence"])
    assert all(len(ref["evidence_sha256"]) == 64 for ref in prepared["evidence"])
    assert "raw_log" not in json.dumps(prepared)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    # Full cadence claims need a locally rebuilt compact receipt, not just booleans.
    import hashlib
    from copy import deepcopy
    from nerya.core.config import Config
    from nerya.skills.builtin.backtest.scripts.lp_replay import cadence_receipt
    from nerya.strategies.tool_verification import attach_strategy_checks, source_binding
    from nerya.tools.types import ToolCall, ToolResult

    config = Config(config.paths, {})
    # Formal Seatbelt replay: off-cadence opening/terminal never invoke the candidate.
    inputs["datasets"][0]["args"]["start"] = "2026-10-01T00:00:01Z"
    inputs_path.write_text(json.dumps(inputs))
    binding = source_binding(config, ToolCall(name="strategy_backtest", arguments={"strategy_id": "lp_fixture"}))
    ready = prepare_lp_cadence(config, args)
    result = freeform_run.run_freeform_backtest(strategy_id="lp_fixture", workspace=tmp_path, timeout_seconds=30)
    assert len(catalog.calls) == 1
    assert result["isolation"]["backend"] == "macos-seatbelt"
    assert result["isolation"]["network"] == "denied"
    assert result["isolation"]["process_exited"] and result["isolation"]["process_group_stopped"]
    assert result["coverage_ok"] is result["requested_window_complete"] is True
    assert result["performance_evidence"] is False and result["accounting_currency"] == "token1:" + TOKEN1
    assert result["replay"]["decision_count"] == 1 and result["replay"]["action_counts"]["lp_add"] == 1
    real_run = Path(result["out_dir"])
    with (real_run / "decisions.csv").open() as stream:
        decisions = list(csv.DictReader(stream))
    with (real_run / "equity.csv").open() as stream:
        curve = list(csv.DictReader(stream))
    assert len(decisions) == 1 and float(decisions[0]["ts"]) == start + 300
    assert [float(row["ts"]) for row in curve] == [start + 1, start + 300, start + 600]
    assert (tmp_path / result["report_path"]).is_file()
    assert json.loads(Path(result["run_receipt"]).read_text())["status"] == "completed"
    saved_receipt = json.loads((real_run / "prepared_inputs/lp_history.json").read_text())
    assert {k: v for k, v in saved_receipt.items() if k != "_envelope"} == cadence_receipt(ready)
    assert saved_receipt["_envelope"]["token_identities"][1]["symbol"] == "WETH"
    assert "observations" not in saved_receipt and "checkpoint" not in saved_receipt
    # Explicitly synthetic consumer case: a decision and terminal share one exact checkpoint.
    from nerya.skills.builtin.backtest.scripts.lp_replay import simulate_lp_policy
    same = deepcopy(ready)
    same["observations"][-1] = deepcopy(same["observations"][1])
    same["observations"][-1].update(timestamp=start + 301, decision=False)
    same["requested_window"]["end"] = "2026-10-01T00:05:01Z"
    for row in same["observations"][1:]:
        row["floor_proof"]["next_block_timestamp"] = start + 302
    same["intervals"][-1].update(end=start + 301, end_checkpoint_sha256=same["intervals"][-1]["start_checkpoint_sha256"])
    calls = []
    def synthetic_policy(ctx):
        calls.append(ctx.clock.now_ms() / 1000)
        return ctx.result.hold(reason="synthetic", metadata={"lp_decision": {"action": "lp_add", "reason": "synthetic"}})
    modeled = simulate_lp_policy(synthetic_policy, manifest, {}, trusted_cadence=same)
    assert calls == [start + 300] and len(modeled["modeled_curve"]) == 3
    assert modeled["modeled_curve"][-1]["block_number"] == modeled["modeled_curve"][-2]["block_number"]
    assert modeled["modeled_curve"][-1]["owed_base"] == modeled["modeled_curve"][-2]["owed_base"]
    metrics_path = Path(result["metrics_path"])
    metrics_bytes = metrics_path.read_bytes()
    def checks(value):
        metrics_path.write_text(json.dumps({**json.loads(metrics_bytes),
            "provenance": value["provenance"], "replay": value["replay"]}))
        verified = attach_strategy_checks(ToolResult.from_json(tool_use_id="real-cadence",
            name="strategy_backtest", data=value), before=binding,
            after=source_binding(config, ToolCall(name="strategy_backtest", arguments={"strategy_id": "lp_fixture"})),
            kind="backtest", config=config)
        return {c["check_id"]: c["status"] for c in verified.metadata["verification"]["checks"]}
    verdicts = checks(result)
    assert verdicts["historical_data"] == verdicts["data_coverage"] == "passed"
    assert verdicts["historical_performance"] == "missing"
    # The compact comparison excludes catalog metadata, but its byte hash does not.
    real_snapshot = real_run / "prepared_inputs/lp_history.json"
    original = real_snapshot.read_bytes()
    tampered = deepcopy(saved_receipt)
    tampered["_envelope"]["token_identities"][0]["symbol"] = "CHANGED"
    real_snapshot.write_text(json.dumps(tampered))
    verdicts = checks(result)
    assert verdicts["historical_data"] == verdicts["data_coverage"] == "missing"
    tampered = deepcopy(saved_receipt)
    tampered["schedule"]["cron"] = "* * * * *"
    real_snapshot.write_text(json.dumps(tampered))
    forged = deepcopy(result)
    forged["provenance"]["datasets"][0]["sha256"] = hashlib.sha256(real_snapshot.read_bytes()).hexdigest()
    forged["replay"]["prepared_input_sha256"] = forged["provenance"]["datasets"][0]["sha256"]
    rejected = checks(forged)
    assert rejected["historical_data"] == rejected["data_coverage"] == "missing"
    real_snapshot.write_bytes(original)
    forged = deepcopy(result)
    forged["replay"]["decision_count"] = 2
    rejected = checks(forged)
    assert rejected["historical_data"] == rejected["data_coverage"] == "missing"
    metrics_path.write_bytes(metrics_bytes)
    manifest["schedule"] = dict(type="interval", every_seconds=300, enabled=False)
    yaml_io.dump(root / "strategy.yml", manifest)
    rejected = checks(result)
    assert rejected["historical_data"] == rejected["data_coverage"] == "missing"
    with pytest.raises(ValueError, match="interval_anchor_unavailable"):
        inspect_lp_state_requirements(config, args)
