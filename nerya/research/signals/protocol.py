"""SignalFrame model + signal engine duck-type protocol.

``SignalFrame`` describes a single time-stamped target
weight for a single symbol.  Engine output is normalised through
:func:`coerce_signal_frame` so subsequent stages can rely on clean data.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable

from ...core.errors import NeryaError


class SignalFrameError(NeryaError):
    """Raised when an engine emits a malformed signal frame."""


@dataclass
class SignalFrame:
    ts: str
    symbol: str
    target_weight: float
    confidence: float = 0.5
    reason: str = ""
    features: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class SignalEngineProtocol(Protocol):
    """Duck type for agent-authored signal engines."""

    def generate(
        self, data_map: dict[str, Any]
    ) -> Iterable[SignalFrame] | Iterable[dict[str, Any]]: ...


# ----------------------------------------------------------------------
# Coercion
# ----------------------------------------------------------------------


_VALID_REASON_REQUIRED_AT_OR_ABOVE = 1e-9


def coerce_signal_frame(
    payload: SignalFrame | dict[str, Any],
    *,
    max_weight: float = 1.0,
    allow_short: bool = False,
) -> SignalFrame:
    """Validate and normalise a single emitted signal.

    The runtime requires:
    * ``target_weight`` finite and within strategy bounds,
    * ``confidence`` in ``[0, 1]``,
    * ``reason`` required for non-zero target deltas.
    """

    if isinstance(payload, SignalFrame):
        frame = payload
    elif isinstance(payload, dict):
        try:
            frame = SignalFrame(
                ts=str(payload["ts"]),
                symbol=str(payload["symbol"]),
                target_weight=float(payload["target_weight"]),
                confidence=float(payload.get("confidence", 0.5)),
                reason=str(payload.get("reason", "")),
                features=dict(payload.get("features") or {}),
            )
        except KeyError as exc:
            raise SignalFrameError(
                f"signal_frame_missing:{exc.args[0]!r}") from exc
        except (TypeError, ValueError) as exc:
            raise SignalFrameError(f"signal_frame_bad_type:{exc}") from exc
    else:
        raise SignalFrameError(
            f"signal_frame_unsupported_type:{type(payload).__name__}")

    if not frame.symbol or frame.symbol != frame.symbol.strip():
        raise SignalFrameError("signal_frame_bad_symbol")

    if not math.isfinite(frame.target_weight):
        raise SignalFrameError("signal_frame_target_weight_not_finite")

    if abs(frame.target_weight) > max_weight + 1e-9:
        raise SignalFrameError(
            f"signal_frame_target_weight_outside_bounds:"
            f"max_weight={max_weight}")

    if not allow_short and frame.target_weight < -1e-9:
        raise SignalFrameError(
            "signal_frame_short_disallowed:target_weight_negative")

    if not 0.0 - 1e-9 <= frame.confidence <= 1.0 + 1e-9:
        raise SignalFrameError("signal_frame_confidence_out_of_range")
    frame.confidence = max(0.0, min(1.0, float(frame.confidence)))

    if (
        abs(frame.target_weight) >= _VALID_REASON_REQUIRED_AT_OR_ABOVE
        and not frame.reason.strip()
    ):
        raise SignalFrameError(
            "signal_frame_reason_required_for_nonzero_target")

    return frame


__all__ = [
    "SignalEngineProtocol",
    "SignalFrame",
    "SignalFrameError",
    "coerce_signal_frame",
]
