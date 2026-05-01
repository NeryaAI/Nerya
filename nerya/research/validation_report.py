"""ValidationReport contract (research runtime spec §4.4).

A validation report is the single artifact that promotion gates and the
dashboard consume.  It must be reproducible: every report
records the code/config hash, data source, symbol set, date range, fees,
slippage, initial capital, engine and generated artifact paths.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..core.errors import NeryaError
from ..core.time import now_iso


ValidationStatus = Literal["pass", "warn", "fail"]


REQUIRED_GATE_NAMES: tuple[str, ...] = (
    "minimum_bars",
    "minimum_trades",
    "max_drawdown",
    "sharpe_or_sortino",
    "cost_stress",
    "walk_forward",
    "paper_shadow_required",
    "risk_gate_compatibility",
)


class ValidationReportError(NeryaError):
    """Raised when a report cannot be (de)serialised cleanly."""


@dataclass
class ValidationGate:
    name: str
    status: ValidationStatus
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationReport:
    strategy_id: str
    candidate_id: str
    status: ValidationStatus
    metrics: dict[str, Any] = field(default_factory=dict)
    gates: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    data_coverage: dict[str, Any] = field(default_factory=dict)
    engine: dict[str, Any] = field(default_factory=dict)
    reproducibility: dict[str, Any] = field(default_factory=dict)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.asdict(), indent=indent, sort_keys=True,
                          default=str)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ValidationReport":
        if not isinstance(payload, dict):
            raise ValidationReportError("validation_report_must_be_object")
        required = {"strategy_id", "candidate_id", "status"}
        missing = required - payload.keys()
        if missing:
            raise ValidationReportError(
                f"validation_report_missing:{','.join(sorted(missing))}")
        if payload["status"] not in ("pass", "warn", "fail"):
            raise ValidationReportError(
                f"validation_report_bad_status:{payload['status']!r}")
        return cls(
            strategy_id=str(payload["strategy_id"]),
            candidate_id=str(payload["candidate_id"]),
            status=payload["status"],
            metrics=dict(payload.get("metrics") or {}),
            gates=list(payload.get("gates") or []),
            artifacts=dict(payload.get("artifacts") or {}),
            data_coverage=dict(payload.get("data_coverage") or {}),
            engine=dict(payload.get("engine") or {}),
            reproducibility=dict(payload.get("reproducibility") or {}),
            blockers=list(payload.get("blockers") or []),
            created_at=str(payload.get("created_at") or now_iso()),
        )

    def gate(self, name: str) -> dict[str, Any] | None:
        for entry in self.gates:
            if isinstance(entry, dict) and entry.get("name") == name:
                return entry
        return None

    def has_required_gates(self) -> bool:
        names = {g.get("name") for g in self.gates if isinstance(g, dict)}
        return set(REQUIRED_GATE_NAMES).issubset(names)

    def gate_failures(self) -> list[str]:
        out: list[str] = []
        for entry in self.gates:
            if not isinstance(entry, dict):
                continue
            if entry.get("status") == "fail":
                out.append(str(entry.get("name") or ""))
        return [n for n in out if n]


__all__ = [
    "REQUIRED_GATE_NAMES",
    "ValidationGate",
    "ValidationReport",
    "ValidationReportError",
    "ValidationStatus",
]
