"""Offline cancellation/resume contract for the bounded event archive."""
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_abi import encode

from nerya.data_api import uniswap_events as events
from nerya.harness.cancellation import CancelledError, CancelToken
from nerya.skills.builtin.backtest.scripts.uniswap_event_data import event_history_operation
from test_uniswap_history_data import Fake, POOL, digest


@pytest.mark.parametrize("with_range", [False, True])
def test_event_archive_cancel_resume_and_checkpoint_link(tmp_path, monkeypatch, with_range):
    def denied(*args, **kwargs):
        pytest.fail("network forbidden")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)

    def mutate(req, doc, transport):
        if req["method"] == "eth_getBlockByNumber":
            doc["result"]["parentHash"] = "0x" + format(int(req["params"][0], 16), "064x")
        return 200, doc

    class Rpc(Fake):
        def request(self, method, url, **kwargs):
            req = kwargs["body"]
            if isinstance(req, list):
                return super().request(method, url, **kwargs)
            if req["method"] == "eth_getLogs":
                block = int(req["params"][0]["toBlock"], 16)
                log = {"address": POOL, "blockNumber": hex(block),
                    "blockHash": "0x" + format(block + 1, "064x"), "transactionIndex": "0x0",
                    "transactionHash": "0x" + "ab" * 32, "logIndex": "0x0", "removed": False,
                    "topics": [events.SWAP_TOPIC, "0x" + "00" * 32, "0x" + "00" * 32],
                    "data": "0x" + encode(events.SWAP_TYPES, [1, -1, 2**96, 1000, 0]).hex()}
                return 200, {"jsonrpc": "2.0", "id": req["id"], "result": [log]}
            return super().request(method, url, **kwargs)

    producer = events.UniswapEventProvider(Rpc(mutate=mutate, rpc_url=events.RPC_ORIGINS["drpc"]))
    calls, outputs = [], []

    class Provider:
        mismatch = False

        def pool_event_slice(self, context, args):
            calls.append((args["start_block"], args["end_block"]))
            output = producer.pool_event_slice(context, args)
            if self.mismatch:
                env = output["_envelope"]
                env["checkpoints"]["initial"]["liquidity"] = "999"
                del env["evidence_sha256"]
                env["evidence_sha256"] = digest(output)
            outputs.append(deepcopy(output))
            return output

    config = SimpleNamespace(paths=SimpleNamespace(root=tmp_path, artifacts=tmp_path / "artifacts"))
    args = dict(action="download", dataset_kind="uniswap_pool_events", pool_address=POOL,
                rpc_provider="drpc", start_block=100, end_block=304, max_chunks=10)
    if with_range:
        args.update(tick_lower=-120, tick_upper=120)
    provider, token = Provider(), CancelToken()
    def inspect():
        return event_history_operation(config, {**args, "action": "inspect"}, provider=provider)
    assert inspect()["next_block"] == 100
    assert inspect()["phase"] == "incomplete"
    if with_range:
        without_range = {k: v for k, v in args.items() if k not in ("tick_lower", "tick_upper")}
        assert inspect()["request_id"] != event_history_operation(
            config, {**without_range, "action": "inspect"}, provider=provider)["request_id"]
    assert not config.paths.artifacts.exists() and calls == []
    with pytest.raises(ValueError, match="exact_event_request_required"):
        event_history_operation(config, {**args, "markets": ["ETH/USDC"]}, provider=provider)

    def progress(receipt):
        assert receipt["chunks_completed"] == inspect()["chunks_completed"] == 1
        assert receipt["completed"] == 100 and receipt["total"] == 205
        assert receipt["unit"] == "blocks" and receipt["phase"] == "downloading"
        token.cancel()

    with pytest.raises(CancelledError):
        event_history_operation(config, args, progress, token.raise_if_cancelled, provider, token)
    out = inspect()
    assert out["status"] == "incomplete" and out["next_block"] == 200
    assert calls == [(100, 199)] and out["chunks_completed"] == out["rows"] == 1
    assert not Path(out["store_path"]).is_absolute()
    path = config.paths.root / out["store_path"]
    first_bytes = path.read_bytes()
    assert inspect()["stored_bounds"] == {"start_block": 100, "end_block": 199}
    assert path.read_bytes() == first_bytes
    resume_args = out["next_download_args"]
    assert resume_args == {**args, "chunk_blocks": 100, "expected_next_block": 200}
    with pytest.raises(ValueError, match="expected_next_block_ahead_of_archive"):
        event_history_operation(config, {**resume_args, "expected_next_block": 300}, provider=provider)
    for invalid in (None, True, 99, 305):
        with pytest.raises(ValueError, match="invalid_expected_next_block"):
            event_history_operation(config, {**resume_args, "expected_next_block": invalid}, provider=provider)
    assert calls == [(100, 199)] and path.read_bytes() == first_bytes

    provider.mismatch = True
    with pytest.raises(ValueError, match="event_checkpoint_link_mismatch"):
        event_history_operation(config, args, provider=provider)
    failed = inspect()
    assert failed["chunks_completed"] == 1 and failed["next_block"] == 200
    assert calls == [(100, 199), (200, 299)] and path.read_bytes() == first_bytes

    provider.mismatch = False
    bounded_args = {**resume_args, "max_chunks": 1}
    bounded = event_history_operation(config, bounded_args, provider=provider)
    assert bounded["next_block"] == 300 and bounded["chunks_downloaded"] == 1
    assert bounded["request_id"] == out["request_id"] and bounded["request"] == out["request"]
    assert bounded["next_download_args"] == {**bounded_args, "expected_next_block": 300}
    partial_bytes = path.read_bytes()
    stale = event_history_operation(config, bounded_args, provider=provider)
    assert stale["chunks_downloaded"] == 0 and stale["next_block"] == 300
    assert len(calls) == 3 and path.read_bytes() == partial_bytes
    complete = event_history_operation(config, bounded["next_download_args"], provider=provider)
    assert complete["event_range_complete"] is True and complete["next_block"] is None
    assert complete["next_download_args"] is None
    assert complete["chunks_completed"] == complete["rows"] == 3
    if with_range:
        assert complete["last_chunk_range_continuity"]["start_block"] == 300
        assert complete["last_chunk_range_continuity"]["receipt"]["verified"] is True
        assert complete["request"]["tick_lower"] == -120
    else:
        assert complete["last_chunk_range_continuity"] is None
    assert calls == [(100, 199), (200, 299), (200, 299), (300, 304)]
    saved = path.read_bytes()
    reopened = event_history_operation(config, args, provider=provider)
    assert reopened["chunks_downloaded"] == 0 and len(calls) == 4 and path.read_bytes() == saved
    for receipt in (out, failed, bounded, complete, reopened, inspect()):
        assert all(receipt[key] is False for key in ("requested_window_complete",
                   "tick_fee_reconstruction_verified", "replay_input", "coverage_complete", "core_state_verified"))
        assert receipt["core_state_status"] == "unverified"
        assert "raw_log" not in json.dumps(receipt)
    assert outputs[0]["_envelope"]["checkpoint_comparison"]["core_reconstruction_verified"] is False
    with sqlite3.connect(path) as db:
        stored = db.execute("SELECT evidence FROM chunks ORDER BY ordinal").fetchall()
        assert [json.loads(row[0]) for row in stored] == [outputs[0], outputs[2], outputs[3]]
        assert all(isinstance(row[0], bytes) and len(row[0]) <= 4_000_000 for row in stored)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE request SET metadata=?", (b"{}",))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM chunks")
