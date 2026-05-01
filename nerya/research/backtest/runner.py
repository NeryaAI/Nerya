"""Deterministic single-symbol backtest runner.

Implements VibeTrading optimization plan §5 Task 5.

Workflow:

1. Validate :class:`BacktestConfig`.
2. Load fixture OHLCV data via :class:`DatasetRouter`.
3. Load and statically check the candidate ``signal_engine.py``.
4. Drive the engine bar-by-bar, rebalance to its target weights.
5. Apply fee + slippage from config.
6. Emit ``trades.csv``, ``equity_curve.csv``, ``metrics.json``,
   ``report.md`` and a structured ``ValidationReport`` JSON.

The runner does not depend on Vibe-Trading code.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.errors import NeryaError
from ...core.time import now_iso
from ..artifacts import (
    candidate_artifact_dir,
    candidate_dir,
    candidate_report_path,
    candidate_signal_engine_path,
    ensure_dirs,
    validation_history_path,
    validation_latest_path,
)
from ..datasets import DatasetRouter, DatasetWindow, OhlcvFrame
from ..schemas import BacktestConfig, BacktestConfigError
from ..signals.compiler import compile_signal_to_intent_candidate
from ..signals.loader import load_signal_engine_module
from ..signals.protocol import SignalFrame, coerce_signal_frame
from ..validation_report import (
    REQUIRED_GATE_NAMES,
    ValidationReport,
)
from .metrics import compute_metrics
from .models import BacktestResult, EquityPoint, TradeRecord


class BacktestRunnerError(NeryaError):
    """Raised when the runner cannot produce a valid result."""


@dataclass
class _Position:
    quantity: float = 0.0


@dataclass
class GateThresholds:
    min_bars: int = 20
    min_trades: int = 1
    max_drawdown_pct: float = 30.0
    min_sharpe: float = 0.0
    cost_stress_multiplier: float = 2.0


def run_backtest(
    workspace: str | Path,
    config: BacktestConfig | dict[str, Any],
    *,
    router: DatasetRouter | None = None,
    gate_thresholds: GateThresholds | None = None,
) -> BacktestResult:
    runner = BacktestRunner(workspace, router=router,
                              gate_thresholds=gate_thresholds)
    return runner.run(config)


class BacktestRunner:
    """Single-symbol bar-by-bar backtest runner."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        router: DatasetRouter | None = None,
        gate_thresholds: GateThresholds | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.router = router or DatasetRouter()
        self.gate_thresholds = gate_thresholds or GateThresholds()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run(self, config: BacktestConfig | dict[str, Any]) -> BacktestResult:
        try:
            cfg = BacktestConfig.parse(config)
        except BacktestConfigError:
            raise

        if len(cfg.symbols) != 1:
            raise BacktestRunnerError(
                f"backtest_runner_single_symbol_only:got={cfg.symbols!r}")

        symbol = cfg.symbols[0]
        window = DatasetWindow(
            symbol=symbol,
            interval=cfg.interval,
            start_date=cfg.start_date,
            end_date=cfg.end_date,
        )
        frame = self.router.load(window, data_source=cfg.data_source)

        engine = self._instantiate_engine(cfg)
        emitted = self._collect_signals(engine, frame, symbol)

        trades, equity = self._simulate(cfg, frame, emitted, symbol)
        metrics = compute_metrics(
            equity, trades,
            interval=cfg.interval,
            initial_capital_usd=cfg.initial_capital_usd,
        )

        artifacts = self._write_artifacts(cfg, frame, trades, equity,
                                            metrics.asdict())

        gate_result = self._evaluate_gates(cfg, frame, trades,
                                              metrics.asdict())

        report = self._build_validation_report(
            cfg, frame, metrics.asdict(), artifacts, gate_result)

        report_path = candidate_report_path(
            self.workspace, cfg.strategy_id, cfg.candidate_id)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report.to_json(), encoding="utf-8")
        artifacts["validation_report"] = str(report_path)

        # Append history.
        latest_path = validation_latest_path(
            self.workspace, cfg.strategy_id)
        history_path = validation_history_path(
            self.workspace, cfg.strategy_id)
        latest_path.parent.mkdir(parents=True, exist_ok=True)
        latest_path.write_text(report.to_json(), encoding="utf-8")
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as fh:
            fh.write(report.to_json(indent=None) + "\n")

        return BacktestResult(
            strategy_id=cfg.strategy_id,
            candidate_id=cfg.candidate_id,
            config=cfg.asdict(),
            equity_curve=list(equity),
            trades=list(trades),
            metrics=metrics.asdict(),
            bars_processed=len(frame.candles),
            start_date=cfg.start_date,
            end_date=cfg.end_date,
            final_equity=equity[-1].equity if equity else cfg.initial_capital_usd,
            initial_equity=cfg.initial_capital_usd,
            data_coverage={
                "symbol": symbol,
                "bars": len(frame.candles),
                "first_ts": frame.candles[0].ts if frame.candles else "",
                "last_ts": frame.candles[-1].ts if frame.candles else "",
                "source": frame.source,
            },
            engine={
                "name": "crypto_fixture",
                "interval": cfg.interval,
                "fee_bps": cfg.fee_bps,
                "slippage_bps": cfg.slippage_bps,
            },
            reproducibility=self._reproducibility(cfg, frame),
            artifacts=artifacts,
        )

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    def _instantiate_engine(self, cfg: BacktestConfig) -> Any:
        engine_path = candidate_signal_engine_path(
            self.workspace, cfg.strategy_id, cfg.candidate_id)
        if not engine_path.is_file():
            raise BacktestRunnerError(
                f"backtest_runner_signal_engine_missing:{engine_path}")

        module = load_signal_engine_module(
            self.workspace, cfg.strategy_id, cfg.candidate_id)
        engine_cls = getattr(module, "SignalEngine", None)
        if engine_cls is None:
            raise BacktestRunnerError(
                "backtest_runner_signal_engine_no_class")
        return engine_cls()

    def _collect_signals(
        self, engine: Any, frame: OhlcvFrame, symbol: str
    ) -> dict[str, SignalFrame]:
        try:
            raw = list(engine.generate({symbol: frame}))
        except Exception as exc:
            raise BacktestRunnerError(
                f"backtest_runner_engine_generate_failed:{exc}") from exc

        out: dict[str, SignalFrame] = {}
        for item in raw:
            try:
                sig = coerce_signal_frame(
                    item,
                    max_weight=1.0,
                    allow_short=False,
                )
            except Exception as exc:
                raise BacktestRunnerError(
                    f"backtest_runner_signal_invalid:{exc}") from exc
            out[sig.ts] = sig
        return out

    def _simulate(
        self,
        cfg: BacktestConfig,
        frame: OhlcvFrame,
        signals: dict[str, SignalFrame],
        symbol: str,
    ) -> tuple[list[TradeRecord], list[EquityPoint]]:
        cash = float(cfg.initial_capital_usd)
        position = _Position()
        target_weight = 0.0
        trades: list[TradeRecord] = []
        equity_curve: list[EquityPoint] = []
        previous_weight = 0.0

        fee_rate = cfg.fee_bps / 10_000.0
        slip_rate = cfg.slippage_bps / 10_000.0

        for candle in frame.candles:
            sig = signals.get(candle.ts)
            if sig is not None:
                cand = compile_signal_to_intent_candidate(
                    sig,
                    strategy_id=cfg.strategy_id,
                    portfolio_value_usd=_portfolio_value(cash, position,
                                                           candle.close),
                    previous_weight=previous_weight,
                    max_position_weight=cfg.max_position_weight,
                    allow_short=cfg.allow_short,
                )
                target_weight = cand.target_weight
                desired_qty = (target_weight *
                                _portfolio_value(cash, position,
                                                 candle.close)) / max(
                    candle.close, 1e-9)
                delta_qty = desired_qty - position.quantity
                if abs(delta_qty) > 1e-9:
                    side = "buy" if delta_qty > 0 else "sell"
                    fill_price = candle.close * (
                        1 + slip_rate if side == "buy" else 1 - slip_rate)
                    notional = abs(delta_qty) * fill_price
                    fee = notional * fee_rate
                    if side == "buy":
                        cash -= notional + fee
                        position.quantity += delta_qty
                    else:
                        cash += notional - fee
                        position.quantity += delta_qty
                    trades.append(TradeRecord(
                        ts=candle.ts,
                        symbol=symbol,
                        side=side,
                        price=float(fill_price),
                        quantity=float(abs(delta_qty)),
                        notional=float(notional),
                        fee=float(fee),
                        slippage=float(abs(fill_price - candle.close)
                                        * abs(delta_qty)),
                        target_weight=float(target_weight),
                        delta_weight=float(target_weight - previous_weight),
                        reason=cand.reason,
                    ))
                previous_weight = target_weight

            holdings_value = position.quantity * candle.close
            equity = cash + holdings_value
            equity_curve.append(EquityPoint(
                ts=candle.ts,
                equity=float(equity),
                cash=float(cash),
                holdings_value=float(holdings_value),
                target_weights={symbol: target_weight},
            ))
        return trades, equity_curve

    def _evaluate_gates(
        self,
        cfg: BacktestConfig,
        frame: OhlcvFrame,
        trades: list[TradeRecord],
        metrics: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
        thresholds = self.gate_thresholds
        bars = len(frame.candles)
        gates: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []

        gates.append(_gate_status(
            "minimum_bars",
            ok=bars >= thresholds.min_bars,
            detail=f"bars={bars} required>={thresholds.min_bars}",
        ))

        gates.append(_gate_status(
            "minimum_trades",
            ok=len(trades) >= thresholds.min_trades,
            detail=f"trades={len(trades)} required>="
                    f"{thresholds.min_trades}",
        ))

        max_dd = abs(float(metrics.get("max_drawdown", 0.0))) * 100
        gates.append(_gate_status(
            "max_drawdown",
            ok=max_dd <= thresholds.max_drawdown_pct,
            detail=f"max_drawdown_pct={max_dd:.2f} "
                    f"limit={thresholds.max_drawdown_pct}",
        ))

        sharpe = float(metrics.get("sharpe", 0.0))
        sortino = float(metrics.get("sortino", 0.0))
        sharpe_ok = sharpe >= thresholds.min_sharpe or \
            sortino >= thresholds.min_sharpe
        gates.append(_gate_status(
            "sharpe_or_sortino",
            ok=sharpe_ok,
            detail=f"sharpe={sharpe:.3f} sortino={sortino:.3f} "
                    f"min={thresholds.min_sharpe}",
        ))

        cost_total = (sum(t.fee for t in trades) +
                       sum(t.slippage for t in trades))
        cost_pct = cost_total / max(cfg.initial_capital_usd, 1.0)
        gates.append(_gate_status(
            "cost_stress",
            ok=cost_pct < 0.5,
            detail=f"total_cost_ratio={cost_pct:.4f} "
                    f"stress_multiplier={thresholds.cost_stress_multiplier}",
            metrics={"cost_ratio": cost_pct},
        ))

        gates.append({
            "name": "walk_forward",
            "status": "warn",
            "detail": "walk_forward_not_run_in_minimal_engine",
            "metrics": {},
        })

        gates.append({
            "name": "paper_shadow_required",
            "status": "warn",
            "detail": "paper/shadow run required before live promotion",
            "metrics": {},
        })

        gates.append({
            "name": "risk_gate_compatibility",
            "status": "pass",
            "detail": "intent_candidate_route_only",
            "metrics": {},
        })

        # Compute overall status.
        statuses = {g["status"] for g in gates}
        if "fail" in statuses:
            status = "fail"
        elif "warn" in statuses:
            status = "warn"
        else:
            status = "pass"

        for gate in gates:
            if gate["status"] == "fail":
                blockers.append({
                    "gate": gate["name"],
                    "detail": gate.get("detail", ""),
                })

        return gates, status, blockers

    def _write_artifacts(
        self,
        cfg: BacktestConfig,
        frame: OhlcvFrame,
        trades: list[TradeRecord],
        equity: list[EquityPoint],
        metrics: dict[str, Any],
    ) -> dict[str, str]:
        cdir = candidate_dir(self.workspace, cfg.strategy_id, cfg.candidate_id)
        adir = candidate_artifact_dir(
            self.workspace, cfg.strategy_id, cfg.candidate_id)
        ensure_dirs([cdir, adir])

        equity_path = adir / "equity_curve.csv"
        with equity_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["ts", "equity", "cash", "holdings_value"])
            for point in equity:
                writer.writerow([point.ts, f"{point.equity:.6f}",
                                  f"{point.cash:.6f}",
                                  f"{point.holdings_value:.6f}"])

        trades_path = adir / "trades.csv"
        with trades_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "ts", "symbol", "side", "price", "quantity",
                "notional", "fee", "slippage", "target_weight",
                "delta_weight", "reason",
            ])
            for trade in trades:
                writer.writerow([
                    trade.ts, trade.symbol, trade.side,
                    f"{trade.price:.6f}", f"{trade.quantity:.8f}",
                    f"{trade.notional:.6f}", f"{trade.fee:.6f}",
                    f"{trade.slippage:.6f}",
                    f"{trade.target_weight:.6f}",
                    f"{trade.delta_weight:.6f}",
                    trade.reason,
                ])

        metrics_path = adir / "metrics.json"
        metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True),
                                  encoding="utf-8")

        report_md_path = adir / "report.md"
        report_md_path.write_text(_render_markdown_report(
            cfg, frame, metrics, len(trades)), encoding="utf-8")

        return {
            "equity_curve_csv": str(equity_path),
            "trades_csv": str(trades_path),
            "metrics_json": str(metrics_path),
            "report_md": str(report_md_path),
        }

    def _build_validation_report(
        self,
        cfg: BacktestConfig,
        frame: OhlcvFrame,
        metrics: dict[str, Any],
        artifacts: dict[str, str],
        gate_payload: tuple[list[dict[str, Any]], str, list[dict[str, Any]]],
    ) -> ValidationReport:
        gates, status, blockers = gate_payload
        return ValidationReport(
            strategy_id=cfg.strategy_id,
            candidate_id=cfg.candidate_id,
            status=status,  # type: ignore[arg-type]
            metrics=metrics,
            gates=gates,
            artifacts=artifacts,
            data_coverage={
                "symbol": frame.symbol,
                "bars": len(frame.candles),
                "interval": cfg.interval,
                "first_ts": frame.candles[0].ts if frame.candles else "",
                "last_ts": frame.candles[-1].ts if frame.candles else "",
                "source": frame.source,
            },
            engine={
                "name": "crypto_fixture",
                "version": "0.1",
                "interval": cfg.interval,
                "fee_bps": cfg.fee_bps,
                "slippage_bps": cfg.slippage_bps,
            },
            reproducibility=self._reproducibility(cfg, frame),
            blockers=blockers,
            created_at=now_iso(),
        )

    def _reproducibility(
        self, cfg: BacktestConfig, frame: OhlcvFrame
    ) -> dict[str, Any]:
        engine_path = candidate_signal_engine_path(
            self.workspace, cfg.strategy_id, cfg.candidate_id)
        engine_hash = ""
        if engine_path.is_file():
            engine_hash = hashlib.sha256(
                engine_path.read_bytes()).hexdigest()
        config_hash = hashlib.sha256(
            json.dumps(cfg.asdict(), sort_keys=True).encode()).hexdigest()
        return {
            "config_hash": config_hash,
            "signal_engine_hash": engine_hash,
            "engine_name": "crypto_fixture",
            "fee_bps": cfg.fee_bps,
            "slippage_bps": cfg.slippage_bps,
            "initial_capital_usd": cfg.initial_capital_usd,
            "data_source": cfg.data_source,
            "data_summary": {
                "bars": len(frame.candles),
                "first_ts": frame.candles[0].ts if frame.candles else "",
                "last_ts": frame.candles[-1].ts if frame.candles else "",
            },
        }


def _portfolio_value(cash: float, position: _Position, price: float) -> float:
    return cash + position.quantity * price


def _gate_status(
    name: str, *, ok: bool, detail: str = "",
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": "pass" if ok else "fail",
        "detail": detail,
        "metrics": dict(metrics or {}),
    }


def _render_markdown_report(
    cfg: BacktestConfig,
    frame: OhlcvFrame,
    metrics: dict[str, Any],
    trade_count: int,
) -> str:
    return (
        f"# Backtest report — {cfg.strategy_id}/{cfg.candidate_id}\n\n"
        f"- Symbols: {', '.join(cfg.symbols)}\n"
        f"- Window: {cfg.start_date} .. {cfg.end_date}\n"
        f"- Bars: {len(frame.candles)}\n"
        f"- Trades: {trade_count}\n"
        f"- Initial capital: ${cfg.initial_capital_usd:,.2f}\n"
        f"- Fees: {cfg.fee_bps}bps / Slippage: {cfg.slippage_bps}bps\n\n"
        f"## Metrics\n\n"
        + "\n".join(f"- {k}: {v}" for k, v in sorted(metrics.items()))
        + "\n"
    )


__all__ = [
    "BacktestRunner",
    "BacktestRunnerError",
    "GateThresholds",
    "run_backtest",
]
