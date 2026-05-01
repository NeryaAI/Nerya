"""Promotion gate — research promotion workflow §5 Task 7.

Gates the canonical ``draft -> paper -> canary -> live`` lifecycle on the
presence and shape of structured ``ValidationReport`` artifacts (plus, in
the next iteration, shadow run records).

Design rules
------------
* The gate is **structured** — every blocked transition returns a list of
  ``Blocker`` dicts so the API/dashboard/ACP layer can render them
  deterministically.
* The gate is **non-mutating** — callers (``strategy.set_status``) decide
  whether to raise.
* The gate is **non-bypassing** — it cannot promote in place of
  ``RiskGate`` or ``ApprovalGate``; it only adds a new pre-condition.
* The gate is **idempotent** — re-evaluating the same workspace twice
  must return the same blockers.
* The gate is **fixture-safe** — it never opens the network, never reads
  outside the workspace, and treats a missing report as
  ``validation_missing`` rather than as an OS error.

The blockers are intentionally machine-friendly. Each blocker carries a
short ``code``, a human ``detail`` string, and (optionally) a
``failed_gates``/``missing`` list pulled straight from the report so the
dashboard can render a single source of truth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .artifacts import (
    ArtifactPathError,
    candidate_report_path,
    validation_latest_path,
)
from .validation_report import REQUIRED_GATE_NAMES, ValidationReport


# ----------------------------------------------------------------------
# Blocker dataclass + structured codes
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Blocker:
    """A single structured promotion blocker."""

    code: str
    detail: str
    extra: Mapping[str, Any] | None = None

    def asdict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "detail": self.detail}
        if self.extra:
            out.update(dict(self.extra))
        return out


# Canonical blocker codes — keep these stable so dashboards/SDKs can
# interpret them without string-matching on `detail`.
CODE_VALIDATION_MISSING = "validation_missing"
CODE_VALIDATION_FAILED = "validation_failed"
CODE_VALIDATION_WARN_REQUIRES_OVERRIDE = "validation_warn_requires_override"
CODE_VALIDATION_GATES_INCOMPLETE = "validation_gates_incomplete"
CODE_VALIDATION_HARD_FAIL = "validation_hard_fail"
CODE_SHADOW_REQUIRED = "shadow_required"
CODE_SHADOW_MIN_NOT_MET = "shadow_minimum_not_met"


# ----------------------------------------------------------------------
# Public entrypoint
# ----------------------------------------------------------------------


def evaluate_promotion(
    *,
    workspace: Path,
    strategy_id: str,
    from_status: str,
    to_status: str,
    config: Optional[Mapping[str, Any]] = None,
    candidate_id: Optional[str] = None,
    override: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Return the structured list of blockers for the requested transition.

    Returns an empty list when the gate allows the transition.

    The function never raises ``InvalidTransition`` — graph-level
    validity is the caller's responsibility (``strategy_lifecycle``).
    Here we only enforce the *content* gates: validation reports and
    shadow runs.
    """

    blockers: list[Blocker] = []
    cfg = _coerce_config(config)
    override_map = dict(override or {})

    # Same-state requests do not require a content gate; the lifecycle
    # graph already short-circuits these.
    if from_status == to_status:
        return []

    if not cfg.get("validation_enabled", True):
        return []

    needs_validation = _needs_validation(from_status, to_status, cfg)
    needs_shadow = _needs_shadow(to_status, cfg)

    report_payload, report_blockers = _load_report(
        workspace, strategy_id, candidate_id)

    if needs_validation:
        if report_payload is None:
            blockers.append(Blocker(
                code=CODE_VALIDATION_MISSING,
                detail=(
                    f"strategy {strategy_id!r} cannot move from "
                    f"{from_status!r} to {to_status!r} without a "
                    "validation report"
                ),
            ))
        else:
            blockers.extend(
                _blockers_from_report(
                    report_payload,
                    to_status=to_status,
                    cfg=cfg,
                    override=override_map,
                )
            )
    elif report_payload is not None and report_blockers is None:
        # ``draft -> paper`` carries no requirement, but we still
        # surface a hard-fail report as a soft warning so the operator
        # never silently promotes broken candidates.
        report = ValidationReport.from_payload(report_payload)
        if report.status == "fail":
            blockers.append(Blocker(
                code=CODE_VALIDATION_HARD_FAIL,
                detail=(
                    "latest validation report is a hard fail; "
                    "review before transitioning to paper"
                ),
                extra={"failed_gates": report.gate_failures()},
            ))

    if needs_shadow:
        shadow_blocker = _blocker_for_shadow(
            workspace=workspace,
            strategy_id=strategy_id,
            candidate_id=candidate_id,
            cfg=cfg,
            override=override_map,
        )
        if shadow_blocker is not None:
            blockers.append(shadow_blocker)

    return [b.asdict() for b in blockers]


def required_for_transition(
    from_status: str, to_status: str,
    config: Optional[Mapping[str, Any]] = None,
) -> dict[str, bool]:
    """Cheap helper for dashboards to know what gates apply.

    Returns ``{"validation": bool, "shadow": bool}`` for the proposed
    transition. Same-state transitions return all-False.
    """

    if from_status == to_status:
        return {"validation": False, "shadow": False}

    cfg = _coerce_config(config)
    if not cfg.get("validation_enabled", True):
        return {"validation": False, "shadow": False}

    return {
        "validation": _needs_validation(from_status, to_status, cfg),
        "shadow": _needs_shadow(to_status, cfg),
    }


# ----------------------------------------------------------------------
# Helpers — config + transition policy
# ----------------------------------------------------------------------


_FLAT_RESEARCH_KEYS = {
    "validation_enabled",
    "validation_required_for_canary",
    "shadow_required_for_live",
    "allow_warn_promotion",
    "allow_fixture_data",
    "default_initial_capital_usd",
    "gates",
    "shadow_min_trades",
}


def _coerce_config(cfg: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if cfg is None:
        return _DEFAULT_RESEARCH_CONFIG.copy()
    if isinstance(cfg, Mapping):
        # If the caller passed a top-level ``Config`` blob, dive into
        # ``research`` first.
        nested = cfg.get("research")
        if isinstance(nested, Mapping):
            merged = _DEFAULT_RESEARCH_CONFIG.copy()
            merged.update(dict(nested))
            return merged
        # Otherwise treat any dict whose keys overlap with the
        # research config as a flat research config.
        if any(k in cfg for k in _FLAT_RESEARCH_KEYS):
            merged = _DEFAULT_RESEARCH_CONFIG.copy()
            merged.update(dict(cfg))
            return merged
    return _DEFAULT_RESEARCH_CONFIG.copy()


_DEFAULT_RESEARCH_CONFIG: dict[str, Any] = {
    "validation_enabled": True,
    "validation_required_for_canary": True,
    "shadow_required_for_live": True,
    "allow_warn_promotion": False,
    "gates": {
        "min_bars": 20,
        "min_trades": 1,
        "max_drawdown_pct": 30.0,
        "min_sharpe": 0.0,
        "cost_stress_multiplier": 2.0,
    },
}


def _needs_validation(
    from_status: str, to_status: str, cfg: Mapping[str, Any]
) -> bool:
    """Validation is required from paper onwards (configurable)."""

    require_canary = bool(cfg.get("validation_required_for_canary", True))

    # ``draft -> paper`` is the legacy "first tradable" hop and
    # historically does not require a report. We keep it advisory.
    if to_status == "paper":
        return False
    if to_status in ("paused", "archived"):
        return False
    if to_status == "canary":
        return require_canary
    if to_status == "live":
        # ``canary -> live`` always requires validation when enabled.
        return True
    return False


def _needs_shadow(to_status: str, cfg: Mapping[str, Any]) -> bool:
    if to_status != "live":
        return False
    return bool(cfg.get("shadow_required_for_live", True))


# ----------------------------------------------------------------------
# Helpers — load + interpret reports
# ----------------------------------------------------------------------


def _load_report(
    workspace: Path,
    strategy_id: str,
    candidate_id: Optional[str],
) -> tuple[dict[str, Any] | None, list[Blocker] | None]:
    """Load the most relevant validation report.

    Preference order:
      1. The candidate-specific report (when ``candidate_id`` was
         provided).
      2. ``workspace/strategies/<id>/validation/latest.json``.
    """

    try:
        if candidate_id:
            path = candidate_report_path(
                workspace, strategy_id, candidate_id)
            if path.is_file():
                return _read_json(path), None
        latest = validation_latest_path(workspace, strategy_id)
        if latest.is_file():
            return _read_json(latest), None
    except ArtifactPathError:
        return None, None
    except (OSError, ValueError):
        return None, None
    return None, None


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _blockers_from_report(
    payload: dict[str, Any],
    *,
    to_status: str,
    cfg: Mapping[str, Any],
    override: Mapping[str, Any],
) -> list[Blocker]:
    """Translate a structured report into promotion blockers."""

    out: list[Blocker] = []
    report = ValidationReport.from_payload(payload)
    allow_warn = bool(
        cfg.get("allow_warn_promotion", False)
        or override.get("allow_warn", False)
    )

    if not report.has_required_gates():
        missing = sorted(
            set(REQUIRED_GATE_NAMES)
            - {g.get("name") for g in report.gates}
        )
        out.append(Blocker(
            code=CODE_VALIDATION_GATES_INCOMPLETE,
            detail=(
                f"validation report is missing required gates: "
                f"{', '.join(missing) or 'unknown'}"
            ),
            extra={"missing": missing},
        ))

    if report.status == "fail":
        out.append(Blocker(
            code=CODE_VALIDATION_FAILED,
            detail=(
                f"latest validation report failed; cannot promote to "
                f"{to_status!r}"
            ),
            extra={"failed_gates": report.gate_failures()},
        ))
    elif report.status == "warn":
        if not allow_warn:
            out.append(Blocker(
                code=CODE_VALIDATION_WARN_REQUIRES_OVERRIDE,
                detail=(
                    "validation report status is warn — promotion to "
                    f"{to_status!r} requires explicit operator override"
                ),
                extra={"failed_gates": report.gate_failures()},
            ))

    # Bubble up report-level structured blockers (e.g. data coverage,
    # missing reproducibility hash) as-is.
    for raw in report.blockers or []:
        if isinstance(raw, dict):
            out.append(Blocker(
                code=str(raw.get("code") or "validation_blocker"),
                detail=str(raw.get("detail") or ""),
                extra={k: v for k, v in raw.items()
                       if k not in {"code", "detail"}},
            ))
    return out


# ----------------------------------------------------------------------
# Helpers — shadow runtime (placeholder for Task 8)
# ----------------------------------------------------------------------


def _blocker_for_shadow(
    *,
    workspace: Path,
    strategy_id: str,
    candidate_id: Optional[str],
    cfg: Mapping[str, Any],
    override: Mapping[str, Any],
) -> Optional[Blocker]:
    """Return a blocker when ``canary -> live`` lacks a usable shadow run.

    The shadow runtime (Task 8) is not yet implemented — until it is,
    we always emit ``shadow_required`` for live transitions when shadow
    enforcement is enabled, so live promotions can never silently land
    without operator action. Operators can override per-transition by
    passing ``override={"allow_missing_shadow": True}`` and journaling
    the decision.
    """

    if bool(override.get("allow_missing_shadow", False)):
        return None

    shadow_dir = workspace / "strategies" / strategy_id / "shadow" / "runs"
    if shadow_dir.is_dir():
        any_completed = any(
            (run / "report.json").is_file()
            for run in shadow_dir.iterdir() if run.is_dir()
        )
        if any_completed:
            min_trades = int(
                cfg.get("shadow_min_trades",
                        cfg.get("gates", {}).get("min_trades", 1))
            )
            if min_trades <= 0:
                return None
            try:
                last = max(
                    (r for r in shadow_dir.iterdir() if r.is_dir()),
                    key=lambda r: r.stat().st_mtime,
                )
                report = json.loads(
                    (last / "report.json").read_text(encoding="utf-8")
                )
                trades = int(report.get("metrics", {}).get("trade_count", 0))
                if trades < min_trades:
                    return Blocker(
                        code=CODE_SHADOW_MIN_NOT_MET,
                        detail=(
                            f"shadow run has {trades} trade(s); minimum "
                            f"{min_trades} required for live promotion"
                        ),
                        extra={
                            "trades": trades,
                            "min_trades": min_trades,
                        },
                    )
            except (OSError, ValueError, KeyError):
                # Treat unreadable reports as missing.
                pass
            return None

    return Blocker(
        code=CODE_SHADOW_REQUIRED,
        detail=(
            "shadow runtime has no completed report for this strategy; "
            "live promotion is blocked until a shadow run finishes"
        ),
    )


__all__ = [
    "Blocker",
    "evaluate_promotion",
    "required_for_transition",
    "CODE_VALIDATION_MISSING",
    "CODE_VALIDATION_FAILED",
    "CODE_VALIDATION_WARN_REQUIRES_OVERRIDE",
    "CODE_VALIDATION_GATES_INCOMPLETE",
    "CODE_VALIDATION_HARD_FAIL",
    "CODE_SHADOW_REQUIRED",
    "CODE_SHADOW_MIN_NOT_MET",
]
