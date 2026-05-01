"""Reconciliation — local projection vs. exchange truth.

04-29 §5.2 / §11 (P4) — three passes:

1. ``reconcile_strategy`` (legacy): per-strategy fills journal vs.
   :class:`VirtualLedger`. Kept stable so the existing CLI/cron path
   keeps working.
2. ``reconcile_local`` (new): local ``fills`` table vs. ``orders``
   roll-up and :class:`PositionBook` projection.
3. ``reconcile_account`` (new): live exchange truth (positions,
   balances, open orders) vs. our :class:`PositionBook` /
   :class:`OrderTracker`.

The new pipeline writes a row into ``reconciliation_reports`` for
every run, severity-tagged. ``info`` and ``warning`` are non-blocking;
``action_required`` and ``trading_halted`` are surfaced to the risk
gate and dashboard for operator action. mandates "report,
do not auto-heal" — drift never silently mutates the real position.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..core import jsonl
from ..core.config import Config
from ..core.ids import reconcile_id as _new_reconcile_id
from ..core.paths import WorkspacePaths
from ..db.sqlite import connect

log = logging.getLogger(__name__)

_SIZE_TOLERANCE = 1e-8
_PRICE_TOLERANCE_BPS = 2  # 2 bps price drift tolerated


@dataclass
class ReconcileReport:
    strategy_id: str
    account_id: str
    fills_count: int
    ledger_positions: int
    positions: dict[str, dict[str, Any]]
    issues: list[dict[str, Any]]
    summary: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "account_id": self.account_id,
            "fills_count": self.fills_count,
            "ledger_positions": self.ledger_positions,
            "positions": self.positions,
            "issues": self.issues,
            "summary": self.summary,
        }


def reconcile_strategy(
    paths: WorkspacePaths,
    strategy_id: str,
    *,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Reconcile a single strategy's fills against the virtual ledger.

    Parameters
    ----------
    paths: workspace paths.
    strategy_id: strategy to inspect.
    account_id: if None, derived from the fills (fallback to the first id
        observed in the journal).
    """
    fills_path = paths.strategy_history(strategy_id) / "fills.jsonl"
    fills: list[dict[str, Any]] = jsonl.read_all(fills_path) if fills_path.exists() else []

    resolved_account = account_id or _infer_account_id(fills) or "paper_main"
    ledger_path = paths.virtual_ledgers / f"{resolved_account}.json"
    ledger_doc: dict[str, Any] = {}
    if ledger_path.exists():
        try:
            ledger_doc = json.loads(ledger_path.read_text(encoding="utf-8"))
        except Exception:
            ledger_doc = {}

    # Rebuild positions from fills
    replay: dict[str, dict[str, float]] = {}
    fees = 0.0
    for f in fills:
        market = f.get("market") or f.get("symbol")
        side = (f.get("side") or "").lower()
        size = float(f.get("size") or 0.0)
        price = float(f.get("price") or 0.0)
        fee = float(f.get("fee_usd") or f.get("fee") or 0.0)
        if not market or side not in ("buy", "sell") or size <= 0:
            continue
        acc = replay.setdefault(market, {"net_size": 0.0, "notional": 0.0,
                                            "avg_price": 0.0})
        signed = size if side == "buy" else -size
        prior = acc["net_size"]
        new_size = prior + signed
        if prior == 0 or (prior > 0 and signed > 0) or (prior < 0 and signed < 0):
            # opening / adding
            if new_size != 0:
                acc["avg_price"] = (
                    acc["avg_price"] * prior + price * signed
                ) / new_size if prior != 0 else price
        acc["net_size"] = new_size
        acc["notional"] += price * size
        fees += fee

    ledger_positions = (ledger_doc.get("positions") or {})
    issues: list[dict[str, Any]] = []

    # markets present in either side
    markets = set(replay.keys()) | set(ledger_positions.keys())
    position_report: dict[str, dict[str, Any]] = {}
    for m in sorted(markets):
        replayed = replay.get(m, {"net_size": 0.0, "avg_price": 0.0, "notional": 0.0})
        ledgered = ledger_positions.get(m) or {"size": 0.0, "avg_price": 0.0}
        size_diff = float(ledgered.get("size", 0.0)) - replayed["net_size"]
        avg_ledger = float(ledgered.get("avg_price", 0.0))
        price_bps_diff = _bps_diff(avg_ledger, replayed["avg_price"])
        position_report[m] = {
            "replayed": replayed,
            "ledgered": ledgered,
            "size_diff": size_diff,
            "price_bps_diff": price_bps_diff,
        }
        if abs(size_diff) > _SIZE_TOLERANCE:
            issues.append({"kind": "size_mismatch", "market": m,
                            "size_diff": size_diff})
        if price_bps_diff > _PRICE_TOLERANCE_BPS and replayed["net_size"] != 0:
            issues.append({"kind": "avg_price_mismatch", "market": m,
                            "bps": price_bps_diff})
        if m not in ledger_positions and replayed["net_size"] != 0:
            issues.append({"kind": "missing_in_ledger", "market": m})
        if m not in replay and float(ledgered.get("size", 0.0)) != 0:
            issues.append({"kind": "missing_in_fills", "market": m})

    fees_ledger = float(ledger_doc.get("fees_paid_usd", 0.0))
    if abs(fees_ledger - fees) > 1e-6:
        issues.append({"kind": "fees_mismatch",
                        "fees_from_fills": fees,
                        "fees_from_ledger": fees_ledger})

    summary = {
        "markets": len(markets),
        "issues": len(issues),
        "ledger_cash_usd": float(ledger_doc.get("cash_usd", 0.0)),
        "ledger_fees_usd": fees_ledger,
        "fills_fees_usd": fees,
    }

    return ReconcileReport(
        strategy_id=strategy_id,
        account_id=resolved_account,
        fills_count=len(fills),
        ledger_positions=len(ledger_positions),
        positions=position_report,
        issues=issues,
        summary=summary,
    ).as_dict()


def _infer_account_id(fills: list[dict[str, Any]]) -> str | None:
    for f in fills:
        aid = f.get("account_id") or f.get("account")
        if aid:
            return aid
    return None


def _bps_diff(a: float, b: float) -> float:
    if a == 0 and b == 0:
        return 0.0
    mid = (a + b) / 2 or a or b or 1.0
    return abs(a - b) / abs(mid) * 10_000


# ---------------------------------------------------------------------------
# control-plane reconciliation pipeline.
# ---------------------------------------------------------------------------


ReconcileScope = Literal[
    "strategy",
    "local",
    "account",
    "active_orders",
    "global",
]
ReconcileSeverity = Literal[
    "info",
    "warning",
    "action_required",
    "trading_halted",
]


@dataclass
class ReconciliationReport:
    """severity-tagged record of a single reconciliation pass."""

    report_id: str
    ts: float
    scope: ReconcileScope
    severity: ReconcileSeverity
    account_id: str | None = None
    strategy_id: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_SEVERITY_ORDER: dict[ReconcileSeverity, int] = {
    "info": 0,
    "warning": 1,
    "action_required": 2,
    "trading_halted": 3,
}


def _max_severity(*levels: ReconcileSeverity) -> ReconcileSeverity:
    if not levels:
        return "info"
    return max(levels, key=lambda lv: _SEVERITY_ORDER[lv])


# ---------------------------------------------------------------------------
# Report store
# ---------------------------------------------------------------------------


class ReconciliationStore:
    """Persistent record of every reconciliation pass.

    The dashboard / RiskGate / operator inbox all read from here. Use
    :meth:`record` to write a freshly-built :class:`ReconciliationReport`,
    :meth:`recent` for the dashboard list, and :meth:`worst_recent`
    for the RiskGate's halt-on-severity check.
    """

    def __init__(self, paths: WorkspacePaths):
        self.paths = paths
        self._con = None

    def _con_lazy(self):
        if self._con is None:
            self._con = connect(self.paths.db)
        return self._con

    def record(self, report: ReconciliationReport) -> None:
        con = self._con_lazy()
        con.execute(
            """
            INSERT INTO reconciliation_reports (
                report_id, ts, scope, account_id, strategy_id, severity,
                summary_json, issues_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.report_id, report.ts, report.scope,
                report.account_id, report.strategy_id, report.severity,
                json.dumps(report.summary),
                json.dumps(report.issues),
            ),
        )

    def recent(
        self,
        *,
        account_id: str | None = None,
        scope: ReconcileScope | None = None,
        limit: int = 50,
    ) -> list[ReconciliationReport]:
        sql = "SELECT * FROM reconciliation_reports"
        clauses: list[str] = []
        params: list[Any] = []
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        rows = self._con_lazy().execute(sql, tuple(params)).fetchall()
        return [_row_to_report(r) for r in rows]

    def worst_recent(
        self,
        *,
        account_id: str | None = None,
        within_seconds: float = 600,
    ) -> ReconciliationReport | None:
        cutoff = time.time() - float(within_seconds)
        sql = "SELECT * FROM reconciliation_reports WHERE ts >= ?"
        params: list[Any] = [cutoff]
        if account_id:
            sql += " AND account_id = ?"
            params.append(account_id)
        rows = self._con_lazy().execute(sql, tuple(params)).fetchall()
        if not rows:
            return None
        reports = [_row_to_report(r) for r in rows]
        return max(reports, key=lambda r: _SEVERITY_ORDER[r.severity])


def _row_to_report(row: Any) -> ReconciliationReport:
    return ReconciliationReport(
        report_id=str(row["report_id"]),
        ts=float(row["ts"] or 0.0),
        scope=str(row["scope"] or "global"),  # type: ignore[arg-type]
        severity=str(row["severity"] or "info"),  # type: ignore[arg-type]
        account_id=(row["account_id"] or None),
        strategy_id=(row["strategy_id"] or None),
        summary=json.loads(str(row["summary_json"] or "{}")),
        issues=list(json.loads(str(row["issues_json"] or "[]"))),
    )


# ---------------------------------------------------------------------------
# Pass 2: local-only reconciliation (fills <-> orders <-> positions)
# ---------------------------------------------------------------------------


def reconcile_local(
    paths: WorkspacePaths,
    *,
    account_id: str | None = None,
    persist: bool = True,
) -> ReconciliationReport:
    """Cross-check the durable local stores against each other.

    Catches a different class of bug than :func:`reconcile_strategy`:
    here we look at orders, fills, and the position book at the table
    level — these all live in SQLite and any divergence is purely a
    bookkeeping bug. Severity is at most ``warning`` because there's
    no exchange comparison.
    """
    con = connect(paths.db)
    issues: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}

    where = ""
    params: tuple[Any, ...] = ()
    if account_id:
        where = " WHERE o.account_id = ?"
        params = (account_id,)

    # 1) Filled orders should have matching fills summing to filled_size.
    rows = con.execute(
        f"""
        SELECT o.order_id, o.account_id, o.market, o.filled_size,
               COALESCE(SUM(f.size_base), 0) AS fill_sum
          FROM orders o
          LEFT JOIN fills f ON f.order_id = o.order_id
          {where}
         GROUP BY o.order_id
        """,
        params,
    ).fetchall()
    order_drift = 0
    for r in rows:
        filled = float(r["filled_size"] or 0.0)
        fill_sum = float(r["fill_sum"] or 0.0)
        if abs(filled - fill_sum) > 1e-8 and (filled > 0 or fill_sum > 0):
            order_drift += 1
            issues.append({
                "kind": "order_fills_mismatch",
                "order_id": str(r["order_id"]),
                "filled_size": filled,
                "fill_sum": fill_sum,
            })
    summary["orders_examined"] = len(rows)
    summary["order_fill_mismatches"] = order_drift

    # 2) Open positions in PositionBook should match the live fill projection.
    pos_where = " WHERE p.closed_at IS NULL"
    pos_params: tuple[Any, ...] = ()
    if account_id:
        pos_where += " AND p.account_id = ?"
        pos_params = (account_id,)
    open_positions = con.execute(
        f"""
        SELECT p.position_id, p.account_id, p.strategy_id, p.market, p.side,
               p.size_base, p.avg_entry_price
          FROM positions p
          {pos_where}
        """,
        pos_params,
    ).fetchall()
    summary["open_positions"] = len(open_positions)
    for p in open_positions:
        # Sum buy minus sell for the same account/strategy/market
        # within the position's lifetime.
        replay = con.execute(
            """
            SELECT side, COALESCE(SUM(size_base), 0) AS s
              FROM fills
             WHERE account_id = ? AND strategy_id = ? AND market = ?
             GROUP BY side
            """,
            (p["account_id"], p["strategy_id"], p["market"]),
        ).fetchall()
        net = 0.0
        for row in replay:
            sign = 1.0 if (row["side"] or "").lower() == "buy" else -1.0
            net += sign * float(row["s"] or 0.0)
        if abs(net - float(p["size_base"] or 0.0)) > 1e-6:
            issues.append({
                "kind": "position_fill_drift",
                "position_id": str(p["position_id"]),
                "market": str(p["market"]),
                "expected_net": net,
                "position_size": float(p["size_base"] or 0.0),
            })

    severity: ReconcileSeverity = "warning" if issues else "info"
    report = ReconciliationReport(
        report_id=_new_reconcile_id(),
        ts=time.time(),
        scope="local",
        severity=severity,
        account_id=account_id,
        summary=summary,
        issues=issues,
    )
    if persist:
        ReconciliationStore(paths).record(report)
    return report


# ---------------------------------------------------------------------------
# Pass 3: exchange-truth reconciliation
# ---------------------------------------------------------------------------


def reconcile_account(
    config: Config,
    account_id: str,
    *,
    persist: bool = True,
) -> ReconciliationReport:
    """Compare local PositionBook + OrderTracker against the venue.

    **never auto-heal**. Issues are written to the report
    so an operator can decide whether to attach/import an unexpected
    external position, accept an external close, or escalate.
    """
    from .accounts import get_account_profile
    from .order_tracker import OrderTracker
    from .position_book import PositionBook

    profile = get_account_profile(config.paths, account_id)
    issues: list[dict[str, Any]] = []
    summary: dict[str, Any] = {"mode": profile.mode}

    if profile.mode == "paper":
        # Paper mode has no exchange truth to compare against.
        report = ReconciliationReport(
            report_id=_new_reconcile_id(),
            ts=time.time(),
            scope="account",
            severity="info",
            account_id=account_id,
            summary={"mode": "paper", "note": "exchange comparison skipped"},
            issues=[],
        )
        if persist:
            ReconciliationStore(config.paths).record(report)
        return report

    # 1) Snapshot freshness / health.
    from .account_snapshots import latest_snapshot, capture_snapshot
    snap = latest_snapshot(config.paths, account_id)
    if snap is None:
        snap = capture_snapshot(config, account_id, profile=profile)
    summary["snapshot_health"] = snap.health
    summary["snapshot_age_s"] = max(0.0, time.time() - snap.ts)
    if snap.health != "ok":
        issues.append({
            "kind": "snapshot_unhealthy",
            "health": snap.health,
            "latency_ms": snap.latency_ms,
        })
    if snap.is_stale(max_age_s=float(config.get("trading.snapshot.max_age_seconds", 60))):
        issues.append({
            "kind": "snapshot_stale",
            "age_s": time.time() - snap.ts,
        })

    # 2) Open positions diff.
    book = PositionBook(config.paths)
    local_open = {p.market: p for p in book.open_positions(account_id=account_id)}
    summary["local_open_positions"] = len(local_open)

    exchange_positions = _fetch_exchange_positions(config, profile)
    summary["exchange_open_positions"] = len(exchange_positions)
    seen_markets: set[str] = set()
    for ex_pos in exchange_positions:
        market = ex_pos.get("market") or ""
        seen_markets.add(market)
        size = float(ex_pos.get("size_base") or 0.0)
        local = local_open.get(market)
        if local is None and abs(size) > _SIZE_TOLERANCE:
            issues.append({
                "kind": "external_position_detected",
                "market": market,
                "exchange_size": size,
            })
            continue
        if local is not None:
            local_signed = local.size_base
            if abs(local_signed - size) > 1e-6:
                issues.append({
                    "kind": "position_size_drift",
                    "market": market,
                    "local": local_signed,
                    "exchange": size,
                    "position_id": local.position_id,
                })
    for market, local in local_open.items():
        if market not in seen_markets:
            # Local thinks we hold something the exchange doesn't.
            issues.append({
                "kind": "external_closed",
                "market": market,
                "local_size": local.size_base,
                "position_id": local.position_id,
            })

    # 3) Active orders sanity check.
    tracker = OrderTracker(config.paths)
    active = tracker.active_orders(account_id=account_id)
    summary["active_orders"] = len(active)
    exchange_open_orders = _fetch_exchange_open_orders(config, profile)
    summary["exchange_open_orders"] = len(exchange_open_orders)
    exchange_oids = {str(o.get("order_id") or o.get("client_order_id") or "") for o in exchange_open_orders}
    for order in active:
        ex_id = order.exchange_order_id or order.client_order_id
        if ex_id and ex_id not in exchange_oids:
            issues.append({
                "kind": "active_order_missing_at_exchange",
                "order_id": order.order_id,
                "client_order_id": order.client_order_id,
                "exchange_order_id": order.exchange_order_id,
            })

    severity = _classify_account_severity(issues)
    report = ReconciliationReport(
        report_id=_new_reconcile_id(),
        ts=time.time(),
        scope="account",
        severity=severity,
        account_id=account_id,
        summary=summary,
        issues=issues,
    )
    if persist:
        ReconciliationStore(config.paths).record(report)
        if severity in ("action_required", "trading_halted"):
            try:
                jsonl.append(config.paths.journal("reconciliation"), report.as_dict())
            except Exception:  # pragma: no cover
                log.exception("failed to journal reconciliation report")
    return report


def _classify_account_severity(issues: list[dict[str, Any]]) -> ReconcileSeverity:
    if not issues:
        return "info"
    severity: ReconcileSeverity = "warning"
    for issue in issues:
        kind = issue.get("kind")
        if kind in ("external_position_detected", "external_closed", "position_size_drift"):
            severity = _max_severity(severity, "action_required")
        elif kind == "snapshot_unhealthy" and issue.get("health") == "auth_error":
            severity = _max_severity(severity, "trading_halted")
        elif kind == "snapshot_stale":
            severity = _max_severity(severity, "action_required")
        elif kind == "active_order_missing_at_exchange":
            severity = _max_severity(severity, "action_required")
    return severity


def _fetch_exchange_positions(config: Config, profile) -> list[dict[str, Any]]:
    """Pull open positions from the exchange.

    For spot CEX this is derived from ``get_balances``: a non-zero
    base-asset balance for a market we hold is treated as an external
    position. For derivatives venues that ccxt exposes a richer
    ``fetch_positions`` API, callers can override by adding a hook on
    the connector layer (out of scope for the first slice).
    """
    if not profile.reads_real_balances:
        return []
    try:
        from ..connectors import ConnectorRegistry
        registry = ConnectorRegistry(workspace=config.paths.root)
        legacy_account = profile.to_account()
        conn = registry.get(profile.id, legacy_account.connector_cfg())
        if hasattr(conn, "fetch_positions"):
            try:
                raw = conn.fetch_positions()  # type: ignore[attr-defined]
                out: list[dict[str, Any]] = []
                for r in raw or []:
                    out.append({
                        "market": str(r.get("symbol") or r.get("market") or ""),
                        "size_base": float(r.get("contracts") or r.get("size") or 0.0),
                    })
                return out
            except Exception:
                pass
        # Spot fallback — surface non-zero base assets so the
        # operator can verify nothing unexpected is sitting there.
        balances = conn.get_balances()
    except Exception as exc:  # pragma: no cover
        log.warning("reconcile_account: cannot fetch exchange positions for %s: %s", profile.id, exc)
        return []
    out = []
    for bal in balances or []:
        asset = (getattr(bal, "asset", "") or "").upper()
        total = float(getattr(bal, "total", 0) or 0)
        if asset in ("USDT", "USDC", "USD", "BUSD", "FDUSD", "TUSD", "DAI"):
            continue
        if total <= 0:
            continue
        market = f"{profile.venue.upper()}:{asset}{profile.base_currency.upper()}"
        out.append({"market": market, "size_base": total})
    return out


def _fetch_exchange_open_orders(config: Config, profile) -> list[dict[str, Any]]:
    if not profile.reads_real_balances:
        return []
    try:
        from ..connectors import ConnectorRegistry
        registry = ConnectorRegistry(workspace=config.paths.root)
        legacy_account = profile.to_account()
        conn = registry.get(profile.id, legacy_account.connector_cfg())
        if hasattr(conn, "fetch_open_orders"):
            raw = conn.fetch_open_orders()  # type: ignore[attr-defined]
            return [
                {
                    "order_id": str(r.get("id") or r.get("order_id") or ""),
                    "client_order_id": str(r.get("clientOrderId") or r.get("client_order_id") or ""),
                    "market": str(r.get("symbol") or r.get("market") or ""),
                }
                for r in (raw or [])
            ]
    except Exception as exc:  # pragma: no cover
        log.warning("reconcile_account: cannot fetch open orders for %s: %s", profile.id, exc)
    return []


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------


def reconcile(
    config: Config,
    *,
    account_id: str | None = None,
    persist: bool = True,
) -> ReconciliationReport:
    """Run all reconciliation passes for ``account_id`` (or all accounts).

    Returns a single combined report with the highest severity from
    any pass; individual per-pass reports are also persisted so the
    dashboard can drill in.
    """
    issues: list[dict[str, Any]] = []
    summary: dict[str, Any] = {"passes": []}

    local = reconcile_local(config.paths, account_id=account_id, persist=persist)
    summary["passes"].append({"scope": local.scope, "severity": local.severity, "issues": len(local.issues)})
    issues.extend(local.issues)

    account_severity: ReconcileSeverity = "info"
    if account_id:
        account = reconcile_account(config, account_id, persist=persist)
        summary["passes"].append({"scope": account.scope, "severity": account.severity, "issues": len(account.issues)})
        issues.extend(account.issues)
        account_severity = account.severity
    else:
        from .accounts import load_account_profiles
        for profile in load_account_profiles(config.paths).values():
            account = reconcile_account(config, profile.id, persist=persist)
            summary["passes"].append({
                "scope": account.scope,
                "severity": account.severity,
                "issues": len(account.issues),
                "account_id": profile.id,
            })
            issues.extend(account.issues)
            account_severity = _max_severity(account_severity, account.severity)

    severity = _max_severity(local.severity, account_severity)
    report = ReconciliationReport(
        report_id=_new_reconcile_id(),
        ts=time.time(),
        scope="global",
        severity=severity,
        account_id=account_id,
        summary=summary,
        issues=issues,
    )
    if persist:
        ReconciliationStore(config.paths).record(report)
    return report


__all__ = [
    "reconcile_strategy",
    "ReconcileReport",
    "ReconciliationReport",
    "ReconciliationStore",
    "ReconcileSeverity",
    "ReconcileScope",
    "reconcile_local",
    "reconcile_account",
    "reconcile",
]
