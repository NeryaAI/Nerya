"""HTTP surface for inline approval prompts.

Today the dashboard had to construct approval renderings ad-hoc and
the gateway had no way to ask Nerya "give me the inline keyboard
payload for this approval". This route module exposes:

- ``GET /approvals/pending`` — list pending approvals plus their
  Telegram-style prompt (text + inline_keyboard).
- ``GET /approvals/prompt`` — fetch the prompt for a single approval id.
- ``POST /approvals/callback`` — dispatch a platform callback (button
  press) into the approval gate. Honors actor ownership when the
  approval record carries an ``actor_id``.

The endpoints are read/append only on top of the existing JSONL
journals already maintained by :class:`ApprovalGate`. We do not
refactor the gate itself in this pass.
"""

from __future__ import annotations

from typing import Any

from ..core import jsonl
from ..core.time import now_iso
from ..messaging.approval_prompts import (
    ApprovalPrompt,
    build_prompt,
    parse_callback_data,
    resolve_callback,
)


def _read_pending(client) -> list[dict[str, Any]]:
    paths = client.config.paths
    p = paths.approvals_pending
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            import json as _json
            rec = _json.loads(line)
        except Exception:
            continue
        if rec.get("state") and rec["state"] != "pending":
            continue
        rows.append(rec)
    return rows


def _find_record(client, approval_id: str) -> dict[str, Any] | None:
    rows = _read_pending(client)
    for rec in rows:
        if rec.get("approval_id") == approval_id or rec.get("id") == approval_id:
            return rec
    return None


def _row_to_prompt(rec: dict[str, Any]) -> ApprovalPrompt:
    actor_id = str(rec.get("actor_id") or "")
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


def _move_record(client, approval_id: str, *, state: str, note: str) -> dict[str, Any] | None:
    """Move a pending approval to the approved/rejected JSONL.

    Mirrors :meth:`AcpServer._move_approval` but local to this module so
    the HTTP surface is independent of the ACP server bootstrapping.
    """
    import json as _json

    paths = client.config.paths
    src = paths.approvals_pending
    if not src.exists():
        return None
    kept: list[str] = []
    moved: dict[str, Any] | None = None
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = _json.loads(line)
        except Exception:
            kept.append(line)
            continue
        if moved is None and (
            rec.get("approval_id") == approval_id or rec.get("id") == approval_id
        ):
            rec["state"] = state
            rec["state_ts"] = now_iso()
            if note:
                rec["state_note"] = note
            moved = rec
            continue
        kept.append(line)
    if moved is None:
        return None
    src.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    dst = (
        paths.approvals_approved if state == "approved" else paths.approvals_rejected
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("a", encoding="utf-8") as f:
        f.write(_json.dumps(moved) + "\n")
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
    client,
    approval_id: str,
    *,
    state: str,
    record: dict[str, Any] | None = None,
) -> None:
    try:
        from ..agent.streaming import get_default_bus

        rec = record or {}
        get_default_bus().publish(
            "approval.resolved",
            approval_id=approval_id,
            state=state,
            session_id=rec.get("session_id"),
            strategy_id=rec.get("strategy_id"),
            record=rec,
        )
    except Exception:
        pass


def _callback(client, payload):
    callback_data = str(payload.get("callback_data") or "").strip()
    actor_id = str(payload.get("actor_id") or "").strip()
    if not callback_data:
        return {"ok": False, "error": "callback_data required"}

    action, aid = parse_callback_data(callback_data)
    if not action or not aid:
        return {"ok": False, "error": "ignored", "reason": "callback_data not recognized"}

    rec = _find_record(client, aid)
    if rec is None and action != "details":
        return {"ok": False, "error": "approval not found", "approval_id": aid}

    record_actor = str((rec or {}).get("actor_id") or "")

    def actor_owns(req_actor: str, approval_id: str) -> bool:
        if not record_actor:
            # Single-tenant default: no actor pinned, accept the callback
            # as long as ``actor_id`` was supplied.
            return True
        return req_actor == record_actor

    if action == "details":
        prompt = _row_to_prompt(rec or {"approval_id": aid})
        return {
            "ok": True,
            "action": "details",
            "approval_id": aid,
            "record": rec,
            "prompt": prompt.as_dict(),
            "telegram": _telegram_envelope(prompt),
        }

    moved_state = {"state": None}

    def _approve(target_id: str) -> None:
        moved = _move_record(client, target_id, state="approved",
                             note=f"approved via callback by {actor_id or 'unknown'}")
        moved_state["state"] = "approved" if moved else None

    def _reject(target_id: str, reason: str) -> None:
        moved = _move_record(client, target_id, state="rejected", note=reason)
        moved_state["state"] = "rejected" if moved else None

    resolution = resolve_callback(
        callback_data,
        actor_id=actor_id,
        approve=_approve,
        reject=_reject,
        actor_owns=actor_owns,
    )
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
        _publish_approval_resolution(
            client,
            aid,
            state=str(resolution.state),
            record=rec,
        )
    items = []
    if isinstance((rec or {}).get("items"), list):
        items = [x for x in (rec or {}).get("items") if isinstance(x, dict)]
    return {"ok": resolution.state in {"approved", "rejected", "details"},
            "approval_id": aid,
            "approval_ids": [aid],
            "action": action,
            "state": resolution.state,
            "batch": str((rec or {}).get("kind") or "") == "tool_permission_batch",
            "item_count": len(items),
            "note": resolution.note}


def routes():
    return [
        ("GET", "/approvals/pending", _pending),
        ("POST", "/approvals/pending", _pending),
        ("GET", "/approvals/prompt", _prompt),
        ("POST", "/approvals/prompt", _prompt),
        ("POST", "/approvals/callback", _callback),
    ]
