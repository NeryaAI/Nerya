"""Operator-facing BFF routes.
.

This module ships three endpoints the dashboard's new top-level shell
consumes directly:

* ``GET /operator/nav`` — capability- and scope-aware top-level nav
  with hidden-reason explanations.
* ``GET /operator/overview`` — Home page aggregate. Rolls up
  workspace/portfolio/approvals/messages/runtime/teams into a single
  envelope so the dashboard stops fan-out fetching.
* ``GET /setup/readiness`` — first-run checklist. Inspects discovery,
  LLM tiers, wallet/exchange providers, risk policy, and the strategy
  package list to surface "what's missing" with a fix-it action.

Everything is read-only. The operator envelope shape lives in
:mod:`nerya.api._envelope` so every operator-facing endpoint speaks
the same language; the dashboard renders ``status``, ``severity``,
``primary_action``, ``next_actions`` and ``source_refs`` without
having to reverse-engineer ad-hoc JSON.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..core import jsonl
from ._envelope import (
    action,
    blocked,
    debug_ref,
    merge_data,
    ok,
    source_ref,
    warn,
)


# ---------------------------------------------------------------------------
# Helpers — shared across nav/overview/readiness
# ---------------------------------------------------------------------------


def _safe(call, default):
    try:
        return call()
    except Exception:  # pragma: no cover — defensive
        return default


def _pending_approvals(client) -> list[dict[str, Any]]:
    p = getattr(client.config.paths, "approvals_pending", None)
    if not p or not p.exists():
        return []
    return [
        rec
        for rec in jsonl.read_all(p)
        if not rec.get("state") or rec["state"] == "pending"
    ]


def _open_turns(client) -> list[dict[str, Any]]:
    try:
        from ..agent.recovery import list_open_turns

        return [s.asdict() for s in list_open_turns(client.config.paths)]
    except Exception:  # pragma: no cover
        return []


def _proposals(client) -> list[dict[str, Any]]:
    try:
        from ..evolution.patch_proposal import list_proposals

        return [p.asdict() for p in list_proposals(client.config.paths)]
    except Exception:
        return []


def _portfolio_summary(client) -> dict[str, Any]:
    try:
        from ..portfolio import portfolio as portfolio_mod

        return portfolio_mod.get_portfolio_summary(client.config.paths) or {}
    except Exception:
        return {}


def _equity_curve(client, limit: int = 120) -> list[dict[str, Any]]:
    try:
        from ..portfolio import portfolio as portfolio_mod

        # Reuse the same inputs the existing /portfolio/equity_curve uses,
        # but keep the helper local so we don't re-import the route handler.
        from ..core import jsonl
        from ..trading.strategies import list_strategies

        paths = client.config.paths
        all_rows: list[tuple[str, float]] = []
        for s in list_strategies(paths):
            fp = paths.strategy_history(s.id) / "pnl.jsonl"
            if not fp.exists():
                continue
            for r in jsonl.read_all(fp):
                p = r.get("pnl") or {}
                ts = r.get("ts") or ""
                realized = float(p.get("realized_usd") or p.get("realized") or 0.0)
                all_rows.append((str(ts), realized))
        all_rows.sort(key=lambda x: x[0])
        base = float(
            (portfolio_mod.get_portfolio_summary(paths) or {})
            .get("totals", {})
            .get("equity_usd", 0.0)
        )
        equity = base
        points: list[dict[str, Any]] = []
        running = base - sum(v for _, v in all_rows)
        for ts, realized in all_rows[-limit:]:
            running += realized
            points.append({"ts": ts, "equity_usd": round(running, 4)})
        if not points:
            points.append({"ts": "", "equity_usd": round(equity, 4)})
        return points
    except Exception:
        return []


def _llm_tier_summary(client) -> dict[str, Any]:
    cfg = client.config
    # IMPORTANT: credentials live one level up from the tier in the YAML —
    # they are stored on ``llm.providers.<name>.provider_key_ref`` rather
    # than directly on the tier. The previous implementation read the
    # tier dict in isolation and reported `has_key=False` for every
    # operator who used the standard "configure provider once, point
    # multiple tiers at it" flow, which made the wizard wedge on
    # "No LLM tier has both a provider and credentials" even when the
    # `/llm/config` merged view showed all four tiers as ready.
    # `effective_tiers` performs the same inheritance the dashboard
    # already relies on, so we reuse it instead of re-implementing the
    # merge here.
    try:
        from ..llm.ops import effective_tiers
        tiers_map = effective_tiers(cfg)
    except Exception:
        tiers_map = cfg.get("llm.tiers") or {}
    summary: list[dict[str, Any]] = []
    for name, raw in tiers_map.items():
        cfg_dict = raw or {}
        provider = (cfg_dict.get("provider") or "").lower()
        has_key = bool(
            cfg_dict.get("provider_key_ref") or cfg_dict.get("provider_key_env")
        )
        summary.append(
            {
                "tier": name,
                "provider": provider,
                "model": cfg_dict.get("model") or "",
                "has_key": has_key,
                "ready": bool(provider and (has_key or provider == "mock")),
            }
        )
    summary.sort(key=lambda r: r["tier"])
    return {"tiers": summary, "default": cfg.get("llm.default_tier", "medium")}


def _strategy_package_count(client) -> int:
    """Return a cheap package count for high-frequency dashboard BFF calls.

    ``load_packages`` validates and hashes every file in every strategy
    package. That is appropriate for strategy detail/validation surfaces, but
    the operator nav, overview, and setup-readiness endpoints only need a
    presence/count signal and are fetched on every dashboard page.
    """

    try:
        root = client.config.paths.strategies
        if not root.exists():
            return 0
        return sum(
            1
            for path in root.iterdir()
            if path.is_dir() and (path / "strategy.yml").exists()
        )
    except Exception:
        return 0


def _trading_strategy_count(client) -> int:
    try:
        from ..trading.strategies import list_strategies

        return len(list(list_strategies(client.config.paths)))
    except Exception:
        return 0


def _wallet_providers(client) -> list[dict[str, Any]]:
    try:
        from ..wallet.providers import describe_providers

        return list(describe_providers(client.config))
    except Exception:
        try:
            return list(getattr(client, "wallet", None).list_providers())  # type: ignore[union-attr]
        except Exception:
            return []


def _exchange_providers(client) -> list[dict[str, Any]]:
    try:
        from ..exchanges.providers import describe_providers

        return list(describe_providers(client.config))
    except Exception:
        return []


def _accounts(client) -> list[dict[str, Any]]:
    """Return the list of configured trading accounts.

    Previously this called ``client.discovery.accounts()``, but
    ``InternalClient`` does not expose a ``.discovery`` attribute, so
    the call always raised ``AttributeError``, was silently swallowed,
    and the readiness probe reported "no trading account" even when
    ``accounts.yml`` had a perfectly good `paper_main` row. We delegate
    to :mod:`nerya.trading.accounts` directly — the same source the
    ``/discovery/accounts`` BFF route uses.
    """
    try:
        from ..trading import accounts as accounts_mod
        loaded = accounts_mod.load_accounts(client.config.paths)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for acc in loaded.values():
        out.append({
            "id": acc.id,
            "exchange": acc.exchange,
            "venue": acc.venue or acc.exchange,
            "kind": acc.kind,
            "mode": acc.mode,
            "status": acc.status,
            "live_trading_enabled": bool(acc.live_trading_enabled),
        })
    return out


# ---------------------------------------------------------------------------
# /operator/nav
# ---------------------------------------------------------------------------


# Consolidated operator navigation. Detailed surfaces stay reachable as
# section tabs in the dashboard, while the sidebar stays scenario-level.
# Order is significant — the dashboard renders entries in this order.
_PRIMARY_NAV: tuple[dict[str, Any], ...] = (
    {
        "id": "home",
        "label": "Home",
        "href": "/dashboard",
        "icon": "home",
        "tagline": "Money, risk, and what needs your attention.",
        "always_visible": True,
    },
    {
        "id": "agent_workspace",
        "label": "Agent Workspace",
        "href": "/chat",
        "icon": "chat",
        "tagline": "Plan, run, and review tasks with the agent.",
        "always_visible": True,
    },
    {
        "id": "trading",
        "label": "Trading",
        "href": "/portfolio",
        "match_hrefs": ["/accounts", "/orders", "/incidents"],
        "icon": "portfolio",
        "tagline": "Portfolio, accounts, orders, incidents.",
        "always_visible": True,
    },
    {
        "id": "strategy_lab",
        "label": "Strategy Lab",
        "href": "/strategies",
        "match_hrefs": ["/workflows"],
        "icon": "strategy",
        "tagline": "Strategies and scheduled automations.",
        "always_visible": True,
    },
    {
        "id": "runtime_library",
        "label": "Runtime Library",
        "href": "/agents",
        "match_hrefs": ["/skills", "/tasks"],
        "icon": "agents",
        "tagline": "Personas, skills, and agent task traces.",
        "always_visible": True,
    },
    {
        "id": "inbox",
        "label": "Action Inbox",
        "href": "/inbox",
        "icon": "inbox",
        "tagline": "Approvals, failed tasks, risk alerts.",
        "always_visible": True,
    },
    {
        "id": "settings",
        "label": "Settings",
        "href": "/settings",
        "icon": "settings",
        "tagline": "Integrations, risk, notifications, secrets.",
        "always_visible": True,
    },
)


_ADVANCED_NAV: tuple[dict[str, Any], ...] = (
    {
        "id": "self_evolution",
        "label": "Self Evolution",
        "href": "/self-evolution",
        "icon": "evolution",
        "tagline": "Signals, assets, proposals, validation, outcomes.",
        "always_visible": True,
    },
    {
        "id": "memory",
        "label": "Memory",
        "href": "/memory",
        "icon": "memory",
        "tagline": "Notebook entries, activity log, write rules, evidence vault, operator profile.",
        "always_visible": True,
    },
    {
        "id": "web_search",
        "label": "Web search",
        "href": "/web-search",
        "icon": "search",
        "tagline": "Multi-engine search chain + per-engine API keys with rotation.",
        "always_visible": True,
    },
    {
        "id": "browsers",
        "label": "Browsers",
        "href": "/browsers",
        "icon": "browsers",
        "tagline": "Engines + interactive session in one workspace. Install engines, then drive a live browser with click/scroll/type and watch its console & network.",
        "always_visible": True,
    },
    {
        "id": "env_vault",
        "label": "Env & Vault",
        "href": "/env-vault",
        "icon": "vault",
        "tagline": "Encrypted runtime env injection plus reusable vault:// references.",
        "always_visible": True,
    },
    {
        "id": "gateway",
        "label": "Gateway",
        "href": "/gateway",
        "icon": "messages",
        "tagline": "Configure each chat platform with its own inline setup docs, then watch live traffic.",
        "always_visible": True,
    },
)


def _capability_flags(client) -> dict[str, bool]:
    """Quick flags the nav builder consults when deciding visibility."""

    cfg = client.config
    return {
        "portfolio": _trading_strategy_count(client) > 0
        or len(_accounts(client)) > 0,
        "strategies": _strategy_package_count(client)
        + _trading_strategy_count(client)
        > 0,
        "workflows": True,
        "messaging": bool(cfg.get("messaging.platforms")),
        "evolution": bool(cfg.get("evolution.enabled", False)),
        "live_trading": cfg.live_trading_enabled(),
    }


def _workspace_nav_entries(client) -> tuple[list[dict[str, Any]], list[str]]:
    """Return safe custom page entries from the declarative UI manifest.

    The shell still owns all compiled routes.  A manifest page is merely a
    JSON/YAML-driven renderer route under ``/workspace/pages/<id>``; arbitrary
    hrefs and duplicate built-in ids are never accepted here.
    """

    try:
        from ..workspace.ui import nav_pages, read

        return nav_pages(read(client.config.paths))
    except Exception as exc:  # pragma: no cover - defensive nav fallback
        return [], [f"workspace UI unavailable: {type(exc).__name__}"]


def _nav_handler(client, _query):
    flags = _capability_flags(client)
    primary: list[dict[str, Any]] = []
    hidden: list[dict[str, Any]] = []
    for entry in _PRIMARY_NAV:
        cap = entry.get("requires_capability")
        if not cap or entry.get("always_visible") or flags.get(cap):
            primary.append(dict(entry))
        else:
            hidden.append(
                {
                    **entry,
                    # ``reason``/``fix_action`` are the current frontend
                    # contract. Keep the old aliases for older dashboard
                    # builds still deployed against this backend.
                    "reason": f"Capability '{cap}' not configured.",
                    "fix_action": action(
                        id=f"setup_{cap}",
                        label="Run setup",
                        href="/settings",
                    ),
                    "hidden_reason": f"Capability '{cap}' not configured.",
                    "fix": action(
                        id=f"setup_{cap}",
                        label="Run setup",
                        href="/settings",
                    ),
                }
            )
    advanced = [dict(e) for e in _ADVANCED_NAV]
    custom_pages, ui_warnings = _workspace_nav_entries(client)
    reserved = {str(entry.get("id") or "") for entry in (*primary, *advanced)}
    for entry in custom_pages:
        entry_id = str(entry.get("id") or "")
        if entry_id in reserved:
            ui_warnings.append(f"workspace page id collides with built-in nav: {entry_id}")
            continue
        section = entry.pop("_section", "advanced")
        if section == "primary":
            primary.append(entry)
        else:
            advanced.append(entry)
        reserved.add(entry_id)
    env = ok(
        f"{len(primary)} primary entries, {len(advanced)} advanced",
        data={
            "primary": primary,
            "advanced": advanced,
            "hidden": hidden,
            "capabilities": flags,
            "workspace_ui_warnings": ui_warnings,
        },
        debug_refs=[debug_ref("module", "routes_operator.nav")],
    )
    return env


# ---------------------------------------------------------------------------
# /operator/overview
# ---------------------------------------------------------------------------


def _build_attention_items(
    *, approvals: list[dict[str, Any]], turns: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compose a unified `What needs attention` list for Home.

    The dashboard renders this as a high-density card so the operator
    can triage without leaving Home. Severity drives ordering and
    colour.
    """

    items: list[dict[str, Any]] = []
    for rec in approvals[:25]:
        aid = rec.get("approval_id") or rec.get("id") or ""
        items.append(
            {
                "id": f"approval:{aid}",
                "type": "approval",
                "severity": "warn",
                "summary": str(rec.get("summary") or rec.get("title") or aid),
                "source_refs": [source_ref("approval", str(aid))],
                "actions": [
                    action(
                        id="open_inbox",
                        label="Review",
                        href=f"/inbox?type=approval&id={aid}",
                    )
                ],
            }
        )
    for state in turns[:25]:
        tid = state.get("turn_id") or state.get("id") or ""
        items.append(
            {
                "id": f"turn:{tid}",
                "type": "failed_task",
                "severity": "danger" if state.get("error") else "warn",
                "summary": (
                    state.get("error_message")
                    or state.get("last_step")
                    or f"Open turn {tid}"
                ),
                "source_refs": [source_ref("turn", str(tid))],
                "actions": [
                    action(
                        id="resume_turn",
                        label="Resume",
                        href=f"/agents?turn_id={tid}",
                    )
                ],
            }
        )
    for prop in proposals[:25]:
        if prop.get("state") not in ("draft", "pending_review", "proposed"):
            continue
        pid = prop.get("id") or ""
        items.append(
            {
                "id": f"proposal:{pid}",
                "type": "proposal",
                "severity": "info",
                "summary": str(prop.get("summary") or pid),
                "source_refs": [source_ref("proposal", str(pid))],
                "actions": [
                    action(
                        id="open_proposal",
                        label="Review",
                        href=f"/inbox?type=proposal&id={pid}",
                    )
                ],
            }
        )
    return items


def _overview_handler(client, _query):
    cfg = client.config
    portfolio = _portfolio_summary(client)
    equity_points = _equity_curve(client, limit=120)
    approvals = _pending_approvals(client)
    open_turns = _open_turns(client)
    proposals = _proposals(client)
    llm = _llm_tier_summary(client)
    accounts = _accounts(client)
    package_count = _strategy_package_count(client)
    trading_count = _trading_strategy_count(client)

    attention = _build_attention_items(
        approvals=approvals, turns=open_turns, proposals=proposals,
    )

    health = {
        "live_trading": cfg.live_trading_enabled(),
        "paper_trading": cfg.paper_trading_enabled(),
        "kill_switch": cfg.kill_switch(),
        "llm_ready": any(t.get("ready") for t in llm["tiers"]),
        "accounts": len(accounts),
        "strategies": package_count + trading_count,
        "open_turns": len(open_turns),
        "pending_approvals": len(approvals),
    }

    if cfg.kill_switch():
        env = blocked(
            "Workspace kill switch is engaged.",
            primary_action=action(
                id="open_settings_security",
                label="Open security settings",
                href="/settings?section=security",
            ),
        )
    elif not health["llm_ready"]:
        env = blocked(
            "No LLM tier is ready. Configure a provider before running tasks.",
            primary_action=action(
                id="open_llm_settings",
                label="Configure LLM",
                href="/settings?section=integrations",
            ),
        )
    elif approvals:
        env = warn(
            f"{len(approvals)} approval(s) waiting for you.",
            primary_action=action(
                id="open_inbox",
                label="Open Action Inbox",
                href="/inbox",
            ),
        )
    else:
        env = ok(
            "Workspace is healthy.",
            primary_action=action(
                id="open_workspace",
                label="Open Agent Workspace",
                href="/chat",
            ),
        )

    merge_data(
        env,
        health=health,
        portfolio=portfolio,
        equity_curve=equity_points,
        attention=attention,
        llm=llm,
        counts={
            "approvals": len(approvals),
            "open_turns": len(open_turns),
            "proposals": sum(
                1
                for p in proposals
                if p.get("state") in ("draft", "pending_review", "proposed")
            ),
            "strategy_packages": package_count,
            "legacy_strategies": trading_count,
        },
    )
    env["debug_refs"] = [
        debug_ref("module", "routes_operator.overview"),
        debug_ref("source", "approvals", href="/approvals/pending"),
        debug_ref("source", "open_turns", href="/agent/open_turns"),
        debug_ref("source", "proposals", href="/evolution/proposals"),
    ]
    return env


# ---------------------------------------------------------------------------
# /setup/readiness
# ---------------------------------------------------------------------------


def _readiness_check(
    *, name: str, status: str, summary: str, fix: dict[str, Any] | None = None,
    sources: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "summary": summary,
        "fix": fix,
        "source_refs": list(sources),
    }


def _readiness_handler(client, _query):
    checks: list[dict[str, Any]] = []
    blocking: list[str] = []

    llm = _llm_tier_summary(client)
    llm_ready = any(t.get("ready") for t in llm["tiers"])
    checks.append(
        _readiness_check(
            name="LLM provider",
            status="ok" if llm_ready else "blocked",
            summary=(
                "At least one LLM tier is ready."
                if llm_ready
                else "No LLM tier has both a provider and credentials."
            ),
            fix=None
            if llm_ready
            else action(
                id="configure_llm",
                label="Configure LLM",
                href="/settings?section=integrations",
            ),
            sources=[source_ref("settings", "llm")],
        )
    )
    if not llm_ready:
        blocking.append("llm")

    accounts = _accounts(client)
    has_account = bool(accounts)
    checks.append(
        _readiness_check(
            name="Trading account",
            status="ok" if has_account else "warn",
            summary=(
                f"{len(accounts)} account(s) discovered."
                if has_account
                else "No trading account found. Paper account recommended for first run."
            ),
            fix=None
            if has_account
            else action(
                id="create_account",
                label="Create paper account",
                href="/settings?section=integrations",
            ),
            sources=[source_ref("settings", "accounts")],
        )
    )
    if not has_account:
        blocking.append("account")

    strategies = _strategy_package_count(client) + _trading_strategy_count(client)
    has_strategy = strategies > 0
    checks.append(
        _readiness_check(
            name="Strategy",
            status="ok" if has_strategy else "warn",
            summary=(
                f"{strategies} strategy(ies) available."
                if has_strategy
                else "No strategies yet. Create one or import a sample."
            ),
            fix=None
            if has_strategy
            else action(
                id="create_strategy",
                label="Create strategy",
                href="/strategies",
            ),
            sources=[source_ref("nav", "strategies")],
        )
    )
    if not has_strategy:
        blocking.append("strategy")

    cfg = client.config
    risk_configured = bool(
        cfg.get("risk.max_drawdown_usd")
        or cfg.get("risk.max_open_positions")
        or cfg.get("approval.policy")
    )
    checks.append(
        _readiness_check(
            name="Risk policy",
            status="ok" if risk_configured else "warn",
            summary=(
                "Risk and approval policies are configured."
                if risk_configured
                else "Default risk/approval policy in use. Review before going live."
            ),
            fix=None
            if risk_configured
            else action(
                id="configure_risk",
                label="Configure risk",
                href="/settings?section=risk",
            ),
            sources=[source_ref("settings", "risk")],
        )
    )

    wallets = _wallet_providers(client)
    wallet_ok = any(p.get("ready") for p in wallets)
    checks.append(
        _readiness_check(
            name="Wallet / Exchange",
            status="ok" if wallet_ok else "warn",
            summary=(
                "At least one wallet/exchange provider is ready."
                if wallet_ok
                else "No wallet or exchange provider is ready. Live trading is gated."
            ),
            fix=None
            if wallet_ok
            else action(
                id="connect_provider",
                label="Connect provider",
                href="/settings?section=integrations",
            ),
            sources=[source_ref("settings", "integrations")],
        )
    )

    if blocking:
        env = blocked(
            f"Blocked on: {', '.join(blocking)}",
            primary_action=action(
                id="resolve_setup",
                label="Resolve setup",
                href="/settings",
            ),
        )
    else:
        env = ok(
            "Workspace is ready.",
            primary_action=action(
                id="open_workspace",
                label="Open Agent Workspace",
                href="/chat",
            ),
        )

    merge_data(env, checks=checks, blocking=blocking)
    env["debug_refs"] = [
        debug_ref("module", "routes_operator.readiness"),
        debug_ref("source", "capability", href="/runtime/capability_matrix"),
    ]
    return env


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def routes():
    return [
        ("GET", "/operator/nav", _nav_handler),
        ("GET", "/operator/overview", _overview_handler),
        ("GET", "/setup/readiness", _readiness_handler),
    ]


__all__ = ["routes"]
