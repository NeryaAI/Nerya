"""Strategy lifecycle graph.

Strategies are first-class runtime
entities with an explicit promotion graph. The previous shape only
had ``draft / paper / canary / live / paused / archived``; the
control-plane refactor adds a richer ramp:

States
------
- ``draft``         — agent-authored package, not yet validated.
- ``static_review`` — passed schema + static analyzer (no direct
  connector / vault / network imports). requires this gate.
- ``backtested``    — has at least one accepted backtest evidence
  artifact attached. Required before any account-bound mode.
- ``paper``         — paper-only execution against a virtual ledger.
- ``shadow``        — runs against a *real* account snapshot but the
  control plane never places orders; intents are journalled for
  side-by-side comparison with paper.
- ``canary``        — places real orders, but with a tighter cap, a
  protection rule requirement, and a per-trade approval gate.
- ``live``          — full real-money execution with kill switch.
- ``paused``        — temporarily halted; resumable.
- ``quarantined``   — incident-flagged; only paused or archived
  transitions allowed until an operator clears it.
- ``archived``      — terminal, no further transitions.

The :data:`ALLOWED_TRANSITIONS` graph is the only authority. Every
caller that wants to change a strategy's status must go through
:func:`validate_transition` so the lifecycle stays auditable.
:data:`PROMOTION_TARGETS` enumerates the *forward* edges that the
promotion gate (``nerya.trading.promotion``) is allowed to drive
automatically — sideways moves (``paper -> paused``) and rollbacks
(``canary -> paper``) stay manual.
"""

from __future__ import annotations

from typing import Final


STATES: Final[tuple[str, ...]] = (
    "draft",
    "static_review",
    "backtested",
    "paper",
    "shadow",
    "canary",
    "live",
    "paused",
    "quarantined",
    "archived",
)

# States from which the strategy may emit intents to the risk gate.
TRADABLE_STATES: Final[frozenset[str]] = frozenset({
    "paper", "shadow", "canary", "live",
})

# States that touch real-money accounts. Shadow does not place orders,
# but it does read real snapshots and write a parallel intent stream.
LIVE_STATES: Final[frozenset[str]] = frozenset({"shadow", "canary", "live"})

# States that actually result in orders being placed (paper, canary,
# live). Shadow does not place orders.
EXECUTING_STATES: Final[frozenset[str]] = frozenset({"paper", "canary", "live"})

# States from which the strategy is allowed to bind to a non-paper
# account. Anything earlier must promote through ``backtested`` first.
ACCOUNT_BINDABLE_STATES: Final[frozenset[str]] = frozenset({
    "shadow", "canary", "live", "paused",
})


ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "draft":         frozenset({"static_review", "archived"}),
    "static_review": frozenset({"backtested", "draft", "quarantined", "archived"}),
    "backtested":    frozenset({"paper", "static_review", "quarantined", "archived"}),
    "paper":         frozenset({"shadow", "canary", "paused", "quarantined", "archived"}),
    "shadow":        frozenset({"canary", "paper", "paused", "quarantined", "archived"}),
    "canary":        frozenset({"live", "paper", "shadow", "paused", "quarantined", "archived"}),
    "live":          frozenset({"paused", "canary", "quarantined", "archived"}),
    "paused":        frozenset({"paper", "shadow", "canary", "live", "quarantined", "archived"}),
    "quarantined":   frozenset({"paused", "archived"}),
    "archived":      frozenset(),
}


# Forward promotions that the promotion gate is allowed to drive.
# Anything else (sideways / rollback / quarantine) must be operator-
# initiated through ``strategy_crud.set_status``.
PROMOTION_TARGETS: Final[dict[str, str]] = {
    "draft":         "static_review",
    "static_review": "backtested",
    "backtested":    "paper",
    "paper":         "shadow",
    "shadow":        "canary",
    "canary":        "live",
}


class InvalidTransition(ValueError):
    """Raised when a caller asks for a status change that the graph forbids."""


def validate_transition(current: str, target: str) -> None:
    if current not in ALLOWED_TRANSITIONS:
        raise InvalidTransition(f"unknown current status: {current!r}")
    if target not in ALLOWED_TRANSITIONS:
        raise InvalidTransition(f"unknown target status: {target!r}")
    if current == target:
        return
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(
            f"illegal transition {current!r} -> {target!r}; "
            f"allowed from {current!r}: {sorted(ALLOWED_TRANSITIONS[current])}"
        )


def is_tradable(status: str) -> bool:
    return status in TRADABLE_STATES


def is_live(status: str) -> bool:
    return status in LIVE_STATES


def is_executing(status: str) -> bool:
    """True iff orders are actually placed for this state.

    Shadow returns ``False`` because the control plane stops at
    journalling the intent + budget reservation; no order is sent
    to the venue or the paper ledger.
    """
    return status in EXECUTING_STATES


def is_account_bindable(status: str) -> bool:
    """True iff the strategy may legally point at a non-paper account.

    A ``draft`` / ``static_review`` / ``backtested`` strategy must not
    receive an account binding to a real-money account profile —
    verification #1.
    """
    return status in ACCOUNT_BINDABLE_STATES


def promotion_target(current: str) -> str | None:
    """Return the next forward state if any, else None."""
    return PROMOTION_TARGETS.get(current)


__all__ = [
    "STATES",
    "TRADABLE_STATES",
    "LIVE_STATES",
    "EXECUTING_STATES",
    "ACCOUNT_BINDABLE_STATES",
    "ALLOWED_TRANSITIONS",
    "PROMOTION_TARGETS",
    "InvalidTransition",
    "validate_transition",
    "is_tradable",
    "is_live",
    "is_executing",
    "is_account_bindable",
    "promotion_target",
]
