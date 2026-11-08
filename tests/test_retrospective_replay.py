"""Actual isolated candidate and host engine; synthetic producer, no network."""

from pathlib import Path
import time
import pytest

from nerya.core.paths import WorkspacePaths
from nerya.evolution.strategy_code_generator import (
    StrategyCodeGenerator,
    StrategyGenerationRequest,
)
from nerya.strategies.package import load_package_from_dir
from nerya.llm.messages import MessagesResponse
from nerya.skills.builtin.backtest.scripts.config import BacktestConfig
from nerya.skills.builtin.backtest.scripts.decision_producer import DecisionProducer
from nerya.skills.builtin.backtest.scripts.retrospective_replay import run_retrospective


def fixture(tmp_path):
    generated = StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(
        StrategyGenerationRequest(
            strategy_id="retrospective_fixture",
            strategy_class="trend",
            execution_mode="script",
            markets=("BINANCE:ETHUSDT",),
            accounts=("paper",),
            timeframe="1h",
            subagents=("market_analyst",),
            create_tuning=False,
        ),
        validate=False,
        create_proposal_record=False,
    )
    root = tmp_path / "candidate"
    root.mkdir()
    for name, text in generated.files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    package = load_package_from_dir(root)
    prices = [100.0] * 160 + [120.0, 121.0, 80.0, 80.0]
    rows = [
        dict(ts=1700006400 + i * 3600, open=p, high=p, low=p, close=p, volume=100.0)
        for i, p in enumerate(prices)
    ]
    cfg = BacktestConfig.from_raw(
        {
            "markets": ["BINANCE:ETHUSDT"],
            "tf": "1h",
            "warmup_bars": 50,
            "max_run_seconds": 60,
        }
    )
    return package, cfg, {"BINANCE:ETHUSDT": rows}


def test_actual_gated_candidate_and_tape_resume_keep_model_and_ledger_in_host(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network"))
    package, cfg, data = fixture(tmp_path)
    calls = []

    def model(request):
        calls.append(request)
        return MessagesResponse(
            content=[{"type": "text", "text": '{"decision":"ENTRY","confidence":0.9}'}],
            provider="fixture",
            model="fixture-model",
            usage={"input_tokens": 12, "output_tokens": 8},
        )

    producer = DecisionProducer(
        tmp_path / "host" / "calls.db",
        authorization_id="test",
        backend=model,
        provider="fixture",
        model="fixture-model",
        max_calls=3,
        max_tokens=100000,
        deadline=time.time() + 120,
    )
    args = dict(
        candles_by_market=data,
        timeframe_candles_by_market={},
        strategy_config=package.manifest.asdict(),
        producer=producer,
        host_key=b"x" * 32,
        tape_path=tmp_path / "host" / "tape.json",
        role="market_analyst",
        role_prompt=(package.root / "subagents/market_analyst.agent.md").read_text(),
    )
    result, proof = run_retrospective(package, cfg, **args)
    assert len(calls) == 1
    assert proof["decision_count"] == 1 and proof["decision_receipts_verified"]
    assert (
        proof["isolation"]["network"] == "denied"
        and proof["isolation"]["process_exited"]
    )
    assert proof["performance_evidence"] is False
    assert result.order_attempts >= 1 and result.trades
    repeated, again = run_retrospective(package, cfg, **args)
    assert len(calls) == 1 and repeated.equity_series == result.equity_series
    assert again["tape_sha256"] == proof["tape_sha256"]


@pytest.mark.parametrize("max_output_tokens", [
    "cancelled", "omitted", 1, 16384, None, True, False, "16384", 16384.0, 0, 16385,
])
def test_full_backtest_producer_preserves_retrospective_receipt_and_report(
    tmp_path, monkeypatch, max_output_tokens
):
    import json
    import sqlite3
    from nerya.harness.cancellation import CancelledError
    from nerya.core.errors import TradingError
    from nerya.core import yaml_io
    from nerya.skills.builtin.backtest.scripts import backtest_run, authorized_replay
    from nerya.skills.builtin.backtest.scripts.decision_producer import DecisionProducerError
    from nerya.skills.builtin.backtest.scripts.retrospective_replay import VERDICT_SCHEMA, _digest
    from nerya.skills.builtin.backtest.scripts.decision_tape import DecisionTapeError

    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network"))
    package, cfg, data = fixture(tmp_path)
    rows = data["BINANCE:ETHUSDT"]
    cancelled = max_output_tokens == "cancelled"
    if cancelled:
        # HOLD the first crossover, then request confirmation on another one.
        for price in [150.0, 151.0]:
            rows.append(dict(ts=rows[-1]["ts"] + 3600, open=price, high=price,
                             low=price, close=price, volume=100.0))
    calls = []
    prepared = []
    prepared_inputs = []
    cap = 4096 if max_output_tokens in ("omitted", "cancelled") else max_output_tokens
    valid = type(cap) is int and 1 <= cap <= 16384
    produce = DecisionProducer.produce

    def checked_produce(producer, **inputs):
        assert inputs["max_output_tokens"] == cap
        assert inputs["binding"]["binding"]["model_settings_hash"] == _digest(
            [producer.provider, producer.model, 0, cap, producer.output_policy(VERDICT_SCHEMA)]
        )
        identity = producer.request_identity(**inputs)
        other_cap = 1 if cap != 1 else 4096
        other = producer.request_identity(**{**inputs, "max_output_tokens": other_cap})
        assert identity["digest"] != other["digest"]
        assert identity["reserve"] - other["reserve"] == cap - other_cap
        prepared.append((producer, identity))
        prepared_inputs.append(inputs)
        return produce(producer, **inputs)

    monkeypatch.setattr(DecisionProducer, "produce", checked_produce)

    from nerya.llm.messages import OpenAIMessagesBackend

    class Transport:
        def post_json(self, url, **request):
            assert request["body"]["max_tokens"] == cap
            producer, identity = prepared[-1]
            with sqlite3.connect(producer.path) as db:
                assert db.execute("SELECT calls,reserved FROM budget").fetchone() == (
                    len(calls) + 1, 20 * len(calls) + identity["reserve"],
                )
                assert db.execute("SELECT status FROM requests WHERE digest=?",
                                  (identity["digest"],)).fetchone() == ("unconfirmed",)
            assert 20 * len(calls) + identity["reserve"] <= producer.max_tokens
            calls.append(request)
            if cancelled and len(calls) == 2:
                raise CancelledError("deadline_exceeded")
            return 200, {
                "choices": [
                    {
                        "message": {"content": '{"decision":"HOLD","confidence":0.6}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            }

    backend = OpenAIMessagesBackend(
        api_key="offline-fixture",
        model="fixture-model",
        provider_name="fixture",
        transport=Transport(),
    )

    deadline = time.time() + 120
    yaml_io.dump(
        tmp_path / "nerya.yml",
        {
            "backtest": {
                "retrospective_replay": {
                    "enabled": True,
                    "authorization_id": "fixture-authorization",
                    "provider": "fixture",
                    "model": "fixture-model",
                    "key_ref": "vault://fixture_sign",
                    "max_calls": 3,
                    "max_tokens": 400000,
                    "deadline": deadline,
                    **({} if max_output_tokens == "omitted" else {"max_output_tokens": cap}),
                }
            }
        },
    )

    class Gateway:
        _con = None

        def __init__(self, *a):
            pass

        def _messages_route_cfgs(self, *a, **k):
            return [{"provider": "fixture", "model": "fixture-model"}]

        def _resolve_messages_backend(self, *a, **k):
            return backend

    class Vault:
        def resolve(self, *a, **k):
            return (b"x" * 32).hex()

    monkeypatch.setattr(authorized_replay, "LLMGateway", Gateway)
    monkeypatch.setattr(authorized_replay.SecretVault, "open", lambda *a, **k: Vault())
    monkeypatch.setattr(backtest_run, "get_candles", lambda *a, **k: rows)
    monkeypatch.setattr(
        "nerya.evidence.autoingest.on_backtest_finalize", lambda *a, **k: None
    )
    run_args = dict(
        package_dir=package.root,
        workspace=tmp_path,
        allow_mock=True,
        settings={
            "agent_replay": True,
            "tf": "1h",
            "warmup_bars": 50,
            "start_utc": str(rows[50]["ts"]),
            "end_utc": str(rows[-1]["ts"] + 3600),
        },
    )
    if not valid:
        with pytest.raises(TradingError, match="invalid_or_expired_retrospective_authorization"):
            backtest_run.run_strategy_backtest(**run_args)
        with pytest.raises(DecisionTapeError, match="invalid decision output budget"):
            run_retrospective(package, cfg, candles_by_market=data, timeframe_candles_by_market={},
                strategy_config=package.manifest.asdict(), producer=None, host_key=b"x" * 32,
                tape_path=tmp_path / "invalid.json", role="market_analyst", role_prompt="fixture",
                max_output_tokens=cap)
        with pytest.raises(DecisionProducerError, match="invalid decision output budget"):
            DecisionProducer.request_identity(None, binding={}, role_prompt="fixture", payload={},
                output_schema=VERDICT_SCHEMA, max_output_tokens=cap)
        assert not calls and not prepared
        return
    if cancelled:
        with pytest.raises(CancelledError, match="^deadline_exceeded$") as caught:
            backtest_run.run_strategy_backtest(**run_args)
        failure = json.loads(Path(caught.value.failure_path).read_text())
        assert failure["status"] == "cancelled"
        assert failure["phase"] == "replaying"
        assert failure["error"]["type"] == "CancelledError"
        detail = failure["details"]
        assert detail["status"] == "cancelled"
        assert detail["reason"] == "deadline_exceeded"
        assert detail["decision_count"] == 1
        assert detail["request_digest"] == prepared[-1][1]["digest"]
        assert detail["request_status"] == "unconfirmed"
        assert detail["authorization_id"] == "fixture-authorization"
        assert detail["performance_evidence"] is False
        assert detail["reservation_retained"] is True
        assert detail["safe_to_retry"] is False
        assert detail["requires_receipt_reconciliation"] is True
        assert detail["recovery_disposition"] == "no_automatic_retry"
        tape = Path(caught.value.failure_path).with_name("decision_tape.json").read_bytes()
        assert len(json.loads(tape)["receipts"]) == 1
        producer, identity = prepared[-1]
        before = producer.path.read_bytes()
        with sqlite3.connect(producer.path) as db:
            assert db.execute("SELECT status FROM requests ORDER BY rowid").fetchall() == [
                ("complete",), ("unconfirmed",)]
            assert db.execute("SELECT calls,reserved FROM budget").fetchone() == (
                2, 20 + identity["reserve"])
        with pytest.raises(DecisionProducerError, match="unconfirmed"):
            produce(producer, **prepared_inputs[-1])
        assert producer.path.read_bytes() == before and len(calls) == 2
        assert Path(caught.value.failure_path).with_name("decision_tape.json").read_bytes() == tape
        assert not Path(caught.value.failure_path).with_name("metrics.json").exists()
        assert not Path(caught.value.failure_path).with_name("report.html").exists()
        public = json.dumps(detail)
        assert len(public) < 1500
        assert "offline-fixture" not in public and (b"x" * 32).hex() not in public
        assert "historical_timestamp_ms" not in public and "confidence" not in public
        return
    receipt = backtest_run.run_strategy_backtest(**run_args)
    assert receipt["ok"] and calls
    assert receipt["decision_replay"]["decision_receipts_verified"]
    assert receipt["decision_replay"]["model_decisions_verified"]
    assert receipt["performance_evidence"] is False
    assert receipt["replay"]["agent_execution"] == "current_model_retrospective_replay"
    metrics = json.loads(Path(receipt["metrics_path"]).read_text())
    report = (tmp_path / receipt["report_path"]).read_text()
    scope = (
        "Historical replay with verified current-model Agent answers; "
        "no contemporaneous decisions, provider model-version attestation, "
        "live fills or out-of-sample claim."
    )
    assert receipt["provenance"]["scope"] == metrics["provenance"]["scope"] == scope
    assert "no real Agent answers" not in report
    assert "Provider model version was not attested." in report
    assert "Historical fills are simulated, not live execution." in report
    assert metrics["performance_evidence"] is False
    assert (
        metrics["decision_replay"]["tape_sha256"]
        == receipt["decision_replay"]["tape_sha256"]
    )
    assert (
        "Current-model retrospective replay" in report
        and "not contemporaneous" in report
    )
    assert (
        json.loads((Path(receipt["run_path"]) / "run.json").read_text())["status"]
        == "completed"
    )
