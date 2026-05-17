"""Compile :class:`SignalFrame` outputs into :class:`IntentCandidate`.

+ Turn target-weight deltas into ``buy/sell/hold``
intent candidates that the trading skill / Risk Gate path can consume.

The compiler is deliberately *pure*:

* No connector calls.
* No account mutation.
* No ledger writes.

Outputs include traceable source-signal metadata so promotion / shadow
artifacts can replay the decision later.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Literal

from ...core.errors import NeryaError
from .protocol import SignalFrame, SignalFrameError, coerce_signal_frame


Side = Literal["buy", "sell", "hold"]


class IntentCompilerError(NeryaError):
    """Raised when a signal cannot be compiled to an intent candidate."""


@dataclass
class IntentCandidate:
    strategy_id: str
    symbol: str
    side: Side
    target_weight: float
    notional_usd_estimate: float
    confidence: float
    reason: str
    source_signal: SignalFrame
    delta_weight: float = 0.0
    previous_weight: float = 0.0
    metadata: dict = field(default_factory=dict)

    def asdict(self) -> dict:
        # Manual dump so SignalFrame is JSON-friendly.
        return {
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "side": self.side,
            "target_weight": self.target_weight,
            "notional_usd_estimate": self.notional_usd_estimate,
            "confidence": self.confidence,
            "reason": self.reason,
            "delta_weight": self.delta_weight,
            "previous_weight": self.previous_weight,
            "source_signal": self.source_signal.asdict(),
            "metadata": dict(self.metadata),
        }


def compile_signal_to_intent_candidate(
    signal: SignalFrame | dict,
    *,
    strategy_id: str,
    portfolio_value_usd: float,
    previous_weight: float = 0.0,
    max_position_weight: float = 1.0,
    allow_short: bool = False,
    epsilon: float = 1e-6,
) -> IntentCandidate:
    """Compile one signal into an IntentCandidate."""

    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise IntentCompilerError("intent_compiler_bad_strategy_id")

    if not isinstance(portfolio_value_usd, (int, float)) or \
            not math.isfinite(float(portfolio_value_usd)):
        raise IntentCompilerError(
            "intent_compiler_bad_portfolio_value")
    portfolio_value_usd = float(portfolio_value_usd)
    if portfolio_value_usd < 0:
        raise IntentCompilerError(
            "intent_compiler_negative_portfolio_value")

    try:
        frame = coerce_signal_frame(
            signal,
            max_weight=max_position_weight,
            allow_short=allow_short,
        )
    except SignalFrameError as exc:
        raise IntentCompilerError(str(exc)) from exc

    delta = float(frame.target_weight) - float(previous_weight)
    if abs(delta) < epsilon:
        side: Side = "hold"
    elif delta > 0:
        side = "buy"
    else:
        side = "sell"

    notional = abs(delta) * portfolio_value_usd

    return IntentCandidate(
        strategy_id=strategy_id,
        symbol=frame.symbol,
        side=side,
        target_weight=float(frame.target_weight),
        notional_usd_estimate=float(notional),
        confidence=float(frame.confidence),
        reason=frame.reason or "rebalance",
        source_signal=frame,
        delta_weight=delta,
        previous_weight=float(previous_weight),
    )


def compile_signals(
    signals: Iterable[SignalFrame | dict],
    *,
    strategy_id: str,
    portfolio_value_usd: float,
    previous_weights: dict[str, float] | None = None,
    max_position_weight: float = 1.0,
    allow_short: bool = False,
    epsilon: float = 1e-6,
) -> list[IntentCandidate]:
    """Compile a batch of signals to intent candidates.

    ``previous_weights`` carries the prior target weight per symbol; for
    fresh strategies all weights default to ``0.0``.
    """

    previous_weights = dict(previous_weights or {})
    out: list[IntentCandidate] = []
    for sig in signals:
        symbol = (sig.symbol if isinstance(sig, SignalFrame)
                  else sig.get("symbol"))
        prev = previous_weights.get(symbol or "", 0.0)
        candidate = compile_signal_to_intent_candidate(
            sig,
            strategy_id=strategy_id,
            portfolio_value_usd=portfolio_value_usd,
            previous_weight=prev,
            max_position_weight=max_position_weight,
            allow_short=allow_short,
            epsilon=epsilon,
        )
        out.append(candidate)
        if symbol:
            previous_weights[symbol] = candidate.target_weight
    return out


__all__ = [
    "IntentCandidate",
    "IntentCompilerError",
    "Side",
    "compile_signal_to_intent_candidate",
    "compile_signals",
]
