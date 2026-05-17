"""Risk Gate — pure function of (intent, strategy, account, market snapshot, ledger).

Every decision now carries a stable
``risk_evaluation_id`` (matching the new ``risk_evaluations`` table)
so reservations, executors, and journal entries can pin themselves to
exactly which decision allowed them.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..core.config import Config
from ..core.ids import risk_evaluation_id as _new_risk_evaluation_id
from ..core.time import now_iso
from ..db import DedupeRepository
from ..db.sqlite import connect
from .account_snapshots import fresh_snapshot
from .accounts import Account, load_accounts
from .capital import CapitalReservationStore
from .intents import TradeIntent
from .position_book import PositionBook
from .reconciliation import ReconciliationStore
from .strategies import Strategy, load_strategy
from .virtual_ledger import open_ledger


@dataclass
class RiskDecision:
    intent_id: str
    decision: Literal["allow", "reject", "escalate"]
    reasons: list[str]
    limits_snapshot: dict[str, Any] = field(default_factory=dict)
    virtual_ledger_snapshot: dict[str, Any] = field(default_factory=dict)
    estimated_notional_usd: float = 0.0
    risk_evaluation_id: str = ""
    account_snapshot: dict[str, Any] = field(default_factory=dict)
    reservation_blocked_usd: float = 0.0
    ts: str = ""
    # promotion-state-aware flags consumed by submit.py.
    # ``shadow_only=True`` means the gate accepted the intent but the
    # submit pipeline must skip the executor (intent is journalled
    # against a real-money account snapshot, never sent to the venue).
    shadow_only: bool = False
    promotion_state: str = ""
    # operator-facing remediation hints. Each entry maps
    # one of the strings in ``reasons`` to a human-readable fix and a
    # deep-link target the dashboard can render as a button.
    fix_hints: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        if not self.ts:
            self.ts = now_iso()
        if not self.risk_evaluation_id:
            self.risk_evaluation_id = _new_risk_evaluation_id()
        # Always recompute hints from reasons so callers that mutate
        # ``reasons`` post-construction still see a consistent surface.
        if self.reasons and not self.fix_hints:
            self.fix_hints = derive_fix_hints(self.reasons)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


# fix-hint catalogue. Maps a reason *prefix* to a
# remediation suggestion. The dashboard picks the first matching
# prefix per reason so ordering matters: most specific first.
_FIX_HINT_CATALOGUE: list[tuple[str, dict[str, Any]]] = [
    (
        "kill_switch_enabled",
        {
            "title": "Global kill switch is on",
            "detail": "Disable the global kill switch from the Incident Center "
            "before any new orders can flow.",
            "action": "open_incidents",
            "href": "/incidents",
        },
    ),
    (
        "live_trading_disabled_runtime",
        {
            "title": "Runtime live trading flag is off",
            "detail": "Set runtime.live_trading_enabled=true in nerya.yml "
            "(or via the runtime panel) before live orders can be placed.",
            "action": "open_runtime",
            "href": "/portfolio",
        },
    ),
    (
        "live_trading_disabled_account",
        {
            "title": "Account is not enabled for live trading",
            "detail": "Open the account driver page and toggle "
            "live_trading_enabled, then try again.",
            "action": "open_account",
            "href_template": "/accounts/{account_id}",
        },
    ),
    (
        "account_status_quarantined",
        {
            "title": "Account is quarantined",
            "detail": "Investigate the quarantine reason on the account page, "
            "then re-activate from /accounts.",
            "action": "open_account",
            "href_template": "/accounts/{account_id}",
        },
    ),
    (
        "account_status_disabled",
        {
            "title": "Account is disabled",
            "detail": "Re-activate the account from its driver page, or rebind "
            "the strategy to another account.",
            "action": "open_account",
            "href_template": "/accounts/{account_id}",
        },
    ),
    (
        "account_status_read_only",
        {
            "title": "Account is read-only",
            "detail": "Read-only mode blocks new orders. Promote the account "
            "back to active or rebind the strategy.",
            "action": "open_account",
            "href_template": "/accounts/{account_id}",
        },
    ),
    (
        "account_unknown",
        {
            "title": "Account binding is invalid",
            "detail": "The strategy is bound to an account that no longer "
            "exists. Rebind it from the strategy page.",
            "action": "rebind_account",
            "href_template": "/strategies/{strategy_id}",
        },
    ),
    (
        "account_binding_mismatch",
        {
            "title": "Intent targets the wrong account",
            "detail": "This intent's account_id does not match the "
            "strategy's bound account. Rebind the strategy or "
            "re-route the intent.",
            "action": "rebind_account",
            "href_template": "/strategies/{strategy_id}",
        },
    ),
    (
        "account_snapshot_stale_exempt",
        {
            "title": "Snapshot stale (allowed because intent reduces position)",
            "detail": "The balance loop hasn't refreshed recently, but this "
            "intent shrinks exposure so we let it through. Investigate "
            "the snapshot worker / venue connectivity ASAP.",
            "action": "open_account",
            "href_template": "/accounts/{account_id}",
        },
    ),
    (
        "account_snapshot_stale",
        {
            "title": "Account snapshot is stale",
            "detail": "The balance loop hasn't refreshed recently. Check the "
            "snapshot worker / venue connectivity, then retry.",
            "action": "open_account",
            "href_template": "/accounts/{account_id}",
        },
    ),
    # Order matters: the more specific ``_exempt`` reason must be
    # checked before the broader ``account_snapshot_health_`` prefix,
    # otherwise the broader hint would swallow the exempt variant.
    (
        "account_snapshot_health_exempt",
        {
            "title": "Snapshot unhealthy (allowed because intent reduces position)",
            "detail": "The latest balance fetch failed but this intent "
            "shrinks exposure so we let it through. Investigate from the "
            "account driver page.",
            "action": "open_account",
            "href_template": "/accounts/{account_id}",
        },
    ),
    (
        "account_snapshot_health_",
        {
            "title": "Account snapshot is unhealthy",
            "detail": "The latest balance fetch failed (auth/network/parse). "
            "Investigate from the account driver page.",
            "action": "open_account",
            "href_template": "/accounts/{account_id}",
        },
    ),
    (
        "reservation_overcommit",
        {
            "title": "Reservations exceed free balance",
            "detail": "Existing capital reservations + this order are larger "
            "than the snapshot's free balance. Cancel stale reservations or "
            "wait for fills to drain.",
            "action": "open_account",
            "href_template": "/accounts/{account_id}",
        },
    ),
    (
        "insufficient_paper_cash",
        {
            "title": "Paper sandbox is out of cash",
            "detail": "Reset the paper account balance from the accounts page.",
            "action": "reset_paper",
            "href_template": "/accounts/{account_id}",
        },
    ),
    (
        "reconciliation_halt",
        {
            "title": "Account is on a reconciliation halt",
            "detail": "Run a fresh reconciliation pass and resolve the drift "
            "before re-arming trading.",
            "action": "run_reconciliation",
            "href_template": "/accounts/{account_id}",
        },
    ),
    (
        "reconciliation_action_required",
        {
            "title": "Reconciliation drift requires operator review",
            "detail": "Open the latest reconciliation report and ack the drift "
            "before placing more orders on this account.",
            "action": "open_reports",
            "href": "/incidents",
        },
    ),
    (
        "max_single_order_exceeded",
        {
            "title": "Order notional exceeds strategy cap",
            "detail": "Either resize the intent under the strategy's "
            "max_single_order_usd, or relax the limit in limits.yml.",
            "action": "open_limits",
            "href_template": "/strategies/{strategy_id}",
        },
    ),
    (
        "canary_max_single_order_exceeded",
        {
            "title": "Order exceeds canary safety cap",
            "detail": "Canary mode forces a stricter notional cap. Lower the "
            "intent size or promote the strategy past canary first.",
            "action": "open_limits",
            "href_template": "/strategies/{strategy_id}",
        },
    ),
    (
        "max_total_exposure_exceeded",
        {
            "title": "Total exposure cap reached",
            "detail": "Close existing positions or raise "
            "max_total_exposure_usd in the strategy limits.",
            "action": "open_positions",
            "href_template": "/strategies/{strategy_id}",
        },
    ),
    (
        "confidence_below_floor",
        {
            "title": "Signal confidence below floor",
            "detail": "Either increase the agent's confidence in the trade or "
            "lower min_confidence in the strategy limits.",
            "action": "open_limits",
            "href_template": "/strategies/{strategy_id}",
        },
    ),
    (
        "stale_market_data",
        {
            "title": "Market data is stale",
            "detail": "The strategy received a tick older than max_stale_seconds. "
            "Check feeds / triggers and retry.",
            "action": "open_strategy",
            "href_template": "/strategies/{strategy_id}",
        },
    ),
    (
        "duplicate_intent",
        {
            "title": "Duplicate intent suppressed",
            "detail": "An identical trade intent fired within the dedupe "
            "window. Tweak the trigger or wait the window out.",
            "action": "open_strategy",
            "href_template": "/strategies/{strategy_id}",
        },
    ),
    (
        "approval_required_threshold",
        {
            "title": "Order needs operator approval",
            "detail": "Notional is at or above approval_threshold_usd. Approve "
            "from the inbox to release the order.",
            "action": "open_inbox",
            "href": "/inbox",
        },
    ),
    (
        "canary_per_trade_approval_required",
        {
            "title": "Canary mode requires per-trade approval",
            "detail": "Approve the intent from the inbox to release the order.",
            "action": "open_inbox",
            "href": "/inbox",
        },
    ),
    (
        "canary_requires_protection_rule",
        {
            "title": "Canary intents must carry a protection rule",
            "detail": "Attach a TP/SL/trailing protection to the TradePlan "
            "(or pass meta.protection_present=true for legacy intents).",
            "action": "open_strategy",
            "href_template": "/strategies/{strategy_id}",
        },
    ),
    (
        "market_not_allowed",
        {
            "title": "Market not in strategy allow-list",
            "detail": "Add the market to limits.allowed_markets or rebind the "
            "intent to a permitted market.",
            "action": "open_limits",
            "href_template": "/strategies/{strategy_id}",
        },
    ),
    (
        "strategy_status_",
        {
            "title": "Strategy is not in a tradable state",
            "detail": "Promote the strategy along the lifecycle "
            "(draft → static_review → backtested → paper → shadow → canary → live), "
            "or take it out of paused/archived.",
            "action": "open_promotions",
            "href_template": "/strategies/{strategy_id}",
        },
    ),
    (
        "strategy_unknown",
        {
            "title": "Strategy is missing or invalid",
            "detail": "The strategy package could not be loaded. Open the "
            "strategy page and investigate.",
            "action": "open_strategy",
            "href_template": "/strategies/{strategy_id}",
        },
    ),
]


def derive_fix_hints(
    reasons: list[str],
    *,
    intent: TradeIntent | None = None,
) -> list[dict[str, Any]]:
    """Translate a list of ``reasons`` into operator remediation hints.

    Each hint mirrors the reason it was derived from so the dashboard
    can render them inline. Hints with ``href_template`` are
    interpolated against the intent (when supplied) so deep-links go
    straight to the right account / strategy page.
    """

    if not reasons:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason == "ok":
            continue
        for prefix, template in _FIX_HINT_CATALOGUE:
            if reason.startswith(prefix):
                key = template.get("title", prefix)
                if key in seen:
                    break
                seen.add(key)
                hint = dict(template)
                hint["reason"] = reason
                hint["match"] = prefix
                href_template = hint.pop("href_template", None)
                if href_template and intent is not None:
                    try:
                        hint["href"] = href_template.format(
                            strategy_id=intent.strategy_id,
                            account_id=intent.account_id,
                        )
                    except Exception:
                        # Missing keys / weird intent shape — fall back
                        # to the unfilled template so the dashboard can
                        # render a non-clickable label.
                        hint["href"] = href_template
                elif href_template:
                    hint["href"] = href_template
                out.append(hint)
                break
    return out


class RiskGate:
    def __init__(self, config: Config):
        self.config = config
        self._con = None

    def _con_lazy(self):
        if self._con is None:
            self._con = connect(self.config.paths.db)
        return self._con

    def evaluate(
        self,
        intent: TradeIntent,
        *,
        market_snapshot: dict[str, Any] | None = None,
    ) -> RiskDecision:
        paths = self.config.paths
        reasons: list[str] = []
        decision: str = "allow"

        # 1. Kill switch
        if self.config.kill_switch():
            reasons.append("kill_switch_enabled")
            decision = "reject"

        # load strategy + account
        try:
            strategy: Strategy = load_strategy(paths, intent.strategy_id)
        except Exception as exc:
            reasons.append(f"strategy_unknown:{exc}")
            return RiskDecision(
                intent.intent_id,
                "reject",
                reasons,
                estimated_notional_usd=intent.notional_usd_estimate,
                fix_hints=derive_fix_hints(reasons, intent=intent),
            )
        accounts = load_accounts(paths)
        if intent.account_id not in accounts:
            reasons.append("account_unknown")
            return RiskDecision(
                intent.intent_id,
                "reject",
                reasons,
                estimated_notional_usd=intent.notional_usd_estimate,
                fix_hints=derive_fix_hints(reasons, intent=intent),
            )
        account: Account = accounts[intent.account_id]

        # 1b. Strategy ↔ account binding integrity.
        #
        # Risk evaluates against `intent.account_id` (snapshots, PositionBook,
        # ledger, reservations). If a caller routes an intent at a *different*
        # account than the one the strategy is bound to in `strategy.yml`,
        # every downstream check silently moves to the wrong book and the
        # strategy can effectively trade on an account it was never approved
        # for. Reject early and short-circuit so we never touch the wrong
        # snapshot/ledger.
        bound_account_id = (strategy.account_id or "").strip()
        if bound_account_id and bound_account_id != intent.account_id:
            reasons.append(
                f"account_binding_mismatch:strategy={bound_account_id}!=intent={intent.account_id}"
            )
            return RiskDecision(
                intent.intent_id,
                "reject",
                reasons,
                limits_snapshot=asdict(strategy.limits),
                estimated_notional_usd=intent.notional_usd_estimate,
                fix_hints=derive_fix_hints(reasons, intent=intent),
            )

        # 2. Live flag
        if account.mode == "live" and not self.config.live_trading_enabled():
            reasons.append("live_trading_disabled_runtime")
            decision = "reject"
        if account.mode == "live" and not account.live_trading_enabled:
            reasons.append("live_trading_disabled_account")
            decision = "reject"

        plan_action = str((intent.meta or {}).get("plan_action") or "").strip()
        risk_reducing = plan_action in {"close_position", "reduce_position"}

        # 3. Strategy status. Operators still need to flatten exposure
        # after a pause/quarantine/archive, so risk-reducing close/reduce
        # plans are allowed to continue through the safety checks.
        if not strategy.is_tradable and not (
            risk_reducing and strategy.status in {"paused", "quarantined", "archived"}
        ):
            reasons.append(f"strategy_status_{strategy.status}")
            decision = "reject"

        # 3b. promotion-state guards.
        #     - ``shadow``  must run against a real-money account but
        #       must NOT place orders. We mark the decision so the
        #       submit pipeline can short-circuit without losing the
        #       audit trail.
        #     - ``canary`` must have a protection rule attached (the
        #       caller threads this through via ``intent.meta``) and
        #       the per-trade approval threshold is forced down so an
        #       operator clicks every fill.
        #     - ``live``  cannot be reached from anything other than
        #       ``canary`` or ``paused`` per the lifecycle graph; that
        #       is enforced at promotion time, not here.
        promotion_state = strategy.status
        if strategy.status == "shadow":
            # Shadow positions do not touch the venue; mark the
            # decision and continue evaluating so we still check
            # confidence, dedupe, snapshots, etc.
            pass

        # 4. Account status
        if account.status != "active":
            reasons.append(f"account_status_{account.status}")
            decision = "reject"

        # 5. Market allow-list
        if strategy.limits.allowed_markets and intent.market not in strategy.limits.allowed_markets:
            reasons.append(f"market_not_allowed:{intent.market}")
            decision = "reject"

        # notional
        mark = (market_snapshot or {}).get("price") or intent.limit_price or 0.0
        if intent.size_unit == "usd":
            notional = float(intent.size)
        elif intent.size_unit == "base":
            notional = float(intent.size) * float(mark or 0)
        else:
            notional = float(intent.size)

        # 6. Per-single-order cap
        cap = strategy.limits.max_single_order_usd
        if not risk_reducing and cap > 0 and notional > cap:
            reasons.append(f"max_single_order_exceeded:{notional:.2f}>{cap:.2f}")
            decision = "reject"

        # 6b. canary forces a stricter cap regardless of
        # what the strategy's own ``limits.yml`` says, so a too-loose
        # YAML cannot bypass the canary safety net.
        if not risk_reducing and strategy.status == "canary":
            canary_cap = float(self.config.get("trading.canary.max_single_order_usd", 250.0))
            if canary_cap > 0 and notional > canary_cap:
                reasons.append(
                    f"canary_max_single_order_exceeded:{notional:.2f}>{canary_cap:.2f}"
                )
                decision = "reject"

        # 7. Total exposure cap.
        #
        # PositionBook is the source of truth for both live and paper
        # accounts after v6 — the merged ``size_base * mark`` per market
        # gives the same gross figure regardless of how many strategies
        # contributed. Fall back to the virtual ledger when the book is
        # empty (e.g. a fresh account before any fill has landed) so
        # paper-only tests that pre-populated the ledger keep passing.
        ledger = open_ledger(paths, account.id, account.initial_balance_usd)
        ledger_snapshot = ledger.snapshot()
        book = PositionBook(paths)
        try:
            book_open = book.open_positions(account_id=account.id)
        except Exception:
            book_open = []
        if book_open:
            current_exposure = 0.0
            current_merged_size_by_market: dict[str, float] = {}
            current_merged_avg_by_market: dict[str, float] = {}
            for pos in book_open:
                size = float(pos.size_base or 0.0)
                avg = float(pos.avg_entry_price or 0.0)
                mark = float(pos.mark_price or avg or 0.0)
                current_exposure += abs(size * mark)
                current_merged_size_by_market[pos.market] = size
                current_merged_avg_by_market[pos.market] = avg
        else:
            current_exposure = sum(
                abs(p.get("size", 0) * p.get("avg_price", 0))
                for p in ledger_snapshot["positions"].values()
            )
            current_merged_size_by_market = {
                m: float((p or {}).get("size") or 0.0)
                for m, p in (ledger_snapshot["positions"] or {}).items()
            }
            current_merged_avg_by_market = {
                m: float((p or {}).get("avg_price") or 0.0)
                for m, p in (ledger_snapshot["positions"] or {}).items()
            }
        total_cap = strategy.limits.max_total_exposure_usd
        if not risk_reducing and total_cap > 0 and current_exposure + notional > total_cap:
            reasons.append(
                f"max_total_exposure_exceeded:{current_exposure:.2f}+{notional:.2f}>{total_cap:.2f}"
            )
            decision = "reject"

        # 7b. Per-(account, market) merged-position size cap.
        #
        # ``max_position_size_usd`` protects the *merged* position from
        # runaway aggregation when several strategies hit the same
        # exchange + symbol. We project the new merged size after this
        # fill and compare to the cap in USD. Risk-reducing intents are
        # exempt — a close/reduce can only ever shrink the merged
        # position so it never blows the cap.
        current_size = current_merged_size_by_market.get(intent.market, 0.0)
        current_avg = current_merged_avg_by_market.get(intent.market, 0.0)
        # New size delta in base units. Used by the per-market cap and
        # by the snapshot-freshness exemption below.
        base_size = (
            float(intent.size)
            if intent.size_unit == "base"
            else (float(intent.size) / float(mark or 1.0) if mark else 0.0)
        )
        signed_delta = base_size if intent.side == "buy" else -base_size
        projected_size = current_size + signed_delta
        # An intent is "position-reducing" if it shrinks the absolute
        # merged size — that covers explicit close/reduce plan actions
        # AND ad-hoc operator sells that mathematically de-risk the
        # book (e.g. a sell against an existing long, even without a
        # `plan_action` tag). We use this to relax gates that exist to
        # protect against *adding* exposure (snapshot freshness, etc.).
        position_reducing = bool(
            risk_reducing
            or (abs(current_size) > 0 and abs(projected_size) < abs(current_size))
        )

        per_market_cap = strategy.limits.max_position_size_usd
        if not risk_reducing and per_market_cap > 0 and notional > 0:
            projected_notional = abs(projected_size) * float(mark or current_avg or 0.0)
            if projected_notional > per_market_cap:
                reasons.append(
                    f"max_position_size_exceeded:{intent.market}:"
                    f"{projected_notional:.2f}>{per_market_cap:.2f}"
                )
                decision = "reject"

        # 8. Virtual ledger balance (paper mode only)
        if (
            not risk_reducing
            and account.mode == "paper"
            and intent.side == "buy"
            and ledger_snapshot["cash_usd"] < notional
        ):
            reasons.append("insufficient_paper_cash")
            decision = "reject"

        # 8b. Account snapshot freshness + reservation overlay.
        # Refresh on demand before risk evaluation so scheduled strategies
        # are not rejected just because the background loop cadence is wider
        # than the risk freshness threshold.
        snapshot_payload: dict[str, Any] = {}
        reservation_blocked_usd = 0.0
        max_age_s = float(self.config.get("trading.snapshot.max_age_seconds", 60))
        snap = fresh_snapshot(self.config, intent.account_id, max_age_s=max_age_s)
        if snap is not None:
            snapshot_payload = snap.asdict()
            # Position-reducing intents (close/reduce plan actions OR
            # sells that mathematically shrink the merged position)
            # bypass the freshness/health rejection: the operator needs
            # an exit valve even when the balance loop has stalled or
            # the venue is sneezing. The reason is still appended for
            # audit, with an ``_exempt`` suffix so the dashboard can
            # show the warning without coloring the intent red.
            stale_age = int(time.time() - snap.ts)
            if snap.is_stale(max_age_s=max_age_s):
                if position_reducing:
                    reasons.append(
                        f"account_snapshot_stale_exempt:{stale_age}s>{int(max_age_s)}s"
                    )
                else:
                    reasons.append(
                        f"account_snapshot_stale:{stale_age}s>{int(max_age_s)}s"
                    )
                    decision = "reject"
            if snap.health != "ok":
                if position_reducing:
                    reasons.append(f"account_snapshot_health_exempt:{snap.health}")
                else:
                    reasons.append(f"account_snapshot_health_{snap.health}")
                    decision = "reject"
            try:
                reservation_blocked_usd = CapitalReservationStore(paths).total_blocked_usd(
                    intent.account_id
                )
            except Exception:
                reservation_blocked_usd = 0.0
            # On real-money modes, blocked reservations must not exceed
            # the snapshot's free balance + the new notional.
            if not risk_reducing and account.is_live and intent.side == "buy":
                free_usd = snap.free_usd
                if reservation_blocked_usd + notional > free_usd:
                    reasons.append(
                        f"reservation_overcommit:"
                        f"reserved={reservation_blocked_usd:.2f}+new={notional:.2f}>"
                        f"free={free_usd:.2f}"
                    )
                    decision = "reject"

        # 8c. Reconciliation halt. If the most recent
        # reconciliation pass for this account left a ``trading_halted``
        # severity unresolved (within the lookback window), every new
        # open is rejected until an operator runs a clean pass.
        try:
            recon_lookback = float(self.config.get("trading.reconciliation.halt_window_s", 1800))
            worst = ReconciliationStore(paths).worst_recent(
                account_id=intent.account_id,
                within_seconds=recon_lookback,
            )
        except Exception:
            worst = None
        if not risk_reducing and worst is not None:
            if worst.severity == "trading_halted":
                reasons.append(f"reconciliation_halt:{worst.report_id}")
                decision = "reject"
            elif worst.severity == "action_required" and account.is_live:
                # Soft escalate so an operator approves any open while
                # an unresolved drift exists on a live account.
                if decision != "reject":
                    reasons.append(f"reconciliation_action_required:{worst.report_id}")
                    decision = "escalate"

        # 11. Confidence floor
        if intent.confidence < strategy.limits.min_confidence:
            reasons.append(
                f"confidence_below_floor:{intent.confidence:.2f}<{strategy.limits.min_confidence:.2f}"
            )
            decision = "reject"

        # 13. Stale data guard
        if market_snapshot and "age_s" in market_snapshot:
            if int(market_snapshot["age_s"]) > strategy.limits.max_stale_seconds:
                reasons.append(
                    f"stale_market_data:{market_snapshot['age_s']}s>{strategy.limits.max_stale_seconds}s"
                )
                decision = "reject"

        # 14. Duplicate / dedupe
        if not risk_reducing:
            dedupe_key = f"{intent.strategy_id}:{intent.market}:{intent.side}:{round(notional, 2)}"
            dedupe = DedupeRepository(self._con_lazy())
            window = float(self.config.get("trading.dedupe_window_seconds", 300))
            if dedupe.seen("trade_intent", dedupe_key, window_s=window):
                reasons.append("duplicate_intent")
                decision = "reject"

        # 12 & 15 & 16. Slippage / conflicts / approval threshold (advisory)
        if (
            not risk_reducing
            and strategy.limits.approval_threshold_usd > 0
            and notional >= strategy.limits.approval_threshold_usd
        ):
            if decision != "reject":
                reasons.append(
                    f"approval_required_threshold:{notional:.2f}>={strategy.limits.approval_threshold_usd:.2f}"
                )
                decision = "escalate"

        # 12b. canary state forces approval on every
        # opening side regardless of notional, and rejects opens that
        # don't carry a protection rule. ``intent.meta['protection_present']``
        # is set by ``submit_trade_plan`` when a TradePlan declares one;
        # legacy ``submit_trade_intent`` callers can opt in by passing
        # ``meta={'protection_present': True}``.
        if not risk_reducing and strategy.status == "canary":
            meta = intent.meta or {}
            protection_present = bool(
                meta.get("protection_present")
                or meta.get("plan_protection_attached")
            )
            if intent.side == "buy" and not protection_present:
                reasons.append("canary_requires_protection_rule")
                decision = "reject"
            if decision != "reject":
                reasons.append("canary_per_trade_approval_required")
                decision = "escalate"

        if not reasons:
            reasons = ["ok"]

        # 12c. shadow strategies pass risk but never
        # actually place orders. Tag the decision so the submit
        # pipeline can short-circuit cleanly. Risk is still evaluated
        # against the real-money snapshot above so a broken paper
        # mock cannot mask issues that would surface in shadow.
        shadow_only = (strategy.status == "shadow") and decision != "reject"

        decision_obj = RiskDecision(
            intent_id=intent.intent_id,
            decision=decision,  # type: ignore[arg-type]
            reasons=reasons,
            limits_snapshot=asdict(strategy.limits),
            virtual_ledger_snapshot=ledger_snapshot,
            estimated_notional_usd=notional,
            account_snapshot=snapshot_payload,
            reservation_blocked_usd=reservation_blocked_usd,
            shadow_only=shadow_only,
            promotion_state=promotion_state,
            fix_hints=derive_fix_hints(reasons, intent=intent),
        )
        try:
            self._persist(decision_obj, intent=intent)
        except Exception:
            # Persistence is advisory — never let a bookkeeping
            # hiccup block trading.
            pass
        return decision_obj

    # --------------------------------------------------------------- persistence

    def _persist(self, decision: RiskDecision, *, intent: TradeIntent) -> None:
        con = self._con_lazy()
        # Embed fix_hints under the snapshot blob so the existing
        # ``risk_evaluations`` table doesn't need a schema migration —
        # the dashboard reads them out by key.
        snapshot_blob = dict(decision.account_snapshot)
        if decision.fix_hints:
            snapshot_blob["_fix_hints"] = decision.fix_hints
        con.execute(
            """
            INSERT OR IGNORE INTO risk_evaluations (
                risk_evaluation_id, intent_id, plan_id, strategy_id, account_id,
                decision, notional_usd, reasons_json, snapshot_json, ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.risk_evaluation_id,
                intent.intent_id,
                None,
                intent.strategy_id,
                intent.account_id,
                decision.decision,
                decision.estimated_notional_usd,
                json.dumps(decision.reasons),
                json.dumps(snapshot_blob),
                time.time(),
            ),
        )
