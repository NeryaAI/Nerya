"""Operator-facing runtime health and certification surfaces.

Everything here is allowed to reach across the rest of the runtime
because it composes the final operator-level evidence: preflight
readiness, certification gates, runbook-shaped snapshots, etc. Do
not put low-level primitives here — those belong in
:mod:`nerya.core`.
"""

from .certification import (
    GATE_TO_MODE,
    GateCheck,
    GateReport,
    certify,
    run_gate,
)
from .preflight import (
    Check,
    PreflightReport,
    require_ready,
    run_preflight,
)

__all__ = [
    "Check",
    "PreflightReport",
    "run_preflight",
    "require_ready",
    "GATE_TO_MODE",
    "GateCheck",
    "GateReport",
    "run_gate",
    "certify",
]
