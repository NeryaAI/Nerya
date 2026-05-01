"""Shadow runtime — drive a candidate's signal engine against fixture
data without ever writing to paper/live ledgers.

VibeTrading deep optimization plan §5 Task 8.

The runtime intentionally **reuses** the research signal/compiler stack
that the backtest runner uses, then captures the per-bar decisions as
``ShadowEvent`` and ``ShadowFill`` rows under the strategy's shadow
directory. It does **not** call into ``trading.RiskGate``; instead it
runs a lightweight compatibility check (``risk_compat_check``) to count
risk rejections without mutating runtime state.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from ...core.errors import NeryaError
from ..artifacts import candidate_signal_engine_path
from ..datasets import DatasetRouter, DatasetWindow, OhlcvFrame
from ..schemas import BacktestConfig, BacktestConfigError
from ..signals.compiler import (
    IntentCandidate,
    compile_signal_to_intent_candidate,
)
from ..signals.loader import load_signal_engine_module
from ..signals.protocol import SignalFrame, coerce_signal_frame
from .models import ShadowEvent, ShadowFill, ShadowReport, ShadowRun
from .store import ShadowStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ShadowRuntimeError(NeryaError):
    """Raised when the shadow runtime cannot finish a run."""


@dataclass(slots=True)
class _Position:
    quantity: float = 0.0


# ----------------------------------------------------------------------
# Public entry points
# ----------------------------------------------------------------------


def run_shadow(
    workspace: Path,
    config: BacktestConfig | dict[str, Any],
    *,
    router: DatasetRouter | None = None,
    risk_compat_check=None,
    run_id: Optional[str] = None,
) -> ShadowReport:
    """One-shot helper. See :class:`ShadowRuntime` for a stateful API."""

    runtime = ShadowRuntime(
        workspace=Path(workspace),
        router=router,
        risk_compat_check=risk_compat_check,
    )
    return runtime.run(config, run_id=run_id)


# ----------------------------------------------------------------------
# Stateful runtime
# ----------------------------------------------------------------------


class ShadowRuntime:
    """Drive a strategy candidate against fixture market data.

    ``risk_compat_check`` is an optional callable that returns ``True``
    when the candidate intent would be accepted by Risk Gate semantics
    and ``False`` otherwise. The runtime calls it for every non-zero
    intent so the report can attribute risk rejections without mutating
    Risk Gate state. Tests pass a fake checker; the production wiring
    will pass a thin wrapper around the real RiskGate that runs in
    "evaluate-only" mode.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        router: DatasetRouter | None = None,
        store: ShadowStore | None = None,
        risk_compat_check=None,
    ) -> None:
        self.workspace = Path(workspace)
        self.router = router or DatasetRouter()
        self.store = store or ShadowStore(self.workspace)
        self._risk_compat_check = risk_compat_check or (lambda *_: True)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run(
        self,
        config: BacktestConfig | dict[str, Any],
        *,
        run_id: Optional[str] = None,
    ) -> ShadowReport:
        try:
            cfg = BacktestConfig.parse(config)
        except BacktestConfigError:
            raise

        if len(cfg.symbols) != 1:
            raise ShadowRuntimeError(
                f"shadow_runtime_single_symbol_only:got={cfg.symbols!r}")

        run_id = run_id or self._mint_run_id()
        run = ShadowRun(
            run_id=run_id,
            strategy_id=cfg.strategy_id,
            candidate_id=cfg.candidate_id,
            started_at=_now_iso(),
            status="running",
            config=cfg.asdict(),
        )
        run = self.store.create(run)

        try:
            report = self._execute(cfg, run)
        except Exception as exc:
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            run.finished_at = _now_iso()
            self.store.update(run)
            raise

        run.status = "completed"
        run.finished_at = _now_iso()
        self.store.update(run)
        return report

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _execute(
        self, cfg: BacktestConfig, run: ShadowRun,
    ) -> ShadowReport:
        symbol = cfg.symbols[0]
        window = DatasetWindow(
            symbol=symbol,
            interval=cfg.interval,
            start_date=cfg.start_date,
            end_date=cfg.end_date,
        )
        frame = self.router.load(window, data_source=cfg.data_source)
        engine = self._instantiate_engine(cfg)
        signals = self._collect_signals(engine, frame, symbol)

        events_emitted = 0
        intents_emitted = 0
        risk_rejections = 0
        virtual_fills: list[ShadowFill] = []
        cash = float(cfg.initial_capital_usd)
        position = _Position()
        previous_weight = 0.0

        fee_rate = cfg.fee_bps / 10_000.0
        slip_rate = cfg.slippage_bps / 10_000.0

        for candle in frame.candles:
            sig = signals.get(candle.ts)
            if sig is None:
                continue

            self.store.append_event(
                cfg.strategy_id, run.run_id,
                ShadowEvent(
                    ts=candle.ts,
                    kind="signal",
                    symbol=symbol,
                    detail={
                        "target_weight": sig.target_weight,
                        "confidence": sig.confidence,
                        "reason": sig.reason,
                    },
                ).asdict(),
            )
            events_emitted += 1

            cand = compile_signal_to_intent_candidate(
                sig,
                strategy_id=cfg.strategy_id,
                portfolio_value_usd=_portfolio_value(
                    cash, position, candle.close),
                previous_weight=previous_weight,
                max_position_weight=cfg.max_position_weight,
                allow_short=cfg.allow_short,
            )
            self.store.append_event(
                cfg.strategy_id, run.run_id,
                ShadowEvent(
                    ts=candle.ts,
                    kind="intent",
                    symbol=symbol,
                    detail={
                        "side": cand.side,
                        "target_weight": cand.target_weight,
                        "notional_usd_estimate": cand.notional_usd_estimate,
                        "confidence": cand.confidence,
                    },
                ).asdict(),
            )
            intents_emitted += 1

            allowed = bool(self._risk_compat_check(cand))
            self.store.append_event(
                cfg.strategy_id, run.run_id,
                ShadowEvent(
                    ts=candle.ts,
                    kind="risk_decision",
                    symbol=symbol,
                    detail={
                        "decision": "approve" if allowed else "reject",
                    },
                ).asdict(),
            )

            if not allowed:
                risk_rejections += 1
                previous_weight = cand.target_weight
                continue

            target_weight = cand.target_weight
            desired_qty = (target_weight *
                            _portfolio_value(cash, position,
                                              candle.close)) / max(
                candle.close, 1e-9)
            delta_qty = desired_qty - position.quantity
            if abs(delta_qty) > 1e-9:
                side: str = "buy" if delta_qty > 0 else "sell"
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

                fill = ShadowFill(
                    ts=candle.ts,
                    symbol=symbol,
                    side=side,  # type: ignore[arg-type]
                    price=float(fill_price),
                    quantity=float(abs(delta_qty)),
                    notional_usd=float(notional),
                )
                virtual_fills.append(fill)
                self.store.append_fill(
                    cfg.strategy_id, run.run_id, fill.asdict())
                self.store.append_event(
                    cfg.strategy_id, run.run_id,
                    ShadowEvent(
                        ts=candle.ts,
                        kind="virtual_fill",
                        symbol=symbol,
                        detail=fill.asdict(),
                    ).asdict(),
                )

            previous_weight = cand.target_weight

        final_equity = _portfolio_value(
            cash, position,
            frame.candles[-1].close if frame.candles else 0.0,
        )
        metrics = {
            "trade_count": len(virtual_fills),
            "intents_emitted": intents_emitted,
            "events_emitted": events_emitted,
            "risk_rejections": risk_rejections,
            "initial_capital_usd": float(cfg.initial_capital_usd),
            "final_equity_usd": float(final_equity),
            "total_return": (
                (final_equity - cfg.initial_capital_usd)
                / max(float(cfg.initial_capital_usd), 1e-9)
            ),
        }
        risk_summary = {
            "rejection_count": risk_rejections,
            "intents_evaluated": intents_emitted,
            "rejection_rate": (
                (risk_rejections / intents_emitted)
                if intents_emitted else 0.0
            ),
        }

        report = ShadowReport(
            run_id=run.run_id,
            strategy_id=cfg.strategy_id,
            candidate_id=cfg.candidate_id,
            started_at=run.started_at,
            finished_at=_now_iso(),
            status="completed",
            metrics=metrics,
            risk_summary=risk_summary,
            artifacts={
                "events": run.events_path or "",
                "fills": run.fills_path or "",
                "report": run.report_path or "",
            },
        )
        self.store.write_report(
            cfg.strategy_id, run.run_id, report.asdict())
        return report

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _instantiate_engine(self, cfg: BacktestConfig) -> Any:
        engine_path = candidate_signal_engine_path(
            self.workspace, cfg.strategy_id, cfg.candidate_id)
        if not engine_path.is_file():
            raise ShadowRuntimeError(
                f"shadow_runtime_signal_engine_missing:{engine_path}")
        module = load_signal_engine_module(
            self.workspace, cfg.strategy_id, cfg.candidate_id)
        engine_cls = getattr(module, "SignalEngine", None)
        if engine_cls is None:
            raise ShadowRuntimeError("shadow_runtime_signal_engine_no_class")
        return engine_cls()

    def _collect_signals(
        self, engine: Any, frame: OhlcvFrame, symbol: str,
    ) -> dict[str, SignalFrame]:
        try:
            raw = list(engine.generate({symbol: frame}))
        except Exception as exc:
            raise ShadowRuntimeError(
                f"shadow_runtime_engine_generate_failed:{exc}") from exc
        out: dict[str, SignalFrame] = {}
        for item in raw:
            try:
                sig = coerce_signal_frame(
                    item, max_weight=1.0, allow_short=False)
            except Exception as exc:
                raise ShadowRuntimeError(
                    f"shadow_runtime_signal_invalid:{exc}") from exc
            out[sig.ts] = sig
        return out

    def _mint_run_id(self) -> str:
        return "shadow-" + secrets.token_hex(4)


def _portfolio_value(
    cash: float, position: _Position, mark_price: float,
) -> float:
    return cash + position.quantity * mark_price


__all__ = [
    "ShadowRuntime",
    "ShadowRuntimeError",
    "run_shadow",
]
