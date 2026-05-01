"""Research-layer data contracts.

Defines ``BacktestConfig`` for the research runtime. The dataclass is
intentionally validation-heavy: every operator-
or agent-authored backtest config goes through ``BacktestConfig.parse``
before any fixture loader, signal engine or runner ever sees it.

The contract is implemented inside Nerya so the runtime keeps the single
source of truth.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from ..core.errors import NeryaError


class BacktestConfigError(NeryaError):
    """Raised when a backtest config payload fails structural validation.

    The error carries a structured ``errors`` list so HTTP/dashboard
    surfaces can render every problem rather than guessing from a string.
    """

    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__(_summarise(errors))
        self.errors = list(errors)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INTERVAL_RE = re.compile(r"^[1-9]\d*[smhdwM]$|^[1-9]\d*D$")
_KNOWN_MARKETS = {"auto", "crypto", "polymarket", "evm", "fixture"}
_KNOWN_DATA_SOURCES = {"fixture", "ccxt", "polymarket", "onchain"}
_KNOWN_ENGINES = {"auto", "crypto", "polymarket", "paper_intent"}


@dataclass
class BacktestConfig:
    """Backtest configuration record.

    Fields are
    primitive types so the payload is trivially JSON serialisable —
    important because validation reports must be reproducible.
    """

    strategy_id: str
    candidate_id: str
    symbols: list[str]
    start_date: str
    end_date: str
    interval: str = "1D"
    market: str = "auto"
    data_source: str = "fixture"
    engine: str = "auto"
    initial_capital_usd: float = 10_000.0
    fee_bps: float = 5.0
    slippage_bps: float = 10.0
    max_position_weight: float = 1.0
    allow_short: bool = False
    validation: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Construction / parsing
    # ------------------------------------------------------------------

    @classmethod
    def parse(cls, payload: dict[str, Any] | "BacktestConfig") -> "BacktestConfig":
        """Validate and normalize a raw payload.

        Raises ``BacktestConfigError`` if any field is malformed.  The
        plan requires path-traversal rejection on identifiers
        (``../../x`` etc), empty symbols, invalid date order and
        non-positive capital — all enforced here.
        """

        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, dict):
            raise BacktestConfigError([
                {"field": "<root>", "code": "must_be_object",
                 "got": type(payload).__name__},
            ])

        errors: list[dict[str, Any]] = []
        normalized: dict[str, Any] = {}

        # ---- mandatory identifiers ----
        for key in ("strategy_id", "candidate_id"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append({"field": key, "code": "required_string"})
                continue
            if not _safe_identifier(value):
                errors.append({"field": key, "code": "invalid_identifier",
                                "got": value})
                continue
            normalized[key] = value.strip()

        # ---- symbols ----
        symbols_raw = payload.get("symbols")
        if not isinstance(symbols_raw, (list, tuple)) or not symbols_raw:
            errors.append({"field": "symbols", "code": "required_non_empty_list"})
        else:
            cleaned: list[str] = []
            for idx, sym in enumerate(symbols_raw):
                if not isinstance(sym, str) or not sym.strip():
                    errors.append({"field": f"symbols[{idx}]",
                                    "code": "non_empty_string"})
                    continue
                cleaned.append(sym.strip())
            if cleaned:
                normalized["symbols"] = cleaned

        # ---- dates ----
        for key in ("start_date", "end_date"):
            value = payload.get(key)
            if not isinstance(value, str) or not _DATE_RE.match(value or ""):
                errors.append({"field": key, "code": "must_be_yyyy_mm_dd"})
                continue
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                errors.append({"field": key, "code": "invalid_calendar_date"})
                continue
            normalized[key] = value

        if "start_date" in normalized and "end_date" in normalized:
            if normalized["start_date"] > normalized["end_date"]:
                errors.append({"field": "end_date", "code": "before_start_date"})

        # ---- interval ----
        interval = payload.get("interval", "1D")
        if not isinstance(interval, str) or not _INTERVAL_RE.match(interval):
            errors.append({"field": "interval", "code": "invalid_interval",
                           "got": interval})
        else:
            normalized["interval"] = interval

        # ---- enum-like fields ----
        for key, allowed in (
            ("market", _KNOWN_MARKETS),
            ("data_source", _KNOWN_DATA_SOURCES),
            ("engine", _KNOWN_ENGINES),
        ):
            value = payload.get(key, getattr(cls, key) if hasattr(cls, key)
                                else "auto")
            if value is None:
                continue
            if not isinstance(value, str) or value not in allowed:
                errors.append({"field": key, "code": "unsupported",
                               "got": value, "allowed": sorted(allowed)})
            else:
                normalized[key] = value

        # ---- numerics ----
        capital = payload.get("initial_capital_usd",
                               cls.initial_capital_usd)
        if not _is_positive_number(capital):
            errors.append({"field": "initial_capital_usd",
                           "code": "must_be_positive_number"})
        else:
            normalized["initial_capital_usd"] = float(capital)

        for key, default in (("fee_bps", cls.fee_bps),
                             ("slippage_bps", cls.slippage_bps)):
            value = payload.get(key, default)
            if not _is_finite_number(value) or value < 0:
                errors.append({"field": key,
                               "code": "must_be_non_negative_number"})
            else:
                normalized[key] = float(value)

        weight = payload.get("max_position_weight", cls.max_position_weight)
        if not _is_finite_number(weight) or weight <= 0 or weight > 5:
            errors.append({"field": "max_position_weight",
                           "code": "must_be_in_range_0_to_5"})
        else:
            normalized["max_position_weight"] = float(weight)

        allow_short = payload.get("allow_short", cls.allow_short)
        if not isinstance(allow_short, bool):
            errors.append({"field": "allow_short", "code": "must_be_bool"})
        else:
            normalized["allow_short"] = bool(allow_short)

        # ---- nested validation/metadata ----
        validation = payload.get("validation", {})
        if validation is None:
            validation = {}
        if not isinstance(validation, dict):
            errors.append({"field": "validation", "code": "must_be_object"})
        else:
            normalized["validation"] = dict(validation)

        metadata = payload.get("metadata", {})
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            errors.append({"field": "metadata", "code": "must_be_object"})
        else:
            normalized["metadata"] = dict(metadata)

        if errors:
            raise BacktestConfigError(errors)

        return cls(**normalized)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def with_overrides(self, **overrides: Any) -> "BacktestConfig":
        payload = self.asdict()
        payload.update(overrides)
        return BacktestConfig.parse(payload)


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _is_positive_number(value: Any) -> bool:
    return _is_finite_number(value) and float(value) > 0.0


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")


def _safe_identifier(value: str) -> bool:
    """Reject path-traversal-like identifiers.

    The plan requires that ``../../x`` and similar inputs are rejected
    before they reach any artifact path resolver.  We only allow short
    alphanumeric tokens with ``-``, ``.``, ``_`` separators.
    """

    if value != value.strip():
        return False
    if any(token in value for token in ("/", "\\", "..", "\x00")):
        return False
    return bool(_SAFE_ID_RE.match(value))


def _summarise(errors: Iterable[dict[str, Any]]) -> str:
    parts = []
    for err in errors:
        field_name = err.get("field", "<unknown>")
        code = err.get("code", "invalid")
        parts.append(f"{field_name}:{code}")
    return "backtest_config_invalid:" + ",".join(parts) if parts else \
        "backtest_config_invalid"


__all__ = [
    "BacktestConfig",
    "BacktestConfigError",
]
