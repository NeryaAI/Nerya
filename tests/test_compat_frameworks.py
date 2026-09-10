"""Framework-compat layer: detection + shims + adapters + importer.

The adapter tests drive real Freqtrade / VNpy strategy classes through
fake contexts with deterministic candle series; the e2e tests import a
foreign strategy file into a temp workspace and replay it through the
built-in backtest engine via the generated package entrypoint.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core import yaml_io
from nerya.core.paths import WorkspacePaths
from nerya.skills.builtin.backtest.scripts.mock_ctx import MockTrading
from nerya.strategies.result import ResultBuilder, StrategyResultStatus

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


FREQTRADE_SOURCE = '''
"""Demo freqtrade strategy used by the compat tests."""
from freqtrade.strategy import IStrategy


class DemoFtStrategy(IStrategy):
    timeframe = "1h"
    can_short = False
    stoploss = -0.10
    minimal_roi = {"0": 0.15}
    startup_candle_count = 10

    def populate_indicators(self, dataframe, metadata):
        dataframe["ma"] = dataframe["close"].rolling(3).mean()
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe.loc[dataframe["close"] > dataframe["ma"] * 1.02, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        dataframe.loc[dataframe["close"] < dataframe["ma"], "exit_long"] = 1
        return dataframe
'''

VNPY_SOURCE = '''
"""Demo vnpy strategy used by the compat tests."""
from vnpy_ctastrategy import CtaTemplate, BarData
from vnpy.trader.utility import ArrayManager


class DualMaStrategy(CtaTemplate):
    author = "nerya"
    parameters = ["fast_window", "slow_window", "fixed_size"]
    fast_window = 2
    slow_window = 3
    fixed_size = 1

    fast_ma = 0.0
    slow_ma = 0.0

    def on_init(self):
        self.write_log("dual ma init")
        self.load_bar(1)
        self.am = ArrayManager(5)

    def on_bar(self, bar: BarData):
        am = self.am
        am.update_bar(bar)
        if not am.inited:
            return
        self.fast_ma = am.sma(self.fast_window)
        self.slow_ma = am.sma(self.slow_window)
        if self.fast_ma > self.slow_ma:
            if self.pos == 0:
                self.buy(bar.close_price, self.fixed_size)
            elif self.pos < 0:
                self.cover(bar.close_price, self.fixed_size)
        elif self.fast_ma < self.slow_ma:
            if self.pos == 0:
                self.short(bar.close_price, self.fixed_size)
            elif self.pos > 0:
                self.sell(bar.close_price, self.fixed_size)
'''


def _workspace(tmp_path: Path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "paper_main",
                    "venue": "mock",
                    "exchange": "mock",
                    "mode": "paper",
                    "status": "active",
                    "initial_balance_usd": 10_000,
                }
            ]
        },
    )
    return cfg


def _candles(closes: list[float], *, start_ts: int = 1_700_000_000, step_s: int = 3_600) -> list[dict]:
    # ts in seconds — the unit Nerya candle rows standardise on; the
    # compat adapters must accept it (and milliseconds) interchangeably.
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


def _fake_ctx(candles: list[dict], *, limit: int = 990):
    # The REAL MockTrading: backtest replay answers submit_intent with a
    # terminal paper-equivalent "filled" envelope (the only status the
    # adapters treat as executed), so these unit tests exercise the same
    # contract the engine provides — no fake bridge in between.
    state = FakeState()
    return SimpleNamespace(
        market=SimpleNamespace(
            candles=lambda market, *a, timeframe="1m", limit=limit, **kw: list(candles)[-limit:]
        ),
        state=state,
        trading=MockTrading(
            [],
            "compat_test",
            state=state,
            mark_price=float(candles[-1]["close"]),
        ),
        policy=SimpleNamespace(default_order_usd=100.0),
        result=ResultBuilder(),
        config=SimpleNamespace(mode="paper", markets=("PAPER:BTCUSDT",)),
    )


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


def test_detect_framework_freqtrade() -> None:
    from nerya.strategies.compat.detect import detect_framework

    info = detect_framework(FREQTRADE_SOURCE, source_file="demo.py")
    assert info.framework == "freqtrade"
    assert info.class_name == "DemoFtStrategy"


def test_detect_framework_vnpy() -> None:
    from nerya.strategies.compat.detect import detect_framework

    info = detect_framework(VNPY_SOURCE, source_file="dual.py")
    assert info.framework == "vnpy"
    assert info.class_name == "DualMaStrategy"


def test_detect_framework_unknown_and_syntax_error() -> None:
    from nerya.strategies.compat.detect import detect_framework

    assert detect_framework("class Foo:\n    pass\n").framework == "unknown"
    assert detect_framework("def broken(:\n").framework == "unknown"


# ---------------------------------------------------------------------------
# vnpy shim units
# ---------------------------------------------------------------------------


def test_vnpy_shim_install_registers_modules() -> None:
    from nerya.strategies.compat.shims import install_vnpy_shims

    installed = install_vnpy_shims()
    assert installed or True  # idempotent either way
    import vnpy_ctastrategy  # noqa: F401
    from vnpy.trader.utility import ArrayManager, BarGenerator  # noqa: F401
    from vnpy_ctastrategy import CtaTemplate  # noqa: F401


def test_array_manager_indicators() -> None:
    from nerya.strategies.compat.shims.vnpy_shim import ArrayManager, BarData, Exchange, Interval
    from datetime import datetime

    am = ArrayManager(5)
    for i in range(5):
        am.update_bar(
            BarData(
                symbol="X",
                exchange=Exchange.LOCAL,
                datetime=datetime(2026, 1, 1, i),
                interval=Interval.MINUTE,
                open_price=float(i),
                high_price=float(i + 1),
                low_price=float(i - 1),
                close_price=float(i + 0.5),
                volume=10.0,
            )
        )
    assert am.inited
    # closes are [0.5, 1.5, 2.5, 3.5, 4.5] → sma(3) over last 3 = 3.5
    assert am.sma(3) == pytest.approx(3.5, abs=1e-6)
    assert am.atr(3) == pytest.approx(2.0, abs=1e-6)
    up, down, mid = am.boll(5)
    assert up > mid > down


def test_bar_generator_window_synthesis() -> None:
    from datetime import datetime, timedelta

    from nerya.strategies.compat.shims.vnpy_shim import BarData, BarGenerator, Exchange, Interval

    emitted: list[BarData] = []
    bg = BarGenerator(lambda b: None, window=5, on_window_bar=emitted.append)
    base = datetime(2026, 1, 1, 10, 7)
    for i in range(12):
        bg.update_bar(
            BarData(
                symbol="X",
                exchange=Exchange.LOCAL,
                datetime=base + timedelta(minutes=i),
                interval=Interval.MINUTE,
                open_price=1.0,
                high_price=1.0,
                low_price=1.0,
                close_price=1.0,
                volume=1.0,
            )
        )
    # The engine started mid-window (:07), so :07..:09 form a partial
    # window emitted when :10 arrives; :10..:14 form a full window
    # emitted when :15 arrives. :15.. has no :20 bar to close it.
    assert [b.datetime.strftime("%H:%M") for b in emitted] == ["10:07", "10:10"]
    assert emitted[-1].volume == pytest.approx(5.0)  # bars :10..:14
    assert bg.window_bar is not None
    assert bg.window_bar.volume == pytest.approx(4.0)  # bars :15..:18


# ---------------------------------------------------------------------------
# freqtrade adapter
# ---------------------------------------------------------------------------


def _load_ft_strategy(tmp_path: Path):
    from nerya.strategies.compat.entrypoint import load_framework_strategy

    source = tmp_path / "demo_ft.py"
    source.write_text(FREQTRADE_SOURCE, encoding="utf-8")
    return load_framework_strategy(
        tmp_path,
        framework="freqtrade",
        source_file="demo_ft.py",
        class_name="DemoFtStrategy",
        settings={},
    )


def test_freqtrade_adapter_entry_then_roi_exit(tmp_path: Path) -> None:
    from nerya.strategies.compat.freqtrade_adapter import run_freqtrade_tick

    strategy = _load_ft_strategy(tmp_path)
    # Mild uptrend then a sharp jump: only the last candle is > 2% above
    # its 3-bar MA, so the entry fires exactly there.
    closes = [100 + i for i in range(40)] + [139 * 1.10]
    candles = _candles(closes)
    settings = {"stake_amount": 100.0}

    ctx = _fake_ctx(candles)
    result = run_freqtrade_tick(ctx, strategy, market="PAPER:BTCUSDT", settings=settings)
    assert ctx.trading.pending_orders, "expected an entry intent on an uptrend"
    intent = ctx.trading.pending_orders[0]
    assert intent["side"] == "buy"
    assert intent["plan_action"] == "open_position"
    assert intent["size_unit"] == "usd"
    assert result.status is StrategyResultStatus.FILLED

    # Tick again on the same candles — must NOT duplicate the entry.
    ctx2 = _fake_ctx(candles)
    ctx2.state = ctx.state  # share state like the runner does across ticks
    run_freqtrade_tick(ctx2, strategy, market="PAPER:BTCUSDT", settings=settings)
    assert len(ctx2.trading.pending_orders) == 0

    # A new candle far above the entry (+15% ROI) must trigger the exit.
    candles_roi = _candles(closes + [closes[-1] * 1.20])
    ctx3 = _fake_ctx(candles_roi)
    ctx3.state = ctx.state
    result3 = run_freqtrade_tick(ctx3, strategy, market="PAPER:BTCUSDT", settings=settings)
    assert ctx3.trading.pending_orders, "expected ROI exit"
    exit_intent = ctx3.trading.pending_orders[0]
    assert exit_intent["side"] == "sell"
    assert exit_intent["plan_action"] == "close_position"
    assert result3.status is StrategyResultStatus.FILLED


def test_freqtrade_adapter_stoploss_exit(tmp_path: Path) -> None:
    from nerya.strategies.compat.freqtrade_adapter import run_freqtrade_tick

    strategy = _load_ft_strategy(tmp_path)
    closes = [100 + i for i in range(40)] + [139 * 1.10]
    candles = _candles(closes)
    settings = {"stake_amount": 100.0}

    ctx = _fake_ctx(candles)
    run_freqtrade_tick(ctx, strategy, market="PAPER:BTCUSDT", settings=settings)
    assert ctx.trading.pending_orders

    # Crash below the -10% stoploss.
    crash = _candles(closes + [closes[-1] * 0.80])
    ctx2 = _fake_ctx(crash)
    ctx2.state = ctx.state
    run_freqtrade_tick(ctx2, strategy, market="PAPER:BTCUSDT", settings=settings)
    assert ctx2.trading.pending_orders, "expected stoploss exit"
    assert ctx2.trading.pending_orders[0]["plan_action"] == "close_position"


def test_freqtrade_adapter_hold_without_signal(tmp_path: Path) -> None:
    from nerya.strategies.compat.freqtrade_adapter import run_freqtrade_tick

    strategy = _load_ft_strategy(tmp_path)
    closes = [100.0, 100.2, 100.1, 100.15, 100.05]  # flat — no ma cross
    ctx = _fake_ctx(_candles(closes))
    result = run_freqtrade_tick(ctx, strategy, market="PAPER:BTCUSDT", settings={})
    assert result.status is StrategyResultStatus.HOLD
    assert ctx.trading.pending_orders == []


# ---------------------------------------------------------------------------
# vnpy adapter
# ---------------------------------------------------------------------------


def _load_vnpy_strategy(tmp_path: Path):
    from nerya.strategies.compat.entrypoint import load_framework_strategy

    source = tmp_path / "dual_ma.py"
    source.write_text(VNPY_SOURCE, encoding="utf-8")
    return load_framework_strategy(
        tmp_path,
        framework="vnpy",
        source_file="dual_ma.py",
        class_name="DualMaStrategy",
        settings={},
    )


def test_vnpy_adapter_init_then_short_and_recover(tmp_path: Path) -> None:
    from nerya.strategies.compat.vnpy_adapter import run_vnpy_tick

    strategy = _load_vnpy_strategy(tmp_path)
    # Warm rise → inited with a long position via golden cross.
    closes = [100 + i * 0.5 for i in range(12)]
    candles = _candles(closes, step_s=60)
    settings = {"bar_timeframe": "1m", "init_bars": 500}

    ctx = _fake_ctx(candles)
    result = run_vnpy_tick(ctx, strategy, market="PAPER:BTCUSDT", settings=settings)
    assert result.status is StrategyResultStatus.OK
    assert result.reason == "vnpy_compat_inited"
    # Warmup happened before trading=True → no orders during init.
    assert ctx.trading.pending_orders == []

    # New rising bar keeps the long position alive via a fresh buy.
    candles2 = _candles(closes + [closes[-1] + 1.0], step_s=60)
    ctx2 = _fake_ctx(candles2)
    ctx2.state = ctx.state
    run_vnpy_tick(ctx2, strategy, market="PAPER:BTCUSDT", settings=settings)
    assert ctx2.trading.pending_orders, "expected a long entry on the new bar"
    assert ctx2.trading.pending_orders[0]["side"] == "buy"
    assert ctx2.trading.pending_orders[0]["plan_action"] == "open_position"
    assert ctx2.trading.pending_orders[0]["size_unit"] == "base"

    # Rolling over → dead cross over two new bars in one tick: first
    # the long is closed, then a short is opened.
    falling = closes + [closes[-1] + 1.0, 90.0, 80.0]
    candles3 = _candles(falling, step_s=60)
    ctx3 = _fake_ctx(candles3)
    ctx3.state = ctx.state
    run_vnpy_tick(ctx3, strategy, market="PAPER:BTCUSDT", settings=settings)
    actions = [(i["side"], i["plan_action"]) for i in ctx3.trading.pending_orders]
    assert actions == [
        ("sell", "close_position"), # dead cross closes the long
        ("sell", "open_position"),  # then opens the short
    ]
    state3 = ctx3.state.get("_compat_vnpy")
    assert state3["pos"] == -1


def test_vnpy_adapter_no_new_bars_hold(tmp_path: Path) -> None:
    from nerya.strategies.compat.vnpy_adapter import run_vnpy_tick

    strategy = _load_vnpy_strategy(tmp_path)
    closes = [100 + i for i in range(10)]
    candles = _candles(closes, step_s=60)
    ctx = _fake_ctx(candles)
    run_vnpy_tick(ctx, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})
    ctx2 = _fake_ctx(candles)
    ctx2.state = ctx.state
    # Warm-up excludes the newest bar, so tick 2 processes it with
    # trading on (a fresh-candle buy on this rising series).
    run_vnpy_tick(ctx2, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})
    ctx3 = _fake_ctx(candles)
    ctx3.state = ctx.state
    result = run_vnpy_tick(ctx3, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})
    assert result.status is StrategyResultStatus.HOLD
    assert result.reason == "vnpy_compat_no_new_bars"


# ---------------------------------------------------------------------------
# importer + backtest e2e
# ---------------------------------------------------------------------------


def test_import_freqtrade_and_backtest(tmp_path: Path) -> None:
    from nerya.strategies.compat.importer import import_external_strategy
    from nerya.skills.builtin.backtest.scripts.config import load_config
    from nerya.skills.builtin.backtest.scripts.engine import _load_run_fn, run_backtest
    from nerya.skills.builtin.backtest.scripts.metrics import assemble_metrics

    cfg_ws = _workspace(tmp_path)
    source = tmp_path / "upload" / "demo_ft.py"
    source.parent.mkdir(parents=True)
    source.write_text(FREQTRADE_SOURCE, encoding="utf-8")

    imported = import_external_strategy(
        cfg_ws,
        source,
        strategy_id="demo_ft",
        markets=["PAPER:BTCUSDT"],
        timeframe="1h",
        stake_amount=100.0,
    )
    assert imported.framework == "freqtrade"
    assert imported.class_name == "DemoFtStrategy"
    assert imported.validation_ok, imported.validation_issues

    package_dir = Path(imported.package_dir)
    assert (package_dir / "strategy.yml").exists()
    assert (package_dir / "main.py").exists()
    assert (package_dir / "demo_ft.py").exists()
    manifest = yaml_io.load(package_dir / "strategy.yml")
    assert manifest["extras"]["framework"]["class"] == "DemoFtStrategy"
    assert manifest["entrypoint"] == "main.py:run"

    # Replay through the generated main.py entrypoint using the built-in
    # engine — proves the compat package is an ordinary package.
    run_fn = _load_run_fn(package_dir)
    # Oscillating series: the 3-bar MA lags the swings, so entries and
    # exits both fire during the replay.
    closes = [100.0 + 10.0 * ((-1) ** (i // 3)) + i * 0.05 for i in range(60)]
    candles = _candles(closes)
    cfg = load_config(
        preset="default",
        markets=["PAPER:BTCUSDT"],
        overrides={
            "window_days": 3,
            "tf": "1h",
            "fee_bps_by_venue": {"PAPER": 0.0, "MOCK": 0.0, "BINANCE": 0.0},
            "slip_bps_by_venue": {"PAPER": 0.0, "MOCK": 0.0, "BINANCE": 0.0},
        },
    )
    result = run_backtest(None, cfg, candles_by_market={"PAPER:BTCUSDT": candles}, run_fn=run_fn)
    metrics = assemble_metrics(result)
    assert metrics["total_trades"] > 0, metrics
    assert metrics["final_equity_usd"] > 0

    # R3S1: the mock's terminal "filled" envelopes must let the imported
    # strategy register its position and manage its own exits. A replay
    # whose only exit is an engine forced_close means the adapter never
    # tracked a position (the pre-fix failure mode).
    managed_exits = [
        t
        for t in result.trades
        if not t.get("forced_close") and str(t.get("reason") or "").startswith("freqtrade exit")
    ]
    assert managed_exits, (
        "imported freqtrade strategy never managed an exit in replay "
        "(only engine forced_close ran): "
        f"{[(t.get('side'), t.get('reason'), t.get('forced_close')) for t in result.trades]}"
    )
    managed_entries = [
        t
        for t in result.trades
        if not t.get("forced_close") and str(t.get("reason") or "").startswith("freqtrade entry")
    ]
    assert managed_entries, "expected at least one strategy-managed entry"


def test_imported_package_runs_through_strategy_runner(tmp_path: Path) -> None:
    """The live runner path: build_strategy_context → compat entrypoint."""

    from nerya.strategies.compat.importer import import_external_strategy
    from nerya.strategies.runner import StrategyRunner

    cfg = _workspace(tmp_path)
    source = tmp_path / "demo_ft.py"
    source.write_text(FREQTRADE_SOURCE, encoding="utf-8")
    imported = import_external_strategy(
        cfg,
        source,
        strategy_id="runner_demo_ft",
        markets=["PAPER:BTCUSDT"],
        timeframe="1h",
    )
    assert imported.validation_ok

    record = StrategyRunner(config=cfg).run_tick("runner_demo_ft")
    assert record.status in {"ok", "hold", "submitted"}, record.outputs
    result = record.outputs["result"]
    # The compat entrypoint must have executed (not crashed): either it
    # ran to a terminal status, or it surfaced an error result — never a
    # runner-level exception.
    assert result["status"] in {"ok", "hold", "submitted", "error"}
    if result["status"] == "error":
        pytest.fail(f"compat tick errored under the runner: {result['reason']}")


def test_import_vnpy_strategy_package(tmp_path: Path) -> None:
    from nerya.strategies.compat.importer import import_external_strategy

    cfg = _workspace(tmp_path)
    source = tmp_path / "upload" / "dual_ma.py"
    source.parent.mkdir(parents=True)
    source.write_text(VNPY_SOURCE, encoding="utf-8")

    imported = import_external_strategy(
        cfg,
        source,
        strategy_id="dual_ma_vn",
        markets=["PAPER:BTCUSDT"],
        timeframe="1m",
    )
    assert imported.framework == "vnpy"
    assert imported.validation_ok, imported.validation_issues
    manifest = yaml_io.load(Path(imported.package_dir) / "strategy.yml")
    block = manifest["extras"]["framework"]
    assert block["bar_timeframe"] == "1m"
    assert block["settings"] == {"fast_window": 2, "slow_window": 3, "fixed_size": 1}


def test_import_rejects_unknown_framework(tmp_path: Path) -> None:
    from nerya.core.errors import TradingError
    from nerya.strategies.compat.importer import import_external_strategy

    cfg = _workspace(tmp_path)
    source = tmp_path / "plain.py"
    source.write_text("class Nope:\n    pass\n", encoding="utf-8")
    with pytest.raises(TradingError):
        import_external_strategy(cfg, source)


def test_import_refuses_overwrite_without_flag(tmp_path: Path) -> None:
    from nerya.core.errors import TradingError
    from nerya.strategies.compat.importer import import_external_strategy

    cfg = _workspace(tmp_path)
    source = tmp_path / "demo_ft.py"
    source.write_text(FREQTRADE_SOURCE, encoding="utf-8")
    import_external_strategy(cfg, source, strategy_id="demo_ft")
    with pytest.raises(TradingError):
        import_external_strategy(cfg, source, strategy_id="demo_ft")


# ---------------------------------------------------------------------------
# native agent tool
# ---------------------------------------------------------------------------


def test_native_tool_imports_external_strategy(tmp_path: Path) -> None:
    from nerya.tools.native.strategy_runtime import strategy_import_external_handler
    from nerya.tools.types import ToolCall

    cfg = _workspace(tmp_path)
    source = tmp_path / "upload" / "dual_ma.py"
    source.parent.mkdir(parents=True)
    source.write_text(VNPY_SOURCE, encoding="utf-8")

    call = ToolCall(
        name="strategy_import_external",
        arguments={
            "sources": [str(source)],
            "strategy_id": "tool_dual_ma",
            "framework": "auto",
            "markets": ["PAPER:BTCUSDT"],
        },
    )
    result = strategy_import_external_handler(call, config=cfg)
    assert result.content[0].data["strategy_id"] == "tool_dual_ma"
    assert result.content[0].data["framework"] == "vnpy"
    assert result.content[0].data["validation_ok"] is True

    # Unknown source → error result, not an exception.
    bad = ToolCall(
        name="strategy_import_external",
        arguments={"sources": [str(tmp_path / "missing.py")]},
    )
    bad_result = strategy_import_external_handler(bad, config=cfg)
    assert bad_result.is_error or not bad_result.content[0].data


def test_sdk_import_external(tmp_path: Path) -> None:
    from nerya.sdk.strategy_api import StrategyAPI
    from nerya.skills.kernel import SkillKernel

    cfg = _workspace(tmp_path)
    source = tmp_path / "demo_ft.py"
    source.write_text(FREQTRADE_SOURCE, encoding="utf-8")

    api = StrategyAPI(config=cfg, skills=SkillKernel.boot(cfg))
    out = api.import_external(
        str(source),
        strategy_id="sdk_demo_ft",
        markets=["PAPER:BTCUSDT"],
    )
    assert out["framework"] == "freqtrade"
    assert out["validation_ok"] is True
    assert Path(out["package_dir"]).exists()
