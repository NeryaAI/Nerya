"""Ingest pipeline shims for the trading evidence vault.

These functions are intentionally thin: they take known runtime payloads
(strategy proposal, backtest report, order/fill, account snapshot,
gateway transcript, research artifact) and emit a normalized
``EvidenceDoc`` via :func:`EvidenceStore.ingest`.

Real subsystems (strategy_history, trading, gateway, research) call into
these helpers when they emit a "decision worthy of audit". Until those
call sites are wired, the HTTP route ``POST /evidence/ingest/run`` can
exercise the same paths with synthetic payloads so the operator can see
the vault working end-to-end.
"""

from __future__ import annotations

from typing import Any, Optional

from .store import EvidenceDoc, EvidenceStore


def ingest_strategy_proposal(
    store: EvidenceStore,
    *,
    strategy_id: str,
    proposal_id: str,
    title: str,
    summary: str,
    body: str = "",
    tags: Optional[list[str]] = None,
    session_id: Optional[str] = None,
) -> EvidenceDoc:
    return store.ingest(
        source_type="strategy",
        source_id=f"strategy:{strategy_id}:proposal:{proposal_id}",
        title=title,
        summary=summary,
        body=body or summary,
        tags=list(tags or []) + [f"strategy:{strategy_id}", "proposal"],
        scope="strategy",
        strategy_id=strategy_id,
        session_id=session_id,
        route="POST /evolution/apply",
        created_by="agent",
    )


def ingest_backtest(
    store: EvidenceStore,
    *,
    strategy_id: str,
    backtest_id: str,
    metrics: dict[str, Any],
    window: str = "",
    symbols: Optional[list[str]] = None,
    artifact_refs: Optional[list[str]] = None,
) -> EvidenceDoc:
    sym_str = ", ".join(symbols or [])
    summary = (
        f"Backtest {backtest_id} for {strategy_id} (window={window}, "
        f"symbols={sym_str}); metrics={metrics}"
    )
    return store.ingest(
        source_type="backtest",
        source_id=f"strategy:{strategy_id}:backtest:{backtest_id}",
        title=f"Backtest {backtest_id} - {strategy_id}",
        summary=summary,
        body=summary,
        tags=[f"strategy:{strategy_id}", "backtest"] + list(symbols or []),
        scope="strategy",
        strategy_id=strategy_id,
        artifact_refs=list(artifact_refs or []),
        route="POST /strategy/backtests",
        created_by="runtime",
    )


def ingest_trade(
    store: EvidenceStore,
    *,
    account_id: str,
    order_id: str,
    symbol: str,
    side: str,
    qty: float,
    status: str,
    strategy_id: Optional[str] = None,
    rejection_reason: Optional[str] = None,
) -> EvidenceDoc:
    summary = (
        f"Order {order_id} on {account_id}: {side} {qty} {symbol} -> {status}"
    )
    if rejection_reason:
        summary += f"; rejection={rejection_reason}"
    return store.ingest(
        source_type="trade",
        source_id=f"order:{order_id}",
        title=f"Order {order_id} {status}",
        summary=summary,
        body=summary,
        tags=[symbol, status, "trade"] + ([f"strategy:{strategy_id}"] if strategy_id else []),
        scope="shared",
        strategy_id=strategy_id,
        route="POST /trading/submit",
        created_by="runtime",
    )


def ingest_account_snapshot(
    store: EvidenceStore,
    *,
    account_id: str,
    snapshot_id: str,
    body: str,
) -> EvidenceDoc:
    summary = f"Account {account_id} snapshot {snapshot_id}"
    return store.ingest(
        source_type="account",
        source_id=f"account:{account_id}:snapshot:{snapshot_id}",
        title=summary,
        summary=summary,
        body=body,
        tags=[f"account:{account_id}", "account_snapshot"],
        scope="shared",
        route="cron:account_refresh",
        created_by="runtime",
    )


def ingest_gateway(
    store: EvidenceStore,
    *,
    channel: str,
    event_id: str,
    direction: str,  # "inbound" | "outbound"
    body: str,
    operator_id: Optional[str] = None,
) -> EvidenceDoc:
    summary = f"Gateway {direction} on {channel} event={event_id}"
    return store.ingest(
        source_type="gateway",
        source_id=f"gateway:{channel}:{event_id}",
        title=summary,
        summary=summary,
        body=body,
        tags=[channel, direction, "gateway"],
        scope="shared",
        route="POST /gateway/inbound",
        created_by="operator" if direction == "inbound" else "runtime",
    )


def ingest_research(
    store: EvidenceStore,
    *,
    provider: str,
    artifact_id: str,
    title: str,
    body: str,
    tags: Optional[list[str]] = None,
) -> EvidenceDoc:
    return store.ingest(
        source_type="research",
        source_id=f"research:{provider}:{artifact_id}",
        title=title,
        summary=title,
        body=body,
        tags=[provider, "research"] + list(tags or []),
        scope="shared",
        route="POST /scripts/run",
        created_by="agent",
    )


def run_synthetic_demo(store: EvidenceStore) -> list[EvidenceDoc]:
    """Emit a small batch of demo evidence so the HTTP smoke can see hits.

    Demo records are ingested with ``scope="shared"`` so the operator
    dashboard can see them without bypassing the ACL. Real subsystem call
    sites (`ingest_strategy_proposal`, `ingest_backtest`) still write at
    ``scope="strategy"`` and are only visible to callers passing the
    matching ``strategy_id``.
    """
    docs: list[EvidenceDoc] = []
    docs.append(store.ingest(
        source_type="strategy",
        source_id="strategy:demo_btc_momentum:proposal:p_001",
        title="[demo] BTC momentum proposal",
        summary="Increase 4h momentum lookback to 24 bars.",
        body="Backtest 45 days; uplift +12bps; sharpe +0.3.",
        tags=["btc", "momentum", "demo", "proposal"],
        scope="shared",
        route="POST /evidence/ingest/run",
        created_by="operator",
    ))
    docs.append(store.ingest(
        source_type="backtest",
        source_id="strategy:demo_btc_momentum:backtest:b_2026_05_13",
        title="[demo] Backtest b_2026_05_13 - btc_momentum",
        summary=(
            "Backtest b_2026_05_13 for btc_momentum "
            "(window=45d, symbols=BTCUSDT); metrics={'sharpe': 1.45, 'max_dd': -0.09, "
            "'pnl_usd': 1234.5}"
        ),
        body=(
            "Backtest b_2026_05_13 for btc_momentum (window=45d, symbols=BTCUSDT); "
            "sharpe=1.45, max_dd=-0.09, pnl_usd=1234.5"
        ),
        tags=["btc_momentum", "backtest", "BTCUSDT", "demo"],
        scope="shared",
        artifact_refs=["result://bt_001"],
        route="POST /evidence/ingest/run",
        created_by="operator",
    ))
    docs.append(ingest_trade(
        store,
        account_id="paper_main",
        order_id="o_test_001",
        symbol="BTCUSDT",
        side="buy",
        qty=0.01,
        status="filled",
        strategy_id="btc_momentum",
    ))
    return docs
