"""Round-2 audit regression tests (R2A1..R2A8).

Each case pins one Round-2 finding:

* R2A1/R2A3 — a VNpy strategy resumed after a process restart re-arms
  ``trading``/``inited`` and restores the persisted ``pos``.
* R2A2 — parked stop-order ids stay unique across ticks (persisted seq).
* R2A4 — backtest metrics pair short entries/exits direction-aware.
* R2A5 — ``load_framework_strategy`` keeps package helper imports
  (``import helpers``) out of the global module table.
* R2A6 — a timed-out zombie run cannot reduce positions, attach
  protection, or mutate/delete persisted state.
* R2A7 — backtest mock timeframe lookups are case-insensitive.
* R2A8 — a corrupted state file is quarantined, not silently wiped.
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.skills.builtin.backtest.scripts.mock_ctx import (
    BacktestUnsupportedSurfaceError,
    MockMarket,
)
from nerya.strategies.context import (
    StrategyRunDeadline,
    StrategyRuntimeError,
    StrategyState,
    StrategyTrading,
)
from nerya.strategies.result import ResultBuilder
from nerya.workspace.state_store import StateStore

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _candles(closes: list[float], *, start_ts: int = 1_700_000_000, step_s: int = 60) -> list[dict]:
    rows = []
    for i, close in enumerate(closes):
        open_ = closes[i - 1] if i else close
        rows.append(
            {
                "ts": start_ts + i * step_s,
                "open": open_,
                "high": max(open_, close) * 1.001,
                "low": min(open_, close) * 0.999,
                "close": close,
                "volume": 10.0 + i,
            }
        )
    return rows


class FakeState:
    def __init__(self) -> None:
        self._data: dict = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value) -> None:
        self._data[key] = value


class ScriptedTrading:
    """submit_intent stub returning a scripted envelope status per order."""

    def __init__(self, *statuses: str) -> None:
        self.intents: list[dict] = []
        self.statuses = list(statuses) or ["filled"]
        self.n = 0

    def submit_intent(self, **payload):
        self.intents.append(dict(payload))
        status = self.statuses[min(self.n, len(self.statuses) - 1)]
        self.n += 1
        return {
            "ok": status not in {"rejected", "failed"},
            "status": status,
            "intent_id": f"scripted_{self.n}",
            "intent": dict(payload),
            "order": {},
            "risk_decision": {"decision": "allow"},
        }


def _fake_ctx(candles: list[dict], state: FakeState | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        market=SimpleNamespace(
            candles=lambda market, *a, timeframe="1m", limit=990, **kw: list(candles)[-limit:]
        ),
        state=state or FakeState(),
        trading=ScriptedTrading(),
        policy=SimpleNamespace(default_order_usd=100.0),
        result=ResultBuilder(),
        audit=None,
        config=SimpleNamespace(mode="paper", markets=("PAPER:BTCUSDT",)),
    )


def _cta_template() -> type:
    from nerya.strategies.compat.shims import install_vnpy_shims

    install_vnpy_shims()
    import vnpy_ctastrategy

    return vnpy_ctastrategy.CtaTemplate


def _vnpy_strategy(cls: type, name: str) -> Any:
    return cls(None, name, "ADV.LOCAL")


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


def _policy() -> Any:
    from nerya.strategies.context import StrategyPolicyView

    return StrategyPolicyView(
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


# ---------------------------------------------------------------------------
# R2A1 + R2A3 — restart resumes trading and restores pos
# ---------------------------------------------------------------------------


def test_vnpy_restart_resumes_trading_and_restores_pos() -> None:
    CtaTemplate = _cta_template()

    from nerya.strategies.compat.vnpy_adapter import run_vnpy_tick

    class BuyThenSellStrategy(CtaTemplate):
        def on_bar(self, bar):
            if self.pos == 0:
                self.buy(bar.close_price, 0.5)
            elif self.pos > 0:
                self.sell(bar.close_price, abs(self.pos))

    settings = {"bar_timeframe": "1m"}
    base = [100.0 + i for i in range(6)]
    state = FakeState()

    # Tick 1: init. Tick 2: the entry fills; pos is persisted.
    strategy = _vnpy_strategy(BuyThenSellStrategy, "r2a1_first")
    run_vnpy_tick(_fake_ctx(_candles(base), state), strategy, market="PAPER:BTCUSDT", settings=settings)
    ctx2 = _fake_ctx(_candles(base + [106.0]), state)
    run_vnpy_tick(ctx2, strategy, market="PAPER:BTCUSDT", settings=settings)
    assert len(ctx2.trading.intents) == 1
    persisted = state.get("_compat_vnpy")
    assert float(persisted["pos"]) == pytest.approx(0.5)
    assert persisted["inited"] is True

    # Process restart: a FRESH strategy instance (shim constructs it
    # dormant: trading=False, pos=0) plus a fresh ctx, seeded with the
    # same persisted state. The next tick must trade again — and the
    # close only fires because pos was restored before on_bar ran.
    fresh = _vnpy_strategy(BuyThenSellStrategy, "r2a1_fresh")
    assert fresh.trading is False
    assert float(fresh.pos) == 0.0

    ctx3 = _fake_ctx(_candles(base + [106.0, 107.0]), state)
    run_vnpy_tick(ctx3, fresh, market="PAPER:BTCUSDT", settings=settings)

    assert fresh.trading is True
    assert fresh.inited is True
    assert len(ctx3.trading.intents) == 1, (
        "restarted strategy must trade again (R2A1) using the restored "
        "position (R2A3)"
    )
    assert ctx3.trading.intents[0]["side"] == "sell"
    assert ctx3.trading.intents[0]["plan_action"] == "close_position"
    assert float(fresh.pos) == pytest.approx(0.0)
    assert float(state.get("_compat_vnpy")["pos"]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# R2A2 — parked stop ids are unique across ticks
# ---------------------------------------------------------------------------


def test_vnpy_stop_ids_unique_across_ticks() -> None:
    CtaTemplate = _cta_template()

    from nerya.strategies.compat.vnpy_adapter import run_vnpy_tick

    class TwoStopStrategy(CtaTemplate):
        seen = 0

        def on_bar(self, bar):
            self.seen += 1
            if self.seen == 1:
                self.id1 = self.buy(bar.close_price * 1.10, 1.0, stop=True)[0]
            elif self.seen == 2:
                self.id2 = self.buy(bar.close_price * 1.20, 1.0, stop=True)[0]
            elif self.seen == 3:
                self.cancel_order(self.id2)

    settings = {"bar_timeframe": "1m"}
    base = [100.0, 100.5, 101.0, 101.5, 102.0]
    state = FakeState()

    strategy = _vnpy_strategy(TwoStopStrategy, "r2a2")
    run_vnpy_tick(_fake_ctx(_candles(base), state), strategy, market="PAPER:BTCUSDT", settings=settings)

    # Tick 1 parks stop #1 at 103 * 1.10; tick 2 parks stop #2 at a
    # different price. The ids must not collide across ticks.
    run_vnpy_tick(
        _fake_ctx(_candles(base + [103.0]), state), strategy, market="PAPER:BTCUSDT", settings=settings
    )
    run_vnpy_tick(
        _fake_ctx(_candles(base + [103.0, 104.0]), state), strategy, market="PAPER:BTCUSDT", settings=settings
    )
    assert strategy.id1 == "STOP.1"
    assert strategy.id2 != strategy.id1
    pending = state.get("_compat_vnpy")["pending_stops"]
    assert [rec["vt_orderid"] for rec in pending] == [strategy.id1, strategy.id2]
    assert state.get("_compat_vnpy")["seq"] == 2

    # Cancelling the *newer* id by id must remove the newer stop and
    # leave the older one parked (the per-tick seq reset removed the
    # wrong one before).
    run_vnpy_tick(
        _fake_ctx(_candles(base + [103.0, 104.0, 105.0]), state),
        strategy,
        market="PAPER:BTCUSDT",
        settings=settings,
    )
    remaining = state.get("_compat_vnpy")["pending_stops"]
    assert len(remaining) == 1
    assert remaining[0]["vt_orderid"] == strategy.id1
    assert remaining[0]["price"] == pytest.approx(103.0 * 1.10)


# ---------------------------------------------------------------------------
# R2A4 — metrics pair short trades direction-aware
# ---------------------------------------------------------------------------


def test_metrics_pairs_short_trades_direction_aware() -> None:
    from nerya.skills.builtin.backtest.scripts.metrics import _closed_trade_pairs

    trades = [
        # Short entry (sell fill) then its buy exit: +10 gross, -2 fees.
        {"ts": 0, "market": "M", "side": "sell", "qty": 1.0, "price": 100.0, "fee": 1.0},
        {"ts": 3600, "market": "M", "side": "buy", "qty": 1.0, "price": 90.0, "fee": 1.0},
        # Long entry then its sell exit: +5 gross, -2 fees.
        {"ts": 7200, "market": "M", "side": "buy", "qty": 1.0, "price": 90.0, "fee": 1.0},
        {"ts": 10800, "market": "M", "side": "sell", "qty": 1.0, "price": 95.0, "fee": 1.0},
    ]
    pairs = _closed_trade_pairs(trades)

    assert len(pairs) == 2, "short entry/exit must pair, not leak a phantom long"
    short, long_ = pairs
    assert short["pnl_usd"] == pytest.approx(8.0)
    assert long_["pnl_usd"] == pytest.approx(3.0)
    assert short["duration_hours"] == pytest.approx(1.0)
    # The short's losing exit stamps the fill's pnl with the signed value.
    assert trades[1]["pnl"] == pytest.approx(8.0)


def test_metrics_pairs_forced_close_of_short() -> None:
    from nerya.skills.builtin.backtest.scripts.metrics import _closed_trade_pairs

    trades = [
        {"ts": 0, "market": "M", "side": "sell", "qty": 1.0, "price": 100.0, "fee": 0.0},
        {
            "ts": 3600,
            "market": "M",
            "side": "buy",
            "qty": 1.0,
            "price": 105.0,
            "fee": 0.0,
            "forced_close": True,
        },
    ]
    pairs = _closed_trade_pairs(trades)
    assert len(pairs) == 1
    assert pairs[0]["pnl_usd"] == pytest.approx(-5.0)


def test_metrics_long_only_pairing_unchanged() -> None:
    from nerya.skills.builtin.backtest.scripts.metrics import _closed_trade_pairs

    trades = [
        {"ts": 0, "market": "M", "side": "buy", "qty": 2.0, "price": 100.0, "fee": 1.0},
        {"ts": 3600, "market": "M", "side": "sell", "qty": 2.0, "price": 110.0, "fee": 1.0},
    ]
    pairs = _closed_trade_pairs(trades)
    assert len(pairs) == 1
    assert pairs[0]["pnl_usd"] == pytest.approx(18.0)
    assert pairs[0]["pnl_pct"] == pytest.approx(9.0)
    assert trades[1]["pnl"] == pytest.approx(18.0)


# ---------------------------------------------------------------------------
# R2A5 — load_framework_strategy pops package-local helper modules
# ---------------------------------------------------------------------------


def test_load_framework_strategy_pops_package_helper_modules(tmp_path: Path) -> None:
    from nerya.strategies.compat.entrypoint import load_framework_strategy
    from nerya.strategies.compat.shims import install_vnpy_shims

    install_vnpy_shims()

    src = (
        "import helpers\n"
        "from vnpy_ctastrategy import CtaTemplate\n"
        "\n"
        "\n"
        "class PkgStrategy(CtaTemplate):\n"
        "    helper_value = helpers.VALUE\n"
    )
    root1 = tmp_path / "pkg_one"
    root1.mkdir()
    (root1 / "helpers.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root1 / "strategy.py").write_text(src, encoding="utf-8")

    s1 = load_framework_strategy(
        root1, framework="vnpy", source_file="strategy.py", class_name="PkgStrategy", settings={}
    )
    assert s1.helper_value == 1
    assert "helpers" not in sys.modules, "bare-name helper import must not leak"
    assert not any(name.startswith("_nerya_compat.") for name in sys.modules)

    # A second package shipping its own helpers.py must see ITS file,
    # not the first package's cached module.
    root2 = tmp_path / "pkg_two"
    root2.mkdir()
    (root2 / "helpers.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root2 / "strategy.py").write_text(src, encoding="utf-8")

    s2 = load_framework_strategy(
        root2, framework="vnpy", source_file="strategy.py", class_name="PkgStrategy", settings={}
    )
    assert s2.helper_value == 2
    assert "helpers" not in sys.modules


# ---------------------------------------------------------------------------
# R2A6 — timed-out zombie cannot reduce / attach / delete / CAS
# ---------------------------------------------------------------------------


def test_timed_out_run_refuses_reduce_attach_delete_and_cas(tmp_path) -> None:
    deadline = StrategyRunDeadline()
    trading = StrategyTrading(
        config=_config(tmp_path),
        strategy_id="r2a6",
        policy=_policy(),
        accounts=("paper_main",),
        execution_mode="shadow",
        run_deadline=deadline,
    )
    state = StrategyState(store=StateStore(tmp_path / "state.json"), run_deadline=deadline)

    # Before the timeout the shadow paths still work.
    assert trading.reduce_position(market="mock:BTC/USDT", side="long")["status"] == "submitted"
    assert (
        trading.attach_protection(position_id="p1", market="mock:BTC/USDT", side="long")["status"]
        == "submitted"
    )
    state.set("k", 0)
    assert state.compare_and_set("k", expect=0, new_value=1) is True
    state.delete("k")
    assert state.get("k") is None

    deadline.trigger("max_run_seconds=5")
    with pytest.raises(StrategyRuntimeError, match="run timed out"):
        trading.reduce_position(market="mock:BTC/USDT", side="long")
    with pytest.raises(StrategyRuntimeError, match="run timed out"):
        trading.attach_protection(position_id="p1", market="mock:BTC/USDT", side="long")
    with pytest.raises(StrategyRuntimeError, match="run timed out"):
        state.compare_and_set("k", expect=None, new_value=1)
    with pytest.raises(StrategyRuntimeError, match="run timed out"):
        state.delete("k")


# ---------------------------------------------------------------------------
# R2A7 — backtest mock timeframe lookups are case-insensitive
# ---------------------------------------------------------------------------


def test_backtest_mock_timeframe_lookup_is_case_insensitive() -> None:
    rows15 = [
        {"ts": 1, "open": 99, "high": 101, "low": 98, "close": 100, "volume": 1},
        {"ts": 2, "open": 100, "high": 102, "low": 99, "close": 101, "volume": 2},
    ]
    primary = [dict(rows15[0], close=50.0)]

    # Mixed-case request against lowercase-keyed bars serves the real
    # 15m bars — the old case-exact lookup silently substituted the
    # primary bars instead of raising.
    provided = MockMarket(
        "MOCK:BTCUSDT",
        {"MOCK:BTCUSDT": primary},
        {"MOCK:BTCUSDT": {"15m": rows15}},
        primary_timeframe="1h",
    )
    assert provided.candles("MOCK:BTCUSDT", timeframe="15M", limit=1)[0]["close"] == 101.0

    # And the reverse: uppercase-keyed bars serve a lowercase request.
    upper_keys = MockMarket(
        "MOCK:BTCUSDT",
        {"MOCK:BTCUSDT": primary},
        {"MOCK:BTCUSDT": {"15M": rows15}},
        primary_timeframe="1h",
    )
    assert upper_keys.candles("MOCK:BTCUSDT", timeframe="15m", limit=2)[0]["close"] == 100.0

    # A genuinely missing foreign timeframe still fails loudly (the
    # message quotes the caller's original casing).
    missing = MockMarket("MOCK:BTCUSDT", {"MOCK:BTCUSDT": primary}, {}, primary_timeframe="1h")
    with pytest.raises(BacktestUnsupportedSurfaceError, match="15M"):
        missing.candles("MOCK:BTCUSDT", timeframe="15M", limit=2)


# ---------------------------------------------------------------------------
# R2A8 — corrupted state files are quarantined, not wiped
# ---------------------------------------------------------------------------


def test_state_store_quarantines_corrupt_file_instead_of_wiping(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")

    store = StateStore(path)
    assert store.get("pending_stops") is None

    # The next write must not silently persist an empty doc over the
    # corrupt file — the corrupt bytes are quarantined next to it.
    store.set("pending_stops", [{"vt_orderid": "STOP.1"}])
    quarantined = sorted(tmp_path.glob("state.json.corrupt-*"))
    assert len(quarantined) == 1
    assert "{not json" in quarantined[0].read_text(encoding="utf-8")
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["pending_stops"] == [{"vt_orderid": "STOP.1"}]

    # A non-dict JSON root is quarantined too instead of crashing .get.
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert store.get("pending_stops", "fallback") == "fallback"
    assert len(list(tmp_path.glob("state.json.corrupt-*"))) == 2

    # The store keeps working afterwards.
    store.set("after", 1)
    assert store.get("after") == 1
