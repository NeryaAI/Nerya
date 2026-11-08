import sqlite3
import time
from types import SimpleNamespace
import pytest

from nerya.core.errors import TradingError
from nerya.llm.messages import MessagesResponse
from nerya.skills.builtin.backtest.scripts.authorized_replay import authorized_policy
from nerya.skills.builtin.backtest.scripts.config import (
    BacktestConfig,
    BacktestConfigError,
)
from nerya.skills.builtin.backtest.scripts.decision_producer import (
    DecisionProducer,
    DecisionProducerError,
)
from nerya.skills.builtin.backtest.scripts.retrospective_replay import VERDICT_SCHEMA


def config(policy):
    return SimpleNamespace(get=lambda key: policy)


def policy():
    return dict(
        enabled=True,
        authorization_id="fixture-authorization",
        provider="fixture",
        model="fixture",
        key_ref="vault://fixture_sign",
        max_calls=20,
        max_tokens=100000,
        deadline=time.time() + 120,
    )


def test_authorization_is_operator_owned_and_bounded():
    row = policy()
    assert authorized_policy(config(row)) == row
    for field, value in [
        ("enabled", False),
        ("max_calls", 21),
        ("max_tokens", 500001),
        ("deadline", time.time() - 1),
        ("deadline", time.time() + 1801),
        ("key_ref", "plaintext"),
        ("authorization_id", "../other"),
    ]:
        changed = {**row, field: value}
        with pytest.raises(TradingError):
            authorized_policy(config(changed))
    with pytest.raises(TradingError):
        authorized_policy(config(None))


@pytest.mark.parametrize("max_tokens", [1, 100000, 400000, 500000])
def test_token_budget_is_accepted_by_policy_and_producer(tmp_path, max_tokens):
    row = authorized_policy(config({**policy(), "max_tokens": max_tokens}))
    producer = DecisionProducer(
        tmp_path / "ledger.db", backend=lambda r: pytest.fail("no model call"),
        **{k: row[k] for k in (
            "authorization_id", "provider", "model", "max_calls", "max_tokens", "deadline"
        )},
    )
    assert producer.max_tokens == max_tokens


@pytest.mark.parametrize("max_tokens", [
    0, -1, 500001, None, True, "400000", 400000.0,
    float("inf"), float("-inf"), float("nan"),
])
def test_invalid_token_budget_is_rejected_by_policy_and_producer(tmp_path, max_tokens):
    row = {**policy(), "max_tokens": max_tokens}
    with pytest.raises(TradingError, match="invalid_or_expired_retrospective_authorization"):
        authorized_policy(config(row))
    with pytest.raises(DecisionProducerError, match="invalid retrospective replay authorization"):
        DecisionProducer(
            tmp_path / "ledger.db", backend=lambda r: pytest.fail("no model call"),
            **{k: row[k] for k in (
                "authorization_id", "provider", "model", "max_calls", "max_tokens", "deadline"
            )},
        )
    assert not (tmp_path / "ledger.db").exists()


@pytest.mark.parametrize("max_tokens,allowed_calls,exhausted", [
    (100000, 5, "tokens"), (400000, 20, "calls"),
])
def test_request_reservations_remain_bounded(tmp_path, monkeypatch, max_tokens, allowed_calls, exhausted):
    def forbidden(*args, **kwargs):
        pytest.fail("offline fixture must not access the network")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    row = authorized_policy(config({**policy(), "max_tokens": max_tokens}))
    calls = []

    def backend(request):
        calls.append(request)
        # Reservation must be durable before even this offline response.
        with sqlite3.connect(producer.path) as db:
            assert db.execute("SELECT calls,reserved FROM budget").fetchone() == (
                len(calls), len(calls) * reserve,
            )
        return MessagesResponse(
            content=[{"type": "text", "text": '{"decision":"HOLD","confidence":0.5}'}],
            provider=row["provider"], model=row["model"], usage={},
        )

    producer = DecisionProducer(
        tmp_path / "ledger.db", backend=backend,
        **{k: row[k] for k in (
            "authorization_id", "provider", "model", "max_calls", "max_tokens", "deadline"
        )},
    )
    inputs = dict(role_prompt="Confirm entry.", payload={"prefix": "x" * 13000},
                  output_schema=VERDICT_SCHEMA, max_output_tokens=4096)
    reserve = producer.request_identity(binding={"timestamp": 0}, **inputs)["reserve"]
    assert 18000 < reserve < 20000
    for stamp in range(allowed_calls):
        producer.produce(binding={"timestamp": stamp}, **inputs)
    before = producer.path.read_bytes()
    with pytest.raises(DecisionProducerError, match="^decision_budget_exhausted$") as caught:
        producer.produce(binding={"timestamp": allowed_calls}, **inputs)
    assert caught.value.receipt["exhausted_limits"] == [exhausted]
    assert caught.value.receipt["request_sent"] is False
    assert len(calls) == allowed_calls
    assert producer.path.read_bytes() == before
    if max_tokens == 100000:
        with pytest.raises(DecisionProducerError, match="authorization policy changed"):
            DecisionProducer(
                producer.path, backend=backend, authorization_id=row["authorization_id"],
                provider=row["provider"], model=row["model"], max_calls=row["max_calls"],
                max_tokens=400000, deadline=row["deadline"],
            )
        assert producer.path.read_bytes() == before


def test_agent_replay_requires_boolean_and_is_not_mock_mode():
    assert BacktestConfig.from_raw({"agent_replay": True}).agent_replay
    assert BacktestConfig.from_raw({}).agent_replay is False
    for value in [1, "true", None]:
        with pytest.raises(BacktestConfigError):
            BacktestConfig.from_raw({"agent_replay": value})
