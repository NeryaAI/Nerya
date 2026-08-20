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

from typing import Any

from ..approval_service import ApprovalService
from ..core import jsonl
from ..core.time import now_iso
from ..messaging.approval_prompts import (
    ApprovalPrompt,
    build_prompt,
    parse_callback_data,
    resolve_callback,
)


def _service(client) -> ApprovalService:
    return ApprovalService(client.config)


def _is_native_tool_approval(rec: dict[str, Any]) -> bool:
    return ApprovalService.is_native_tool(rec)


def _required_approval_scope(rec: dict[str, Any]) -> str:
    return ApprovalService.required_scope(rec)


def _approval_owner_actor_id(rec: dict[str, Any]) -> str:
    return ApprovalService.owner_actor_id(rec)


def _can_resolve(
    rec: dict[str, Any],
    actor_id: str,
    *,
    operator_authorized: bool = False,
) -> bool:
    return ApprovalService.can_resolve(
        rec,
        actor_id,
        operator_authorized=operator_authorized,
    )


def _trusted_operator(
    payload: dict[str, Any],
    actor_id: str,
    rec: dict[str, Any],
) -> bool:
    return ApprovalService.trusted_operator(payload, actor_id, rec)


def _expired(rec: dict[str, Any], *, now: float | None = None) -> bool:
    return ApprovalService.expired(rec, now=now)


def _read_pending(client) -> list[dict[str, Any]]:
    return _service(client).pending()


def _find_record(client, approval_id: str) -> dict[str, Any] | None:
    return _service(client).find(approval_id)


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
    return _service(client).move(
        approval_id,
        state=state,
        note=note,
        resolver_actor_id=resolver_actor_id,
        operator_authorized=operator_authorized,
    )


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
    if config is None:
        return None
    return ApprovalService(config).publish_resolution(
        approval_id,
        state=state,
        record=record,
    )


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
