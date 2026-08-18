"""HTTP surface for inline approval prompts.

Today the dashboard had to construct approval renderings ad-hoc and
the gateway had no way to ask Nerya "give me the inline keyboard
payload for this approval". This route module exposes:

- ``GET /approvals/pending`` — list pending approvals plus their
  Telegram-style prompt (text + inline_keyboard).
- ``GET /approvals/prompt`` — fetch the prompt for a single approval id.
- ``POST /approvals/callback`` — dispatch a platform callback (button
  press) into the approval gate. Honors actor ownership when the
  approval record carries an owner; native prompts fail closed when
  requester scope is missing.

The endpoints use the same locked, expiry-aware JSONL transition as
the ACP and gateway surfaces. We do not refactor the gate itself in
this pass.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any

from ..core import jsonl
from ..core.time import now_iso
from ..messaging.approval_prompts import (
    ApprovalPrompt,
    build_prompt,
    parse_callback_data,
    resolve_callback,
)


@contextmanager
def _approval_file_lock(path):
    """Serialize approval queue moves across gateway/API workers."""

    lock_path = path.with_name(f".{path.name}.lock")
    # ponytail: one queue-wide lock is enough until approval throughput proves
    # it needs per-record coordination.
    with jsonl._open_append(lock_path):  # noqa: SLF001
        yield


def _is_native_tool_approval(rec: dict[str, Any]) -> bool:
    return str(rec.get("kind") or "").strip() in {
        "tool_permission",
        "tool_permission_batch",
    }


_FINANCIAL_APPROVAL_KINDS = frozenset({"trade_intent", "wallet_swap"})


def _required_approval_scope(rec: dict[str, Any]) -> str:
    """Return the least-privilege HTTP scope for this approval record."""

    if str(rec.get("kind") or "").strip() in _FINANCIAL_APPROVAL_KINDS:
        return "approve:trade"
    return "approve:tool"


def _approval_owner_actor_id(rec: dict[str, Any]) -> str:
    """Return the actor bound to an approval, or empty when unbound."""

    for key in ("approval_actor_id", "actor_id"):
        actor_id = str(rec.get(key) or "").strip()
        if actor_id:
            return actor_id
    if _is_native_tool_approval(rec):
        return str(rec.get("requester_actor_id") or "").strip()
    return ""


def _can_resolve(
    rec: dict[str, Any],
    actor_id: str,
    *,
    operator_authorized: bool = False,
) -> bool:
    """Keep native approvals bound to an owner or trusted operator scope."""

    actor_id = str(actor_id or "").strip()
    if not actor_id:
        return False
    owner = _approval_owner_actor_id(rec)
    if _is_native_tool_approval(rec):
        # Native permission prompts must never be ownerless. HTTP operators
        # with approve:tool/api:all are the explicit exception.
        return bool(owner) and (operator_authorized or actor_id == owner)
    return not owner or operator_authorized or actor_id == owner


def _trusted_operator(
    payload: dict[str, Any],
    actor_id: str,
    rec: dict[str, Any],
) -> bool:
    """Validate dispatcher auth and bind its scope to the record kind."""

    stamped_actor = str(payload.get("_auth_actor_id") or "").strip()
    if not stamped_actor or stamped_actor != str(actor_id or "").strip():
        return False
    raw_scopes = payload.get("_auth_scopes")
    if isinstance(raw_scopes, str):
        scopes = {
            part.strip()
            for part in raw_scopes.replace(",", " ").split()
            if part.strip()
        }
    elif isinstance(raw_scopes, (list, tuple, set, frozenset)):
        scopes = {str(part).strip() for part in raw_scopes if str(part).strip()}
    else:
        scopes = set()
    return "api:all" in scopes or _required_approval_scope(rec) in scopes


def _expired(rec: dict[str, Any], *, now: float | None = None) -> bool:
    """Return true only for an explicitly invalid/expired expiry value."""

    if "expires_at" not in rec or rec.get("expires_at") in (None, ""):
        return False  # legacy approval rows had no expiry field
    try:
        return float(rec["expires_at"]) <= float(now if now is not None else time.time())
    except (TypeError, ValueError):
        return True


def _read_pending(client) -> list[dict[str, Any]]:
    p = client.config.paths.approvals_pending
    if not p.exists():
        return []
    now = time.time()
    return [
        rec
        for rec in jsonl.read_all(p)
        if (not rec.get("state") or rec["state"] == "pending")
        and not _expired(rec, now=now)
    ]


def _find_record(client, approval_id: str) -> dict[str, Any] | None:
    rows = _read_pending(client)
    for rec in rows:
        if rec.get("approval_id") == approval_id or rec.get("id") == approval_id:
            return rec
    return None


def _row_to_prompt(rec: dict[str, Any]) -> ApprovalPrompt:
    actor_id = _approval_owner_actor_id(rec)
    return build_prompt(rec, actor_id=actor_id)


def _telegram_envelope(prompt: ApprovalPrompt) -> dict[str, Any]:
    return {
        "text": prompt.text,
        "reply_markup": prompt.telegram_reply_markup(),
    }


def _pending(client, _payload):
    rows = _read_pending(client)
    out: list[dict[str, Any]] = []
    for rec in rows:
        prompt = _row_to_prompt(rec)
        out.append({
            "record": rec,
            "prompt": prompt.as_dict(),
            "telegram": _telegram_envelope(prompt),
        })
    return {"ok": True, "count": len(out), "approvals": out}


def _prompt(client, payload):
    aid = str(payload.get("approval_id") or payload.get("id") or "").strip()
    if not aid:
        return {"ok": False, "error": "approval_id required"}
    rec = _find_record(client, aid)
    if rec is None:
        return {"ok": False, "error": "not_found", "approval_id": aid}
    prompt = _row_to_prompt(rec)
    return {
        "ok": True,
        "record": rec,
        "prompt": prompt.as_dict(),
        "telegram": _telegram_envelope(prompt),
    }


def _move_record(
    client,
    approval_id: str,
    *,
    state: str,
    note: str,
    resolver_actor_id: str = "",
    operator_authorized: bool = False,
) -> dict[str, Any] | None:
    """Move a pending approval to the approved/rejected JSONL.

    This is the canonical transition shared by HTTP, ACP, and gateway
    approval surfaces.
    """
    paths = client.config.paths
    src = paths.approvals_pending
    if not src.exists():
        return None
    with _approval_file_lock(src):
        rows = jsonl.read_all(src)
        kept: list[dict[str, Any]] = []
        moved: dict[str, Any] | None = None
        for rec in rows:
            if moved is None and (
                rec.get("approval_id") == approval_id or rec.get("id") == approval_id
            ):
                if _expired(rec):
                    return None
                if not _can_resolve(
                    rec,
                    resolver_actor_id,
                    operator_authorized=operator_authorized,
                ):
                    return None
                rec["state"] = state
                rec["state_ts"] = now_iso()
                if note:
                    rec["state_note"] = note
                if resolver_actor_id:
                    rec["resolved_by_actor_id"] = str(resolver_actor_id)
                moved = rec
                continue
            kept.append(rec)
        if moved is None:
            return None
        jsonl.write_all(src, kept)
        dst = (
            paths.approvals_approved if state == "approved" else paths.approvals_rejected
        )
        # Coordinate with the kernel's one-shot consumer, which rewrites the
        # terminal queue after marking a verdict consumed.
        with _approval_file_lock(dst):
            jsonl.append(dst, moved)
    try:
        from ..db.repositories import ApprovalRepository
        from ..db.sqlite import connect

        con = connect(client.config.paths.db)
        ApprovalRepository(con).set_state(approval_id, state)
        con.close()
    except Exception:
        pass
    return moved


def _retract_approval_cards(client, approval_id: str, *, state: str) -> None:
    """Best-effort removal of already-delivered approval buttons.

    Dashboard cards disappear because /approvals/pending no longer
    returns the approval. For outbox-backed gateway deliveries we also
    mark the recorded card resolved and, for Telegram messages whose
    upstream message id is known, clear the inline keyboard.
    """

    import json as _json

    outbox = client.config.paths.outbox_messages
    if not outbox.is_dir():
        return
    try:
        from ..core import yaml_io
        channels_doc = yaml_io.load(client.config.paths.messages_channels, default={}) or {}
        channels = channels_doc.get("channels") or {}
    except Exception:
        channels = {}

    def _resolve_secret(ref: str) -> str | None:
        if not ref or not ref.startswith("vault://"):
            return None
        try:
            from ..security.secrets import SecretVault

            vault = SecretVault.open(client.config.paths.vault_enc)
            return vault.resolve(ref[len("vault://"):], required_scope="messaging")
        except Exception:
            return None

    for path in outbox.glob("approval-*.json"):
        try:
            doc = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(doc.get("approval_id") or "") != approval_id:
            continue
        channel = str(doc.get("channel") or "")
        kind = str(doc.get("kind") or "").lower()
        cfg = dict((channels.get(channel) or {}) if isinstance(channels, dict) else {})
        if not kind:
            kind = str(cfg.get("kind") or "").lower()
        if kind == "telegram":
            chat_id = cfg.get("chat_id")
            message_id = doc.get("telegram_message_id")
            if chat_id and message_id is not None:
                try:
                    from ..messaging import telegram as _tg

                    _tg.clear_reply_markup(
                        channel_cfg=cfg,
                        chat_id=chat_id,
                        message_id=message_id,
                        resolve_secret=_resolve_secret,
                    )
                except Exception:
                    pass
        doc["state"] = state
        doc["resolved_state"] = state
        doc["buttons"] = []
        doc["reply_markup"] = {"inline_keyboard": []}
        try:
            path.write_text(
                _json.dumps(doc, indent=2, default=str, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass


def _publish_approval_resolution(
    approval_id: str,
    *,
    state: str,
    record: dict[str, Any] | None = None,
    config: Any = None,
) -> dict[str, Any] | None:
    resume_result: dict[str, Any] | None = None
    try:
        from ..agent.streaming import get_default_bus

        rec = record or {}
        get_default_bus().publish(
            "approval.resolved",
            approval_id=approval_id,
            state=state,
            resolver_actor_id=rec.get("resolved_by_actor_id"),
            approval_kind=rec.get("kind"),
            session_id=rec.get("session_id"),
            strategy_id=rec.get("strategy_id"),
            record=rec,
        )
    except Exception:
        pass
    # The API server and AgentKernel commonly run as separate processes. In
    # that deployment the kernel's event-bus subscriber is not present in the
    # API process, so an approved trade would otherwise only move JSONL rows
    # and never reach the executor. Resume directly when no subscriber is
    # registered; the durable claim keeps this safe if both paths race.
    kind = str((record or {}).get("kind") or "").strip()
    if str(state or "").lower() == "approved" and config is not None:
        try:
            if kind == "trade_intent":
                from ..trading import approval_resume

                if not getattr(
                    approval_resume, "_resume_subscriber_registered", False
                ):
                    resume_result = approval_resume.resume_approved(
                        config, approval_id
                    )
            elif kind == "wallet_swap":
                from ..wallet import swap_approval

                resume_result = swap_approval.resume_approved(config, approval_id)
        except Exception as exc:  # pragma: no cover - callback must still resolve
            resume_result = {
                "ok": False,
                "approval_id": approval_id,
                "error": f"approval_resume_dispatch_failed:{exc}",
            }
    return resume_result


def _callback(client, payload):
    callback_data = str(payload.get("callback_data") or "").strip()
    trusted_actor_id = str(payload.get("_auth_actor_id") or "").strip()
    actor_id = trusted_actor_id or str(payload.get("actor_id") or "").strip()
    if not callback_data:
        return {"ok": False, "error": "callback_data required"}
    if not actor_id:
        return {"ok": False, "error": "trusted actor required"}

    action, aid = parse_callback_data(callback_data)
    if not action or not aid:
        return {"ok": False, "error": "ignored", "reason": "callback_data not recognized"}

    rec = _find_record(client, aid)
    if rec is None:
        return {"ok": False, "error": "approval not found", "approval_id": aid}

    operator_authorized = _trusted_operator(payload, actor_id, rec)
    # HTTP callbacks always carry a dispatcher stamp. Even when the token
    # actor happens to own the approval, a trade-only token must not resolve
    # tool permissions (or vice versa). Gateway/ACP callbacks have no stamp
    # and continue to rely on exact owner binding below.
    if trusted_actor_id and not operator_authorized:
        return {
            "ok": False,
            "error": "insufficient approval scope",
            "approval_id": aid,
            "required_scope": _required_approval_scope(rec),
        }

    def actor_owns(req_actor: str, approval_id: str) -> bool:
        return _can_resolve(
            rec or {},
            req_actor,
            operator_authorized=operator_authorized,
        )

    if action == "details":
        if not actor_owns(actor_id, aid):
            return {
                "ok": False,
                "error": "approval owner mismatch",
                "approval_id": aid,
            }
        prompt = _row_to_prompt(rec or {"approval_id": aid})
        return {
            "ok": True,
            "action": "details",
            "approval_id": aid,
            "record": rec,
            "prompt": prompt.as_dict(),
            "telegram": _telegram_envelope(prompt),
        }

    moved_state = {"state": None, "record": None}

    def _approve(target_id: str) -> None:
        moved = _move_record(client, target_id, state="approved",
                             note=f"approved via callback by {actor_id or 'unknown'}",
                             resolver_actor_id=actor_id,
                             operator_authorized=operator_authorized)
        moved_state["state"] = "approved" if moved else None
        moved_state["record"] = moved

    def _reject(target_id: str, reason: str) -> None:
        moved = _move_record(
            client, target_id, state="rejected", note=reason,
            resolver_actor_id=actor_id,
            operator_authorized=operator_authorized,
        )
        moved_state["state"] = "rejected" if moved else None
        moved_state["record"] = moved

    resolution = resolve_callback(
        callback_data,
        actor_id=actor_id,
        approve=_approve,
        reject=_reject,
        actor_owns=actor_owns,
    )
    if resolution.state in {"approved", "rejected"} and moved_state["state"] != resolution.state:
        return {
            "ok": False,
            "error": "approval already resolved or expired",
            "approval_id": aid,
            "action": action,
            "state": resolution.state,
        }
    if resolution.state == "error" and moved_state["state"] is None:
        return {
            "ok": False,
            "error": resolution.note or "approval callback failed",
            "approval_id": aid,
            "action": action,
        }
    if resolution.state in {"approved", "rejected"}:
        try:
            jsonl.append(client.config.paths.approvals_pending.parent / "callbacks.jsonl", {
                "approval_id": aid,
                "action": action,
                "actor_id": actor_id,
                "state": resolution.state,
                "ts": now_iso(),
            })
        except Exception:  # pragma: no cover - audit best effort
            pass
        try:
            _retract_approval_cards(client, aid, state=resolution.state)
        except Exception:
            pass
        resume_result = _publish_approval_resolution(
            aid,
            state=str(resolution.state),
            record=moved_state["record"] or rec,
            config=client.config,
        )
    else:
        resume_result = None
    items = []
    if isinstance((rec or {}).get("items"), list):
        items = [x for x in (rec or {}).get("items") if isinstance(x, dict)]
    return {"ok": resolution.state in {"approved", "rejected", "details"},
            "approval_id": aid,
            "approval_ids": [aid],
            "action": action,
            "state": resolution.state,
            "approval_kind": str((rec or {}).get("kind") or ""),
            "batch": str((rec or {}).get("kind") or "") == "tool_permission_batch",
            "item_count": len(items),
            "note": resolution.note,
            **({"resume": resume_result} if resume_result is not None else {}),
    }


def routes():
    return [
        ("GET", "/approvals/pending", _pending),
        ("POST", "/approvals/pending", _pending),
        ("GET", "/approvals/prompt", _prompt),
        ("POST", "/approvals/prompt", _prompt),
        ("POST", "/approvals/callback", _callback),
    ]
