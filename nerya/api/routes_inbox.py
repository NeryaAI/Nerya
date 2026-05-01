"""Action Inbox routes.

Plan ref: ``docs/frontend-agent-workspace-redesign-plan.md`` Phase 26.

The inbox unifies five sources the operator currently has to triage
across three tabs:

* approvals (``approvals/pending``)
* evolution proposals (``evolution/proposals``)
* failed / open agent turns (``agent/open_turns``)
* outbound messages with ``severity`` ≥ warn (``messages/list``)
* provider readiness errors (LLM tiers without credentials, wallets
  not ready, etc.)

Returned items share one schema::

    {
      "id":        "approval:abc123",
      "type":      "approval" | "proposal" | "failed_task" |
                   "notification" | "provider_error",
      "severity":  "info" | "warn" | "danger",
      "status":    "pending" | "in_progress" | "resolved" | "expired",
      "title":     "...",
      "summary":   "...",
      "requires_action": True,
      "source_refs":     [...],
      "actions":         [...],
      "created_at":      "...",
      "data":            {...},
    }

The legacy ``/approvals/*``, ``/evolution/*``, ``/agent/open_turns``,
``/messages/*`` endpoints stay live for the Advanced/Debug surfaces.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from ._envelope import action, debug_ref, ok, source_ref


# ---------------------------------------------------------------------------
# Data sources (shared with /operator/overview)
# ---------------------------------------------------------------------------


def _read_approvals(client) -> list[dict[str, Any]]:
    paths = client.config.paths
    p = getattr(paths, "approvals_pending", None)
    if not p or not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("state") and rec["state"] != "pending":
            continue
        rows.append(rec)
    return rows


def _read_proposals(client) -> list[dict[str, Any]]:
    try:
        from ..evolution.patch_proposal import list_proposals

        return [p.asdict() for p in list_proposals(client.config.paths)]
    except Exception:
        return []


def _read_open_turns(client) -> list[dict[str, Any]]:
    try:
        from ..agent.recovery import list_open_turns

        return [s.asdict() for s in list_open_turns(client.config.paths)]
    except Exception:
        return []


def _read_messages(client, limit: int = 100) -> list[dict[str, Any]]:
    try:
        out = client.messages.list(limit=limit) or {}
        return list(out.get("messages") or [])
    except Exception:
        return []


def _read_llm_tiers(client) -> list[dict[str, Any]]:
    cfg = client.config
    rows: list[dict[str, Any]] = []
    for name, raw in (cfg.get("llm.tiers") or {}).items():
        cfg_dict = raw or {}
        provider = (cfg_dict.get("provider") or "").lower()
        has_key = bool(
            cfg_dict.get("provider_key_ref") or cfg_dict.get("provider_key_env")
        )
        rows.append(
            {
                "tier": name,
                "provider": provider,
                "has_key": has_key,
                "ready": bool(provider and (has_key or provider == "mock")),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Item builders
# ---------------------------------------------------------------------------


def _approval_item(rec: dict[str, Any]) -> dict[str, Any]:
    aid = rec.get("approval_id") or rec.get("id") or ""
    severity = "warn"
    if (rec.get("severity") or "").lower() in ("danger", "critical"):
        severity = "danger"
    return {
        "id": f"approval:{aid}",
        "raw_id": str(aid),
        "type": "approval",
        "severity": severity,
        "status": rec.get("state") or "pending",
        "title": str(
            rec.get("title")
            or rec.get("summary")
            or f"Approval {aid}"
        ),
        "summary": str(rec.get("summary") or ""),
        "requires_action": True,
        "created_at": rec.get("created_at") or rec.get("ts") or "",
        "source_refs": [source_ref("approval", str(aid))],
        "actions": [
            action(
                id="approve",
                label="Approve",
                method="POST",
                href="/approvals/callback",
                body={
                    "callback_data": f"approve:{aid}",
                    "actor_id": "operator",
                },
                requires_scope="approve",
            ),
            action(
                id="reject",
                label="Reject",
                method="POST",
                href="/approvals/callback",
                body={
                    "callback_data": f"reject:{aid}",
                    "actor_id": "operator",
                },
                requires_scope="approve",
                severity="danger",
            ),
            action(
                id="open",
                label="Open detail",
                href=f"/inbox?type=approval&id={aid}",
            ),
        ],
        "data": rec,
    }


def _proposal_item(prop: dict[str, Any]) -> dict[str, Any]:
    pid = prop.get("id") or ""
    state = prop.get("state") or "draft"
    severity = "info"
    if state in ("rejected", "rolled_back"):
        severity = "warn"
    elif state in ("applied", "approved"):
        severity = "info"
    elif state in ("draft", "pending_review", "proposed"):
        severity = "warn"
    return {
        "id": f"proposal:{pid}",
        "raw_id": str(pid),
        "type": "proposal",
        "severity": severity,
        "status": state,
        "title": str(prop.get("title") or prop.get("summary") or pid),
        "summary": str(prop.get("summary") or ""),
        "requires_action": state in ("draft", "pending_review", "proposed"),
        "created_at": prop.get("ts") or prop.get("created_at") or "",
        "source_refs": [source_ref("proposal", str(pid))],
        "actions": [
            action(
                id="apply",
                label="Apply",
                method="POST",
                href="/evolution/apply",
                body={"proposal_id": str(pid)},
                severity="warn",
                requires_scope="approve",
            ),
            action(
                id="rollback",
                label="Rollback",
                method="POST",
                href="/evolution/rollback",
                body={"proposal_id": str(pid)},
                severity="danger",
                requires_scope="approve",
            ),
            action(
                id="open",
                label="Open detail",
                href=f"/inbox?type=proposal&id={pid}",
            ),
        ],
        "data": prop,
    }


def _failed_task_item(state: dict[str, Any]) -> dict[str, Any]:
    tid = state.get("turn_id") or state.get("id") or ""
    has_error = bool(state.get("error") or state.get("error_message"))
    severity = "danger" if has_error else "warn"
    summary = (
        state.get("error_message")
        or state.get("last_step")
        or "Open turn — agent paused"
    )
    return {
        "id": f"failed_task:{tid}",
        "raw_id": str(tid),
        "type": "failed_task",
        "severity": severity,
        "status": "in_progress" if not has_error else "pending",
        "title": str(summary),
        "summary": str(state.get("strategy_id") or "agent"),
        "requires_action": True,
        "created_at": state.get("opened_at") or state.get("ts") or "",
        "source_refs": [source_ref("turn", str(tid))],
        "actions": [
            action(
                id="resume",
                label="Resume",
                method="POST",
                href="/agent/turn_state",
                body={"turn_id": str(tid)},
            ),
            action(
                id="explain",
                label="Explain",
                method="POST",
                href="/agent/explain",
                body={"turn_id": str(tid)},
            ),
        ],
        "data": state,
    }


def _notification_item(msg: dict[str, Any]) -> dict[str, Any]:
    severity = (msg.get("severity") or msg.get("priority") or "info").lower()
    if severity not in ("info", "warn", "danger"):
        severity = "info"
    needs_action = severity != "info"
    mid = (
        msg.get("message_id")
        or msg.get("id")
        or f"msg-{msg.get('ts') or 'n/a'}"
    )
    return {
        "id": f"notification:{mid}",
        "raw_id": str(mid),
        "type": "notification",
        "severity": severity,
        "status": msg.get("state") or "pending",
        "title": str(msg.get("kind") or msg.get("channel") or "notification"),
        "summary": str(msg.get("text") or ""),
        "requires_action": needs_action,
        "created_at": str(msg.get("ts") or ""),
        "source_refs": [source_ref("message", str(mid))],
        "actions": [
            action(
                id="open",
                label="Open",
                href=f"/inbox?type=notification&id={mid}",
            ),
        ],
        "data": msg,
    }


def _provider_error_item(tier: dict[str, Any]) -> dict[str, Any]:
    name = tier.get("tier") or "unknown"
    return {
        "id": f"provider_error:llm:{name}",
        "raw_id": f"llm:{name}",
        "type": "provider_error",
        "severity": "danger",
        "status": "pending",
        "title": f"LLM tier '{name}' is not ready",
        "summary": f"Provider '{tier.get('provider') or 'unset'}' "
        f"missing credentials.",
        "requires_action": True,
        "created_at": "",
        "source_refs": [source_ref("llm_tier", str(name))],
        "actions": [
            action(
                id="fix_provider",
                label="Configure provider",
                href=f"/settings?section=integrations&tier={name}",
            ),
        ],
        "data": tier,
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


_TYPE_FILTER = {"approval", "proposal", "failed_task", "notification", "provider_error"}
_SEV_RANK = {"danger": 0, "warn": 1, "info": 2}


def _collect_items(
    client,
    *,
    types: Optional[Iterable[str]] = None,
    severities: Optional[Iterable[str]] = None,
    requires_action: Optional[bool] = None,
    status: Optional[Iterable[str]] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    type_set = set(types) if types else _TYPE_FILTER
    sev_set = set(severities) if severities else None
    status_set = set(status) if status else None

    items: list[dict[str, Any]] = []
    if "approval" in type_set:
        items.extend(_approval_item(r) for r in _read_approvals(client))
    if "proposal" in type_set:
        items.extend(_proposal_item(p) for p in _read_proposals(client))
    if "failed_task" in type_set:
        items.extend(_failed_task_item(s) for s in _read_open_turns(client))
    if "notification" in type_set:
        for m in _read_messages(client):
            sev = (m.get("severity") or m.get("priority") or "info").lower()
            if sev == "info":
                continue
            items.extend([_notification_item(m)])
    if "provider_error" in type_set:
        for tier in _read_llm_tiers(client):
            if not tier.get("ready"):
                items.append(_provider_error_item(tier))

    if sev_set is not None:
        items = [i for i in items if i["severity"] in sev_set]
    if requires_action is not None:
        items = [i for i in items if bool(i["requires_action"]) == requires_action]
    if status_set is not None:
        items = [i for i in items if i.get("status") in status_set]

    items.sort(
        key=lambda i: (
            _SEV_RANK.get(i["severity"], 9),
            0 if i["requires_action"] else 1,
            -(_to_epoch(i.get("created_at"))),
        )
    )
    return items[:limit]


def _to_epoch(value: Any) -> float:
    if not value:
        return 0.0
    try:
        from datetime import datetime

        if isinstance(value, (int, float)):
            return float(value)
        return datetime.fromisoformat(
            str(value).rstrip("Z").replace("Z", "")
        ).timestamp()
    except Exception:
        return 0.0


def _split_csv(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _items_handler(client, query):
    q = dict(query or {})
    types = _split_csv(q.get("type"))
    severities = _split_csv(q.get("severity"))
    statuses = _split_csv(q.get("status"))
    requires_action_raw = q.get("requires_action")
    requires_action: Optional[bool] = None
    if requires_action_raw is not None:
        requires_action = str(requires_action_raw).lower() in ("1", "true", "yes")
    limit = max(1, int(q.get("limit") or 200))

    items = _collect_items(
        client,
        types=types or None,
        severities=severities or None,
        requires_action=requires_action,
        status=statuses or None,
        limit=limit,
    )

    needs = [i for i in items if i["requires_action"]]
    summary = (
        f"{len(needs)} action(s) needed across {len(items)} inbox items"
        if needs
        else f"{len(items)} inbox items, none currently require action"
    )

    env = ok(
        summary,
        data={"items": items, "count": len(items), "needs_action": len(needs)},
        primary_action=action(
            id="resolve_top",
            label="Open top item",
            href=f"/inbox?id={items[0]['id']}" if items else "/inbox",
        )
        if items
        else None,
        debug_refs=[
            debug_ref("source", "approvals", href="/approvals/pending"),
            debug_ref("source", "proposals", href="/evolution/proposals"),
            debug_ref("source", "open_turns", href="/agent/open_turns"),
            debug_ref("source", "messages", href="/messages/list"),
        ],
    )
    return env


def _resolve_handler(client, payload):
    """Resolve an inbox item.

    The actual mutation lives in the underlying subsystem (approvals,
    evolution, agent recovery). This handler dispatches to the right
    one based on the ``id`` prefix and returns a unified envelope.
    """

    pid = str((payload or {}).get("id") or "").strip()
    decision = str((payload or {}).get("decision") or "").strip().lower()
    if not pid:
        return {"ok": False, "error": "id required"}
    kind, _, raw = pid.partition(":")
    if not raw:
        return {"ok": False, "error": "id must be 'type:raw_id'"}

    try:
        if kind == "approval":
            # Apr-29 2026 — earlier this branch tried to call a 4-arg
            # ``resolve_callback(paths, approval_id=, decision=, actor_id=)``
            # which never existed. Reuse the same plumbing the
            # ``/approvals/callback`` HTTP surface and Telegram callback
            # path use so the dashboard, gateways, and the operator
            # inbox stay in lockstep.
            from . import routes_approvals as _ra

            decision = decision or "approve"
            normalised = "approve" if decision in {"approve", "applied"} else (
                "reject" if decision in {"reject", "rejected"} else decision
            )
            actor_id = str((payload or {}).get("actor_id") or "operator")
            if normalised in {"approve", "reject"}:
                callback_data = f"{normalised}:{raw}"
                outcome = _ra._callback(
                    client,
                    {"callback_data": callback_data, "actor_id": actor_id},
                )
            else:
                # "dismiss" / unsupported -> just record the audit trail
                # and leave the approval pending.
                outcome = {
                    "ok": True, "approval_id": raw,
                    "action": normalised,
                    "state": "ignored",
                    "note": f"decision {decision!r} not actionable on approval",
                }
            return ok(
                f"approval {normalised}",
                data={"id": pid, "outcome": outcome},
            )

        if kind == "proposal":
            from ..evolution.promotion import apply_proposal
            from ..evolution.rollback import rollback_proposal

            if decision == "rollback":
                result = rollback_proposal(client.config.paths, raw)
            else:
                result = apply_proposal(client.config.paths, raw)
            return ok(
                f"proposal {decision or 'applied'}",
                data={"id": pid, "outcome": result},
            )

        if kind == "failed_task":
            from ..agent.recovery import load_turn_state

            state = load_turn_state(client.config.paths, raw).asdict()
            return ok(
                "turn state loaded; resume via /agent/run_turn",
                data={"id": pid, "turn_state": state},
            )

        if kind == "notification":
            return ok("notification dismissed", data={"id": pid})

        if kind == "provider_error":
            return ok(
                "navigate to settings to fix provider",
                data={"id": pid},
                primary_action=action(
                    id="open_settings",
                    label="Open settings",
                    href="/settings?section=integrations",
                ),
            )

        return {"ok": False, "error": f"unknown inbox kind {kind!r}"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def routes():
    return [
        ("GET", "/inbox/items", _items_handler),
        ("POST", "/inbox/resolve", _resolve_handler),
    ]


__all__ = ["routes"]
