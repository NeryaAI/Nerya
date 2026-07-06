"""Operator remediation hints for Risk Gate reasons."""

from __future__ import annotations

from typing import Any


# Maps a reason *prefix* to a remediation suggestion. The dashboard
# picks the first matching prefix per reason, so ordering matters:
# most specific first.
FIX_HINT_CATALOGUE: list[tuple[str, dict[str, Any]]] = [
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
    # The exempt variant must be checked before the broader health prefix.
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
            "(draft -> static_review -> backtested -> paper -> shadow -> canary -> live), "
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
    intent: Any | None = None,
) -> list[dict[str, Any]]:
    """Translate risk reasons into operator-facing remediation hints."""

    if not reasons:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason == "ok":
            continue
        for prefix, template in FIX_HINT_CATALOGUE:
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
                            strategy_id=getattr(intent, "strategy_id"),
                            account_id=getattr(intent, "account_id"),
                        )
                    except Exception:
                        hint["href"] = href_template
                elif href_template:
                    hint["href"] = href_template
                out.append(hint)
                break
    return out
