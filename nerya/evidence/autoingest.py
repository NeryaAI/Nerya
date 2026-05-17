"""Auto-ingest hooks for the Trading Evidence Vault.

Subsystems call these helpers when they reach a "decision worth auditing"
(a strategy promotion, a backtest finalize, a gateway health change, a
research note save, an order fill). Each hook:

- Honors the ``runtime.evidence_vault`` feature flag (no-op when off).
- Opens the workspace evidence store via
  :func:`nerya.evidence.store.open_store`.
- Wraps :mod:`nerya.evidence.ingest` helpers in defensive try/except so a
  vault failure can never break the calling subsystem.
- Always returns the ingested :class:`EvidenceDoc` (or ``None`` on no-op
  / failure) so callers can attach the workspace_path to their own
  artifacts if they want.

These hooks are the runtime-closed-loop counterpart to the manual
``POST /evidence/ingest/run`` smoke route — once a hook is wired into
its real call site, the vault fills automatically as the platform runs.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import ingest as _ingest
from .store import EvidenceDoc, EvidenceStore, open_store


_LOG = logging.getLogger(__name__)

_FLAG = "runtime.evidence_vault"


def _flag_enabled(client: Any) -> bool:
    try:
        from ..runtime import feature_flags as ff
        return bool(ff.is_enabled(client, _FLAG))
    except Exception:  # pragma: no cover - defensive
        return True


def _store(client: Any) -> Optional[EvidenceStore]:
    if not _flag_enabled(client):
        return None
    try:
        return open_store(client)
    except Exception:  # pragma: no cover - defensive
        _LOG.exception("evidence.autoingest: failed to open store")
        return None


# ---------------------------------------------------------------------------
# Decision-point hooks
# ---------------------------------------------------------------------------


def on_strategy_promote(
    client: Any,
    *,
    strategy_id: str,
    proposal_id: str,
    title: str,
    summary: str,
    body: str = "",
    tags: Optional[list[str]] = None,
    session_id: Optional[str] = None,
) -> Optional[EvidenceDoc]:
    """Called after a strategy promotion / evolution apply succeeds."""

    store = _store(client)
    if store is None:
        return None
    try:
        return _ingest.ingest_strategy_proposal(
            store,
            strategy_id=strategy_id,
            proposal_id=proposal_id,
            title=title,
            summary=summary,
            body=body,
            tags=tags,
            session_id=session_id,
        )
    except Exception:
        _LOG.exception("evidence.autoingest: on_strategy_promote failed")
        return None


def on_backtest_finalize(
    client: Any,
    *,
    strategy_id: str,
    backtest_id: str,
    metrics: dict[str, Any],
    window: str = "",
    symbols: Optional[list[str]] = None,
    artifact_refs: Optional[list[str]] = None,
) -> Optional[EvidenceDoc]:
    """Called when a backtest run reaches a terminal state with metrics."""

    store = _store(client)
    if store is None:
        return None
    try:
        return _ingest.ingest_backtest(
            store,
            strategy_id=strategy_id,
            backtest_id=backtest_id,
            metrics=metrics,
            window=window,
            symbols=symbols,
            artifact_refs=artifact_refs,
        )
    except Exception:
        _LOG.exception("evidence.autoingest: on_backtest_finalize failed")
        return None


def on_order_filled(
    client: Any,
    *,
    account_id: str,
    order_id: str,
    symbol: str,
    side: str,
    qty: float,
    status: str,
    strategy_id: Optional[str] = None,
    rejection_reason: Optional[str] = None,
) -> Optional[EvidenceDoc]:
    """Called after an order reaches a terminal status (filled / rejected)."""

    store = _store(client)
    if store is None:
        return None
    try:
        return _ingest.ingest_trade(
            store,
            account_id=account_id,
            order_id=order_id,
            symbol=symbol,
            side=side,
            qty=qty,
            status=status,
            strategy_id=strategy_id,
            rejection_reason=rejection_reason,
        )
    except Exception:
        _LOG.exception("evidence.autoingest: on_order_filled failed")
        return None


def on_account_snapshot(
    client: Any,
    *,
    account_id: str,
    snapshot_id: str,
    body: str,
) -> Optional[EvidenceDoc]:
    """Called after a periodic account snapshot is taken."""

    store = _store(client)
    if store is None:
        return None
    try:
        return _ingest.ingest_account_snapshot(
            store,
            account_id=account_id,
            snapshot_id=snapshot_id,
            body=body,
        )
    except Exception:
        _LOG.exception("evidence.autoingest: on_account_snapshot failed")
        return None


def on_gateway_event(
    client: Any,
    *,
    channel: str,
    event_id: str,
    direction: str,
    body: str,
    operator_id: Optional[str] = None,
) -> Optional[EvidenceDoc]:
    """Called when a gateway emits an inbound/outbound event of audit interest.

    Only ``direction in ("inbound", "outbound")`` is accepted; other
    values are coerced to ``"outbound"`` so the call site never raises.
    """

    if direction not in ("inbound", "outbound"):
        direction = "outbound"
    store = _store(client)
    if store is None:
        return None
    try:
        return _ingest.ingest_gateway(
            store,
            channel=channel,
            event_id=event_id,
            direction=direction,
            body=body,
            operator_id=operator_id,
        )
    except Exception:
        _LOG.exception("evidence.autoingest: on_gateway_event failed")
        return None


def on_research_save(
    client: Any,
    *,
    provider: str,
    artifact_id: str,
    title: str,
    body: str,
    tags: Optional[list[str]] = None,
) -> Optional[EvidenceDoc]:
    """Called after a research artifact (web fetch, notebook entry, etc.) is saved."""

    store = _store(client)
    if store is None:
        return None
    try:
        return _ingest.ingest_research(
            store,
            provider=provider,
            artifact_id=artifact_id,
            title=title,
            body=body,
            tags=tags,
        )
    except Exception:
        _LOG.exception("evidence.autoingest: on_research_save failed")
        return None


__all__ = [
    "on_strategy_promote",
    "on_backtest_finalize",
    "on_order_filled",
    "on_account_snapshot",
    "on_gateway_event",
    "on_research_save",
]
