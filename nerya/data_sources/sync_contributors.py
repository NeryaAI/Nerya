"""Default ``sync_now`` contributors for the runtime data-source ledger.

Each contributor is a callable ``(client, source_id) -> dict``. They are
registered with :func:`nerya.data_sources.sync_state.register_sync_now`
so that hitting ``POST /data-sources/sync-now`` actually performs the
refresh and updates the ledger's freshness row, instead of falling
through to the ``marker_only`` placeholder.

This module is imported once at API startup (see
``nerya.api.local_server._ensure_sync_contributors``). It is intentionally
defensive: any contributor that raises is wrapped, recorded via
:func:`mark_error`, and surfaced in the operator envelope.

Real subsystems can override or extend the registry by calling
:func:`register_sync_now` directly — these are the platform defaults.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import sync_state as ss


# ---------------------------------------------------------------------------
# memory:notebook  -- workspace memory provider freshness
# ---------------------------------------------------------------------------


def _notebook_root(client: Any) -> Path:
    """Return the workspace memory/notebook directory."""

    cfg = client.config
    return cfg.paths.state / "memory" / "notebook"


def _sync_memory_notebook(client: Any, source_id: str) -> dict[str, Any]:
    """Refresh the workspace notebook freshness row.

    The contributor inspects the notebook directory's last modification
    time as a cheap freshness signal. It records an attempt, then either
    a success (with the notebook cursor) or an error if the directory is
    unreachable.
    """

    ss.mark_attempt(
        client,
        source_id,
        kind="memory_provider",
        provider="filesystem",
        freshness_sla_seconds=3600,
    )
    try:
        root = _notebook_root(client)
        root.mkdir(parents=True, exist_ok=True)
        latest_mtime = 0.0
        file_count = 0
        for entry in root.rglob("*"):
            if entry.is_file():
                file_count += 1
                try:
                    mtime = entry.stat().st_mtime
                    if mtime > latest_mtime:
                        latest_mtime = mtime
                except OSError:
                    continue
        cursor = f"mtime:{latest_mtime:.0f}:files:{file_count}"
        row = ss.mark_success(client, source_id, cursor=cursor)
        return {
            "ok": True,
            "source_id": source_id,
            "row": row,
            "note": "notebook scanned",
            "files": file_count,
            "latest_mtime": latest_mtime,
        }
    except Exception as exc:
        ss.mark_error(client, source_id, message=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "source_id": source_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# llm:model_catalog -- LLM model registry freshness
# ---------------------------------------------------------------------------


def _sync_llm_model_catalog(client: Any, source_id: str) -> dict[str, Any]:
    """Refresh the LLM model catalog row.

    Inspects ``workspace/llm/model_catalog.json`` which is owned by
    :class:`nerya.llm.model_catalog.ModelCatalog`. If the cache exists we
    record success with a cursor derived from ``updated_at`` and the
    provider count. We deliberately do *not* re-call provider ``/models``
    here — that's a heavier action triggered via the LLM settings tab.
    """

    ss.mark_attempt(
        client,
        source_id,
        kind="model_provider",
        provider="registry",
        freshness_sla_seconds=86400,
    )
    try:
        from ..llm.model_catalog import ModelCatalog

        catalog = ModelCatalog(workspace=client.config.paths.root)
        doc = catalog.load() or {}
        providers = doc.get("providers") or {}
        provider_count = len(providers)
        model_count = sum(len(v or []) for v in providers.values())
        updated_at = doc.get("updated_at") or ""
        if not updated_at:
            # No cache yet — leave the row stale so the operator knows
            # to open Settings > LLM and run a refresh.
            ss.mark_error(
                client,
                source_id,
                message="model catalog cache not initialised",
            )
            return {
                "ok": False,
                "source_id": source_id,
                "error": "model catalog cache not initialised",
                "providers": provider_count,
            }
        cursor = f"updated_at:{updated_at};providers:{provider_count};models:{model_count}"
        row = ss.mark_success(client, source_id, cursor=cursor)
        return {
            "ok": True,
            "source_id": source_id,
            "row": row,
            "providers": provider_count,
            "models": model_count,
            "updated_at": updated_at,
        }
    except Exception as exc:
        ss.mark_error(client, source_id, message=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "source_id": source_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# gateway:platforms -- messaging gateway registry freshness
# ---------------------------------------------------------------------------


def _sync_gateway_platforms(client: Any, source_id: str) -> dict[str, Any]:
    """Refresh the gateway platform registry row.

    Reads the configured platforms from ``messaging.platforms`` in
    ``nerya.yml`` and cross-references the static :mod:`nerya.messaging.platforms`
    registry. The cursor counts configured + supported platforms so a
    drop in either is observable in the dashboard.
    """

    ss.mark_attempt(
        client,
        source_id,
        kind="gateway",
        provider="registry",
        freshness_sla_seconds=600,
    )
    try:
        from ..messaging.platforms import list_platforms

        supported = list_platforms() or []
        configured = client.config.get("messaging.platforms") or []
        if not isinstance(configured, list):
            configured = []
        cursor = (
            f"supported:{len(supported)};configured:{len(configured)}"
        )
        row = ss.mark_success(client, source_id, cursor=cursor)
        return {
            "ok": True,
            "source_id": source_id,
            "row": row,
            "supported_count": len(supported),
            "configured_count": len(configured),
        }
    except Exception as exc:
        ss.mark_error(client, source_id, message=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "source_id": source_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# account:paper_main -- paper-trading account ledger freshness
# ---------------------------------------------------------------------------


_PAPER_ACCOUNT_ID = "paper_main"


def _sync_account_paper_main(client: Any, source_id: str) -> dict[str, Any]:
    """Refresh the paper trading account row.

    Reads ``workspace/state/virtual_ledgers/paper_main.json``. Uses the
    ledger's ``trade_count`` + ``fees_paid_usd`` as the cursor so a new
    fill bumps the cursor and the operator immediately sees freshness.
    """

    ss.mark_attempt(
        client,
        source_id,
        kind="trading_account",
        provider="paper",
        account_id=_PAPER_ACCOUNT_ID,
        freshness_sla_seconds=900,
    )
    try:
        ledger_path = (
            client.config.paths.virtual_ledgers / f"{_PAPER_ACCOUNT_ID}.json"
        )
        if not ledger_path.exists():
            ss.mark_error(
                client,
                source_id,
                message="paper ledger not initialised",
            )
            return {
                "ok": False,
                "source_id": source_id,
                "error": "paper ledger not initialised",
                "expected_path": str(ledger_path),
            }
        state = json.loads(ledger_path.read_text(encoding="utf-8"))
        positions = state.get("positions") or {}
        cash = state.get("cash_usd") or 0
        trades = state.get("trade_count") or 0
        cursor = (
            f"cash:{float(cash):.2f};positions:{len(positions)};trades:{trades}"
        )
        row = ss.mark_success(client, source_id, cursor=cursor)
        return {
            "ok": True,
            "source_id": source_id,
            "row": row,
            "cash_usd": cash,
            "position_count": len(positions),
            "trade_count": trades,
        }
    except Exception as exc:
        ss.mark_error(client, source_id, message=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "source_id": source_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# market:public_ccxt -- best-effort public market data ping
# ---------------------------------------------------------------------------


def _sync_market_public_ccxt(client: Any, source_id: str) -> dict[str, Any]:
    """Touch a public market client via :mod:`nerya.trading.market_clients`.

    We deliberately avoid hard requirements: if ccxt isn't installed or
    the network is offline, this records an error and returns gracefully.
    Operators can disable the source from ``/data-sources/status`` if they
    don't want it polled.
    """

    ss.mark_attempt(
        client,
        source_id,
        kind="market_data",
        provider="ccxt",
        freshness_sla_seconds=300,
    )
    if os.environ.get("NERYA_DISABLE_NETWORK", "").strip().lower() in {"1", "true", "yes"}:
        ss.mark_error(client, source_id, message="network disabled by NERYA_DISABLE_NETWORK")
        return {
            "ok": False,
            "source_id": source_id,
            "error": "network disabled",
        }
    try:
        try:
            import ccxt  # type: ignore
        except Exception as exc:
            ss.mark_error(client, source_id, message=f"ccxt unavailable: {exc}")
            return {
                "ok": False,
                "source_id": source_id,
                "error": "ccxt unavailable",
            }
        exchange = ccxt.binance({"enableRateLimit": True, "timeout": 5000})
        ticker = exchange.fetch_ticker("BTC/USDT")
        last = ticker.get("last") if isinstance(ticker, dict) else None
        cursor = f"binance:btcusdt:{last}"
        row = ss.mark_success(client, source_id, cursor=cursor)
        return {
            "ok": True,
            "source_id": source_id,
            "row": row,
            "exchange": "binance",
            "symbol": "BTC/USDT",
            "last": last,
        }
    except Exception as exc:
        ss.mark_error(client, source_id, message=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "source_id": source_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# Registry bootstrap
# ---------------------------------------------------------------------------


_INSTALLED = False


def install_default_contributors(*, force: bool = False) -> None:
    """Register the built-in contributors with the sync ledger.

    Safe to call repeatedly; idempotent within a single process. The
    HTTP server calls this once at startup. Tests can call it with
    ``force=True`` if they reset the registry.
    """

    global _INSTALLED
    if _INSTALLED and not force:
        return
    ss.register_sync_now("memory:notebook", _sync_memory_notebook)
    ss.register_sync_now("llm:model_catalog", _sync_llm_model_catalog)
    ss.register_sync_now("gateway:platforms", _sync_gateway_platforms)
    ss.register_sync_now(f"account:{_PAPER_ACCOUNT_ID}", _sync_account_paper_main)
    ss.register_sync_now("market:public_ccxt", _sync_market_public_ccxt)
    _INSTALLED = True


def seed_additional_rows(client: Any) -> None:
    """Ensure the new contributor rows exist in the ledger so the
    dashboard renders them on first load (before any sync_now call).
    """

    extras = [
        ("account:paper_main", "trading_account", "paper", 900),
        ("market:public_ccxt", "market_data", "ccxt", 300),
    ]
    for sid, kind, provider, sla in extras:
        if ss.get(client, sid) is None:
            ss.mark_attempt(
                client,
                sid,
                kind=kind,
                provider=provider,
                freshness_sla_seconds=sla,
            )


__all__ = [
    "install_default_contributors",
    "seed_additional_rows",
]
