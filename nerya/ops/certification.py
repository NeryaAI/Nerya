"""production certification gates.

The production alignment plan defines three gates:

* **Gate A** (``prod_paper``)  — real providers, paper execution
* **Gate B** (``canary_live``) — controlled live exposure
* **Gate C** (``full_live``)   — full production trading

Each gate is more than a preflight. Preflight (``nerya.ops.preflight``)
answers "can this process boot?". Certification answers "is there
enough evidence on disk to honestly claim this stage?".

This module is intentionally *evidence-driven*:

* every gate is a pure function over ``Config`` + workspace state,
* each check returns a :class:`GateCheck` carrying ``name``, ``status``,
  ``detail`` and the required artifact path/key it looked for,
* the final :class:`GateReport` is serialisable so it can be attached
  to a release record.

The checks deliberately do **not** execute live trades, call
connectors, or talk to the LLM. They only verify that the artifacts
the runbook asks the operator to collect actually exist.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal

from ..core.config import Config
from . import certification_evidence as _ev
from .preflight import Mode, run_preflight


Gate = Literal["A", "B", "C"]
GATE_TO_MODE: dict[Gate, Mode] = {
    "A": "prod_paper",
    "B": "canary_live",
    "C": "full_live",
}

# Freshness windows in seconds — tightest for full_live. Release
# checklists explicitly call out that evidence must be produced against
# the release candidate, not cached forever from an old session.
FRESHNESS_BY_GATE: dict[Gate, int] = {
    "A": 7 * 24 * 3600,
    "B": 3 * 24 * 3600,
    "C": 24 * 3600,
}

# Which kinds of evidence each gate requires. Gate A only needs paper
# evidence (explain+attribution). Gate B adds scenario replay and
# rehearsal, and Gate C adds live divergence evidence plus a signed
# approval record. Keep the audit/checklist and this mapping in sync.
REQUIRED_EVIDENCE: dict[Gate, tuple[str, ...]] = {
    "A": ("explain", "attribution"),
    "B": ("explain", "attribution", "scenario_replay", "rehearsal"),
    "C": ("explain", "attribution", "scenario_replay",
          "rehearsal", "divergence", "approval"),
}


@dataclass
class GateCheck:
    name: str
    status: str  # "pass" | "fail" | "warn"
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


@dataclass
class GateReport:
    gate: Gate
    mode: Mode
    checks: list[GateCheck] = field(default_factory=list)

    def ok(self) -> bool:
        return all(c.status != "fail" for c in self.checks)

    def failures(self) -> list[GateCheck]:
        return [c for c in self.checks if c.status == "fail"]

    def warnings(self) -> list[GateCheck]:
        return [c for c in self.checks if c.status == "warn"]

    def asdict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "mode": self.mode,
            "ok": self.ok(),
            "checks": [c.asdict() for c in self.checks],
            "failures": [c.asdict() for c in self.failures()],
            "warnings": [c.asdict() for c in self.warnings()],
        }


# --------------------------------------------------------------------- checks

def _preflight_check(cfg: Config, gate: Gate) -> GateCheck:
    mode = GATE_TO_MODE[gate]
    report = run_preflight(cfg, mode=mode)
    if report.ok():
        return GateCheck(
            name="preflight",
            status="pass",
            detail=f"preflight(mode={mode!r}) returned ok",
            evidence={"mode": mode, "warnings":
                      [c.name for c in report.warnings()]},
        )
    return GateCheck(
        name="preflight",
        status="fail",
        detail=f"preflight(mode={mode!r}) has {len(report.failures())} failures",
        evidence={"failures":
                  [c.asdict() for c in report.failures()]},
    )


def _one_paper_cycle(cfg: Config, gate: Gate) -> GateCheck:
    """Gate A requires at least one strategy to have a completed paper
    cycle: a fills row and a pnl row for the same session in history."""
    root = cfg.paths.strategies
    if not root.exists():
        return GateCheck(name="paper_cycle", status="fail",
                         detail="no strategies/ directory")
    from ..strategy_history import store
    for sdir in sorted(root.iterdir()):
        if not sdir.is_dir():
            continue
        sid = sdir.name
        try:
            fills = store.read_ledger(cfg.paths, sid, "fills")
            pnls = store.read_ledger(cfg.paths, sid, "pnl")
        except Exception:
            continue
        fill_sessions = {f.get("session_id") for f in fills if f.get("session_id")}
        pnl_sessions = {p.get("session_id") for p in pnls if p.get("session_id")}
        both = fill_sessions & pnl_sessions
        if both:
            sid_example = sorted(both)[0]
            return GateCheck(
                name="paper_cycle",
                status="pass",
                detail=(
                    f"strategy {sid!r} has a completed paper cycle for "
                    f"session {sid_example!r}"
                ),
                evidence={"strategy_id": sid, "session_id": sid_example},
            )
    return GateCheck(
        name="paper_cycle",
        status="fail",
        detail="no strategy has a session with both fills and pnl",
        evidence={"searched": str(root)},
    )


def _version_pinned(cfg: Config, gate: Gate) -> GateCheck:
    """Gate B requires a pinned strategy version + at least one promotion
    record. Gate C requires the same (pin is inherited)."""
    from ..trading import strategy_versions
    strategies_root = cfg.paths.strategies
    if not strategies_root.exists():
        return GateCheck(name="version_pinned", status="fail",
                         detail="no strategies/ directory")
    pinned: list[dict[str, Any]] = []
    for sdir in sorted(strategies_root.iterdir()):
        if not sdir.is_dir():
            continue
        sid = sdir.name
        try:
            active = strategy_versions.active_version_id(cfg.paths, sid)
        except Exception:
            active = None
        if not active:
            continue
        try:
            promos = strategy_versions.list_promotions(cfg.paths, sid)
        except Exception:
            promos = []
        if promos:
            pinned.append({
                "strategy_id": sid,
                "active_version_id": active,
                "latest_promotion": promos[-1].asdict(),
            })
    if pinned:
        return GateCheck(
            name="version_pinned",
            status="pass",
            detail=f"{len(pinned)} strategy/ies pinned with promotion record",
            evidence={"pinned": pinned},
        )
    return GateCheck(
        name="version_pinned",
        status="fail",
        detail="no strategy has both an active version and a promotion record",
    )


def _rollback_target(cfg: Config, gate: Gate) -> GateCheck:
    """Gate B/C require at least one strategy to expose a usable
    rollback target — i.e. list_versions returns ≥ 2 entries."""
    from ..trading import strategy_versions
    strategies_root = cfg.paths.strategies
    if not strategies_root.exists():
        return GateCheck(name="rollback_target", status="fail",
                         detail="no strategies/ directory")
    for sdir in sorted(strategies_root.iterdir()):
        if not sdir.is_dir():
            continue
        sid = sdir.name
        try:
            versions = strategy_versions.list_versions(cfg.paths, sid)
        except Exception:
            continue
        if len(versions) >= 2:
            return GateCheck(
                name="rollback_target",
                status="pass",
                detail=(
                    f"strategy {sid!r} has {len(versions)} versions "
                    "(rollback target available)"
                ),
                evidence={"strategy_id": sid,
                          "count": len(versions),
                          "latest": versions[-1].asdict()},
            )
    return GateCheck(
        name="rollback_target",
        status="fail",
        detail="no strategy has ≥ 2 versions to roll back to",
    )


def _no_experimental_on_live(cfg: Config, gate: Gate) -> GateCheck:
    """Gate C forbids ``experimental`` cells on the active tiers."""
    from ..llm.capability_matrix import capability_of
    tiers = cfg.get("llm.tiers") or {}
    offenders: list[str] = []
    for name, tier in (tiers or {}).items():
        provider = (tier or {}).get("provider") or "mock"
        cap = capability_of(provider)
        for key, level in cap.tiers.items():
            if level == "experimental":
                offenders.append(f"{name}({provider}):{key}")
    if not offenders:
        return GateCheck(
            name="no_experimental_on_live",
            status="pass",
            detail="no experimental capability on active tiers",
        )
    return GateCheck(
        name="no_experimental_on_live",
        status="fail",
        detail=f"{len(offenders)} experimental capability/ies on active tiers",
        evidence={"offenders": offenders},
    )


def _kill_switch_ready(cfg: Config, gate: Gate) -> GateCheck:
    """Gate B/C require the kill switch to be armed (i.e. reachable)
    but not currently engaged at boot."""
    from ..core import yaml_io  # local import keeps the boot cost low
    p = cfg.paths.root / "approvals" / "kill_switch.yml"
    if not p.exists():
        return GateCheck(
            name="kill_switch_ready",
            status="warn",
            detail=f"{p} missing — create before live cut-over",
        )
    doc = yaml_io.load(p, default={}) or {}
    engaged = bool(doc.get("engaged", False))
    if engaged:
        return GateCheck(
            name="kill_switch_ready",
            status="fail",
            detail="kill switch is currently engaged",
            evidence={"path": str(p)},
        )
    return GateCheck(
        name="kill_switch_ready",
        status="pass",
        detail="kill switch present, not engaged",
        evidence={"path": str(p)},
    )


def _evidence_package(cfg: Config, gate: Gate) -> GateCheck:
    """Verify the release evidence bundle exists and is fresh.

    For each gate we pick the first strategy that presents a complete
    and fresh bundle (per ``REQUIRED_EVIDENCE`` and
    ``FRESHNESS_BY_GATE``). If no strategy qualifies, we still return
    the best candidate's per-kind status so the operator sees which
    artifact is missing or stale.
    """
    required = REQUIRED_EVIDENCE[gate]
    freshness = FRESHNESS_BY_GATE[gate]
    sid, info = _ev.pick_certifiable_strategy(
        cfg.paths, required_kinds=required, fresh_within_s=freshness,
    )
    if sid is not None:
        return GateCheck(
            name="evidence_package",
            status="pass",
            detail=(
                f"strategy {sid!r} has a fresh evidence package "
                f"(required={list(required)}, fresh_within_s={freshness})"
            ),
            evidence={
                "strategy_id": sid,
                "required": list(required),
                "fresh_within_s": freshness,
                "rows": info,
            },
        )
    return GateCheck(
        name="evidence_package",
        status="fail",
        detail=(
            "no strategy has a complete & fresh release evidence "
            f"package (required={list(required)}, "
            f"fresh_within_s={freshness})"
        ),
        evidence={
            "required": list(required),
            "fresh_within_s": freshness,
            "best": info,
        },
    )


GateChecker = Callable[[Config, Gate], GateCheck]


GATE_CHECKS: dict[Gate, tuple[GateChecker, ...]] = {
    "A": (_preflight_check, _one_paper_cycle, _evidence_package),
    "B": (_preflight_check, _one_paper_cycle,
          _version_pinned, _rollback_target, _kill_switch_ready,
          _evidence_package),
    "C": (_preflight_check, _one_paper_cycle,
          _version_pinned, _rollback_target, _kill_switch_ready,
          _no_experimental_on_live, _evidence_package),
}


def run_gate(cfg: Config, gate: Gate) -> GateReport:
    """Run every check required for ``gate`` and return the report."""
    if gate not in GATE_CHECKS:
        raise ValueError(f"unknown gate {gate!r}; want one of A/B/C")
    report = GateReport(gate=gate, mode=GATE_TO_MODE[gate])
    for checker in GATE_CHECKS[gate]:
        try:
            report.checks.append(checker(cfg, gate))
        except Exception as exc:
            report.checks.append(GateCheck(
                name=getattr(checker, "__name__", "unknown"),
                status="fail",
                detail=f"{type(exc).__name__}: {exc}",
            ))
    return report


def certify(cfg: Config, gate: Gate) -> GateReport:
    """Run the gate and raise on failure — the boot equivalent of
    :func:`nerya.ops.preflight.require_ready`."""
    report = run_gate(cfg, gate)
    if not report.ok():
        lines = ", ".join(f"{c.name}:{c.detail}" for c in report.failures())
        raise RuntimeError(
            f"certification gate {gate} ({GATE_TO_MODE[gate]}) failed: {lines}"
        )
    return report


__all__ = [
    "Gate",
    "GateCheck",
    "GateReport",
    "GATE_TO_MODE",
    "FRESHNESS_BY_GATE",
    "REQUIRED_EVIDENCE",
    "run_gate",
    "certify",
]
