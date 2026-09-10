"""Hardening tests for strategy-core audit fixes (D2/D3/D4/D5/D7/D8/D10/D11/D12)."""

from __future__ import annotations

import importlib.util
import sys
import threading
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.skills.builtin.backtest.scripts.mock_ctx import (
    BacktestUnsupportedSurfaceError,
    MockMarket,
)
from nerya.strategies.context import (
    StrategyAudit,
    StrategyClock,
    StrategyMarket,
    StrategyPolicyView,
    StrategyRunDeadline,
    StrategyRuntimeError,
    StrategyState,
    StrategySubAgents,
    StrategyTrading,
)
from nerya.strategies.evolution import _filter_changes
from nerya.strategies.package import (
    StrategyTuningConfig,
    StrategyTuningGuardrails,
    load_package,
)
from nerya.strategies.runner import _pop_strategy_package_modules
from nerya.workspace.state_store import StateStore


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "paper_main",
                    "exchange": "mock",
                    "venue": "mock",
                    "mode": "paper",
                    "status": "active",
                    "initial_balance_usd": 10_000,
                    "permissions": {
                        "read_balances": True,
                        "place_order": True,
                        "cancel_order": True,
                    },
                }
            ]
        },
    )
    return cfg


def _policy(**overrides) -> StrategyPolicyView:
    values = dict(
        max_single_order_usd=1_000,
        max_daily_notional_usd=10_000,
        max_open_positions=10,
        min_confidence=0,
        allow_direct_order=True,
        require_subagent_before_order=False,
        default_order_usd=100,
        max_run_seconds=30,
        default_tier="light",
        allowed_tiers=("light",),
        max_calls_per_run=1,
        raw_policy={},
        raw_llm_policy={},
    )
    values.update(overrides)
    return StrategyPolicyView(**values)


# ---------------------------------------------------------------------------
# D8 — StrategyClock.now() honours freeze()
# ---------------------------------------------------------------------------


def test_clock_freeze_pins_now_datetime() -> None:
    clock = StrategyClock()
    clock.freeze(iso="2031-06-01T12:34:56+00:00")

    assert clock.now() == datetime(2031, 6, 1, 12, 34, 56, tzinfo=timezone.utc)
    assert clock.now_ms() == int(
        datetime(2031, 6, 1, 12, 34, 56, tzinfo=timezone.utc).timestamp() * 1000
    )
    assert clock.now_iso() == "2031-06-01T12:34:56+00:00"


# ---------------------------------------------------------------------------
# D3b — StateStore instances are shared per path, so compare_and_set
# actually excludes every thread in this process.
# ---------------------------------------------------------------------------


def test_state_store_instances_share_one_lock_per_path(tmp_path) -> None:
    path = tmp_path / "state.json"
    store_a = StateStore(path)
    store_b = StateStore(path)

    assert store_a is store_b
    assert store_a._lock is store_b._lock


def test_state_store_compare_and_set_excludes_threads(tmp_path) -> None:
    path = tmp_path / "state.json"
    # Two construction sites (as the runner would do per tick) and two
    # StrategyState wrappers — they must all share the underlying store.
    state_a = StrategyState(store=StateStore(path))
    state_b = StrategyState(store=StateStore(path))
    state_a.set("n", 0)

    barrier = threading.Barrier(2)
    wins: dict[str, int] = {"a": 0, "b": 0}

    def _cas_loop(state: StrategyState, tag: str) -> None:
        barrier.wait()
        for _ in range(50):
            current = state.get("n")
            if state.compare_and_set("n", expect=current, new_value=int(current) + 1):
                wins[tag] += 1

    threads = [
        threading.Thread(target=_cas_loop, args=(state_a, "a")),
        threading.Thread(target=_cas_loop, args=(state_b, "b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = wins["a"] + wins["b"]
    assert total > 0
    # No lost updates: the final value equals the number of successful CAS.
    assert int(StrategyState(store=StateStore(path)).get("n")) == total


# ---------------------------------------------------------------------------
# D4 — a timed-out run refuses late side effects (fail closed).
# ---------------------------------------------------------------------------


def test_timed_out_run_refuses_submit_intent_and_state_writes(tmp_path) -> None:
    deadline = StrategyRunDeadline()
    trading = StrategyTrading(
        config=_config(tmp_path),
        strategy_id="d4_guard",
        policy=_policy(),
        accounts=("paper_main",),
        execution_mode="shadow",
        run_deadline=deadline,
    )
    state = StrategyState(
        store=StateStore(tmp_path / "state.json"),
        run_deadline=deadline,
    )

    # Before the deadline, a shadow submission still works.
    envelope = trading.submit_intent(
        market="mock:BTC/USDT", side="buy", size=10, size_unit="usd"
    )
    assert envelope["status"] == "submitted"

    deadline.trigger("max_run_seconds=5")
    with pytest.raises(StrategyRuntimeError, match="run timed out"):
        trading.submit_intent(
            market="mock:BTC/USDT", side="buy", size=10, size_unit="usd"
        )
    with pytest.raises(StrategyRuntimeError, match="run timed out"):
        trading.open_position(market="mock:BTC/USDT", side="long", sizing={})
    with pytest.raises(StrategyRuntimeError, match="run timed out"):
        trading.close_position(market="mock:BTC/USDT", side="long")
    with pytest.raises(StrategyRuntimeError, match="run timed out"):
        state.set("late", "write")
    with pytest.raises(StrategyRuntimeError, match="run timed out"):
        state.update(late="write")


# ---------------------------------------------------------------------------
# D7 — live venues fail closed on empty candle fetches; MOCK still returns [].
# ---------------------------------------------------------------------------


def test_live_candles_empty_raises_but_mock_returns_empty(tmp_path, monkeypatch) -> None:
    market = StrategyMarket(
        paths=WorkspacePaths(root=tmp_path),
        accounts=(),
        _registry_factory=lambda: None,
    )
    monkeypatch.setattr(
        "nerya.strategies.context.fetch_candles", lambda *args, **kwargs: []
    )
    with pytest.raises(StrategyRuntimeError, match="no_live_candles"):
        market.candles("BINANCE:BTCUSDT", timeframe="1h", limit=10)

    class _StubConnector:
        venue = "MOCK"

        def get_klines(self, market: str, *, interval: str, limit: int):
            return []

    monkeypatch.setattr(
        market, "_connector_for", lambda m, account=None: _StubConnector()
    )
    assert market.candles("MOCK:BTCUSDT", timeframe="1h", limit=10) == []


# ---------------------------------------------------------------------------
# D2 — the notional cap sees base/quote units instead of skipping them.
# ---------------------------------------------------------------------------


def test_base_unit_cap_uses_snapshot_price(tmp_path) -> None:
    trading = StrategyTrading(
        config=_config(tmp_path),
        strategy_id="d2_guard",
        policy=_policy(max_single_order_usd=100),
        accounts=("paper_main",),
        execution_mode="shadow",
    )

    # 5 base * 50 USD = 250 > 100 cap → refused (previously skipped).
    with pytest.raises(StrategyRuntimeError, match="exceeds max_single_order_usd"):
        trading.submit_intent(
            market="mock:BTC/USDT",
            side="buy",
            size=5,
            size_unit="base",
            market_snapshot={"price": 50, "age_s": 0},
        )
    # 1 base * 50 USD = 50 <= 100 cap → passes strategy-local policy.
    out = trading.submit_intent(
        market="mock:BTC/USDT",
        side="buy",
        size=1,
        size_unit="base",
        market_snapshot={"mid": 50, "age_s": 0},
    )
    assert out["status"] == "submitted"


# ---------------------------------------------------------------------------
# D10 — backtest mock refuses unprovided foreign timeframes.
# ---------------------------------------------------------------------------


def test_backtest_mock_market_rejects_missing_foreign_timeframe() -> None:
    rows = [
        {"ts": 1, "open": 99, "high": 101, "low": 98, "close": 100, "volume": 1},
        {"ts": 2, "open": 100, "high": 102, "low": 99, "close": 101, "volume": 2},
    ]
    market = MockMarket("MOCK:BTCUSDT", {"MOCK:BTCUSDT": rows}, primary_timeframe="1h")

    # Same-timeframe fallback stays.
    assert market.candles("MOCK:BTCUSDT", timeframe="1h", limit=1)[0]["close"] == 101
    with pytest.raises(BacktestUnsupportedSurfaceError, match="15m"):
        market.candles("MOCK:BTCUSDT", timeframe="15m", limit=2)

    # A provided timeframe key is served without raising.
    provided = MockMarket(
        "MOCK:BTCUSDT",
        {"MOCK:BTCUSDT": rows},
        {"MOCK:BTCUSDT": {"15m": rows}},
        primary_timeframe="1h",
    )
    assert provided.candles("MOCK:BTCUSDT", timeframe="15m", limit=1)[0]["close"] == 101


# ---------------------------------------------------------------------------
# D11 — the position-size guardrail is enforced in _filter_changes.
# ---------------------------------------------------------------------------


def _write_strategy_pkg(tmp_path, strategy_id: str = "d11_guard"):
    cfg = _config(tmp_path)
    root = cfg.paths.strategy(strategy_id)
    yaml_io.dump(
        root / "strategy.yml",
        {
            "version": 1,
            "strategy_id": strategy_id,
            "title": "D11 guard",
            "mode": "paper",
            "entrypoint": "main.py:run",
            "markets": ["mock:BTC/USDT"],
            "accounts": ["paper_main"],
            "schedule": {"type": "interval", "every_seconds": 60},
            "policy": {
                "max_single_order_usd": 100,
                "max_daily_notional_usd": 1_000,
            },
        },
    )
    (root / "main.py").write_text("def run(ctx):\n    return ctx.result.hold()\n", encoding="utf-8")
    return cfg, load_package(cfg.paths, strategy_id)


def test_filter_changes_enforces_position_size_guardrail(tmp_path) -> None:
    _, pkg = _write_strategy_pkg(tmp_path)
    tuning_cfg = StrategyTuningConfig(
        guardrails=StrategyTuningGuardrails(max_position_size_change_pct=25.0)
    )

    def _output(max_single_order_usd: float) -> dict:
        return {
            "proposed_changes": [
                {
                    "file": "strategy.yml",
                    "kind": "strategy_yml",
                    "config_after": {
                        "policy": {
                            "max_single_order_usd": max_single_order_usd,
                            "max_daily_notional_usd": 1_000,
                        }
                    },
                }
            ]
        }

    accepted, dropped, _ = _filter_changes(
        _output(500), tuning_cfg, pkg=pkg
    )
    assert accepted == []
    assert dropped[0]["reason"].startswith("position_size_guardrail")
    assert "max_single_order_usd" in dropped[0]["reason"]

    accepted, dropped, _ = _filter_changes(_output(110), tuning_cfg, pkg=pkg)
    assert len(accepted) == 1 and dropped == []

    # Without a package (legacy callers) the guardrail stays prompt-side.
    accepted, _, _ = _filter_changes(_output(500), tuning_cfg)
    assert len(accepted) == 1


# ---------------------------------------------------------------------------
# D12 — subagent dispatches emit strategy.subagent.run audit events.
# ---------------------------------------------------------------------------


def test_subagent_run_emits_audit_event(tmp_path) -> None:
    audit = StrategyAudit(
        paths=WorkspacePaths(root=tmp_path), strategy_id="d12", run_id="run_d12"
    )

    class _StubDispatcher:
        def dispatch(self, target, **kwargs):
            return {"ok": True, "output": {}}

    subagents = StrategySubAgents(
        config=_config(tmp_path),
        skills=None,
        strategy_id="d12",
        audit=audit,
    )
    subagents._dispatcher = _StubDispatcher()

    envelope = subagents.run("analyst", payload={})
    assert envelope["ok"] is True

    events = [e for e in audit.events() if e.get("kind") == "strategy.subagent.run"]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["name"] == "analyst"
    assert payload["ok"] is True
    assert isinstance(payload["duration_ms"], int)


# ---------------------------------------------------------------------------
# D5 — helper modules imported by a strategy package don't leak into the
# global module table.
# ---------------------------------------------------------------------------


def test_pop_strategy_package_modules_scopes_cleanup(tmp_path) -> None:
    pkg_root = tmp_path / "pkg"
    pkg_root.mkdir()
    helper = pkg_root / "helpers.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")

    before = frozenset(sys.modules)
    spec = importlib.util.spec_from_file_location("_d5_helpers", helper)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_d5_helpers"] = module
    try:
        assert "_d5_helpers" in sys.modules
        _pop_strategy_package_modules(pkg_root, before)
        assert "_d5_helpers" not in sys.modules

        # Modules outside the package root (and pre-existing ones) survive.
        sys.modules["_d5_helpers"] = module
        _pop_strategy_package_modules(tmp_path / "other", frozenset(sys.modules))
        assert "_d5_helpers" in sys.modules
    finally:
        sys.modules.pop("_d5_helpers", None)
