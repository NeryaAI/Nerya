"""Durable approval coordination for native tool calls.

The permission engine decides *whether* a call needs operator input. This
module owns the remaining durable protocol in one place:

* consume one exact approved/rejected verdict on an approval continuation;
* create or merge one pending batch for unresolved calls in the same turn;
* attach a provider-independent ``approval_request`` block to the result;
* keep requester scope, argument fingerprint, expiry and one-shot semantics
  fail-closed.

Domain approval gates such as trading remain authoritative for their own
invariants. This coordinator only replaces the duplicated kernel plumbing for
native-tool ``ASK`` decisions.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..core import jsonl
from ..core.config import Config
from ..core.time import now_iso
from .permissions import PermissionDecision
from .types import ToolCall, ToolDescriptor


@dataclass(frozen=True)
class ToolApprovalScope:
    session_id: str = ""
    strategy_id: str = ""
    actor_id: str = ""

    @classmethod
    def from_values(
        cls,
        *,
        session_id: str | None,
        strategy_id: str | None,
        actor_id: str | None,
    ) -> "ToolApprovalScope":
        return cls(
            session_id=str(session_id or "").strip(),
            strategy_id=str(strategy_id or "").strip(),
            actor_id=str(actor_id or "").strip(),
        )


@dataclass(frozen=True)
class ToolApprovalResolution:
    verdict: bool | None = None
    request: dict[str, Any] | None = None


class ToolApprovalResolver(Protocol):
    def resolve(
        self,
        call: ToolCall,
        descriptor: ToolDescriptor,
        decision: PermissionDecision,
    ) -> ToolApprovalResolution: ...


@dataclass(frozen=True)
class _Request:
    turn_id: str
    call_id: str
    tool_name: str
    skill_id: str
    arguments: dict[str, Any]
    caller: str
    reason: str
    scope: ToolApprovalScope
    fingerprint: str


@contextmanager
def _locked(path: Path):
    """Serialize approval queue read/modify/write across processes."""

    lock_path = Path(path).with_name(f".{Path(path).name}.lock")
    with jsonl._open_append(lock_path):  # noqa: SLF001
        yield


def tool_permission_fingerprint(tool_name: str, payload: dict[str, Any]) -> str:
    try:
        body = json.dumps(
            {"tool": tool_name, "payload": payload or {}},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    except Exception:
        body = f"{tool_name}:{payload}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _safe_id(value: str, *, limit: int = 80) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "")[:limit]


def _expired(row: dict[str, Any], *, now_ts: float | None = None) -> bool:
    raw = row.get("expires_at")
    try:
        return not raw or float(raw) <= float(now_ts or time.time())
    except (TypeError, ValueError):
        return True


def _items(row: dict[str, Any]) -> list[dict[str, Any]]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    raw = row.get("items") or payload.get("items") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _tool(row: dict[str, Any], parent: dict[str, Any] | None = None) -> dict[str, Any]:
    parent = parent or {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    own = row.get("tool") if isinstance(row.get("tool"), dict) else {}
    payload_tool = payload.get("tool") if isinstance(payload.get("tool"), dict) else {}
    parent_tool = parent.get("tool") if isinstance(parent.get("tool"), dict) else {}
    return {**parent_tool, **payload_tool, **own}


def _scope_value(row: dict[str, Any], parent: dict[str, Any], name: str, legacy: str = "") -> str:
    return str(
        row.get(name)
        or row.get(legacy)
        or parent.get(name)
        or parent.get(legacy)
        or ""
    ).strip()


def _scope_matches(
    row: dict[str, Any],
    parent: dict[str, Any] | None,
    scope: ToolApprovalScope,
) -> bool:
    parent = parent or {}
    stored_session = _scope_value(row, parent, "requester_session_id", "session_id")
    stored_strategy = _scope_value(row, parent, "requester_strategy_id", "strategy_id")
    stored_actor = _scope_value(row, parent, "requester_actor_id")
    return bool(
        stored_session
        and scope.session_id
        and stored_session == scope.session_id
        and stored_strategy == scope.strategy_id
        and stored_actor
        and scope.actor_id
        and stored_actor == scope.actor_id
    )


def _item_matches(
    row: dict[str, Any],
    parent: dict[str, Any] | None,
    request: _Request,
    *,
    now_ts: float,
) -> bool:
    parent = parent or {}
    if parent and _expired(parent, now_ts=now_ts):
        return False
    expiry_row = row if row.get("expires_at") is not None else (parent or row)
    if _expired(expiry_row, now_ts=now_ts):
        return False
    if not _scope_matches(row, parent, request.scope):
        return False
    tool = _tool(row, parent)
    stored_name = str(
        tool.get("name") or row.get("action") or parent.get("action") or ""
    ).strip()
    stored_fp = str(row.get("fingerprint") or tool.get("fingerprint") or "").strip()
    return stored_name == request.tool_name and bool(
        stored_fp and stored_fp == request.fingerprint
    )


def _mark_consumed(
    rows: list[dict[str, Any]],
    *,
    row_index: int,
    item_index: int,
    call_id: str,
) -> None:
    row = rows[row_index]
    items = _items(row)
    target = items[item_index] if str(row.get("kind") or "") == "tool_permission_batch" else row
    consumed_at = now_iso()
    target["consumed_at"] = consumed_at
    target["consumed_call_id"] = call_id
    if target is not row:
        row["items"] = items
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        row["payload"] = {**payload, "items": items}
        if items and all(item.get("consumed_at") for item in items):
            row["consumed_at"] = consumed_at
            row["consumed_call_id"] = call_id


class ToolApprovalStore:
    """Persistence boundary for native-tool approvals."""

    def __init__(self, config: Config):
        self.config = config

    def consume(
        self,
        request: _Request,
        *,
        approval_id: str,
    ) -> bool | None:
        """Consume one exact verdict; missing scope/expiry/fingerprint fails closed."""

        requested_id = str(approval_id or "").strip()
        if not requested_id:
            return None
        now_ts = time.time()
        for path, verdict in (
            (self.config.paths.approvals_rejected, False),
            (self.config.paths.approvals_approved, True),
        ):
            with _locked(path):
                rows = jsonl.read_all(path)
                for row_index in range(len(rows) - 1, -1, -1):
                    row = rows[row_index]
                    row_id = str(row.get("approval_id") or row.get("id") or "").strip()
                    if row_id != requested_id:
                        continue
                    kind = str(row.get("kind") or "")
                    if kind == "tool_permission":
                        if row.get("consumed_at") or not _item_matches(
                            row, None, request, now_ts=now_ts
                        ):
                            continue
                        _mark_consumed(
                            rows,
                            row_index=row_index,
                            item_index=0,
                            call_id=request.call_id,
                        )
                        jsonl.write_all(path, rows)
                        return verdict
                    if kind != "tool_permission_batch":
                        continue
                    for item_index, item in enumerate(_items(row)):
                        if item.get("consumed_at"):
                            continue
                        if not _item_matches(item, row, request, now_ts=now_ts):
                            continue
                        _mark_consumed(
                            rows,
                            row_index=row_index,
                            item_index=item_index,
                            call_id=request.call_id,
                        )
                        jsonl.write_all(path, rows)
                        return verdict
        return None

    def ensure_pending(self, request: _Request) -> dict[str, Any]:
        """Create or merge one pending batch for this turn and requester scope."""

        created_at = time.time()
        try:
            expires_s = max(
                0.0,
                float(self.config.get("approvals.expire_seconds", 600) or 600),
            )
        except (TypeError, ValueError):
            expires_s = 600.0
        expires_at = created_at + expires_s
        scope_hash = hashlib.sha256(
            json.dumps(
                [
                    request.scope.session_id,
                    request.scope.strategy_id,
                    request.scope.actor_id,
                ],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:12]
        base_aid = (
            f"tool_batch_{_safe_id(request.turn_id) or _safe_id(request.call_id) or 'pending'}_"
            f"{scope_hash}"
        )
        terminal_ids: set[str] = set()
        for terminal_path in (
            self.config.paths.approvals_approved,
            self.config.paths.approvals_rejected,
        ):
            with _locked(terminal_path):
                terminal_ids.update(
                    str(row.get("approval_id") or row.get("id") or "")
                    for row in jsonl.read_all(terminal_path)
                )
        aid = base_aid
        revision = 2
        while aid in terminal_ids:
            aid = f"{base_aid}_r{revision}"
            revision += 1
        item_id = f"tool_{_safe_id(request.call_id) or request.fingerprint[:12]}"
        tool = {
            "name": request.tool_name,
            "skill_id": request.skill_id,
            "call_id": request.call_id,
            "fingerprint": request.fingerprint,
        }
        item = {
            "approval_id": item_id,
            "id": item_id,
            "kind": "tool_permission",
            "state": "pending",
            "turn_id": request.turn_id,
            "session_id": request.scope.session_id,
            "strategy_id": request.scope.strategy_id,
            "requester_actor_id": request.scope.actor_id,
            "requester_session_id": request.scope.session_id,
            "requester_strategy_id": request.scope.strategy_id,
            "requester_caller": request.caller,
            "expires_at": expires_at,
            "tool_use_id": request.call_id,
            "tool": tool,
            "reason": request.reason,
            "fingerprint": request.fingerprint,
            "payload": {
                "tool": tool,
                "risk": {"reasons": [request.reason]},
                "arguments": dict(request.arguments),
            },
        }

        def same_scope(row: dict[str, Any]) -> bool:
            return (
                str(row.get("requester_session_id") or row.get("session_id") or "").strip()
                == request.scope.session_id
                and str(row.get("requester_strategy_id") or row.get("strategy_id") or "").strip()
                == request.scope.strategy_id
                and str(row.get("requester_actor_id") or "").strip()
                == request.scope.actor_id
            )

        def merge(record: dict[str, Any]) -> dict[str, Any]:
            items = _items(record)
            if not any(
                str(x.get("tool_use_id") or _tool(x).get("call_id") or "")
                == request.call_id
                or str(x.get("fingerprint") or _tool(x).get("fingerprint") or "")
                == request.fingerprint
                for x in items
            ):
                items.append(item)
            reasons = list(dict.fromkeys(
                str(x.get("reason") or "") for x in items if str(x.get("reason") or "")
            ))
            tool_use_ids = list(dict.fromkeys(
                str(x.get("tool_use_id") or _tool(x).get("call_id") or "")
                for x in items
                if str(x.get("tool_use_id") or _tool(x).get("call_id") or "")
            ))
            fingerprints = list(dict.fromkeys(
                str(x.get("fingerprint") or _tool(x).get("fingerprint") or "")
                for x in items
                if str(x.get("fingerprint") or _tool(x).get("fingerprint") or "")
            ))
            first_tool = _tool(items[0]) if items else tool
            merged = {
                **record,
                "approval_id": aid,
                "id": aid,
                "kind": "tool_permission_batch",
                "state": "pending",
                "updated_at": time.time(),
                "updated_at_iso": now_iso(),
                "turn_id": request.turn_id,
                "session_id": request.scope.session_id,
                "strategy_id": request.scope.strategy_id,
                "requester_actor_id": request.scope.actor_id,
                "requester_session_id": request.scope.session_id,
                "requester_strategy_id": request.scope.strategy_id,
                "requester_caller": request.caller,
                "expires_at": float(record.get("expires_at") or expires_at),
                "tool_use_ids": tool_use_ids,
                "fingerprints": fingerprints,
                "tool": first_tool,
                "reason": (
                    f"{len(items)} tool calls require permission"
                    if len(items) != 1
                    else reasons[0] if reasons else request.reason
                ),
                "items": items,
                "payload": {
                    "kind": "tool_permission_batch",
                    "items": items,
                    "risk": {"reasons": reasons},
                },
            }
            merged.setdefault("created_at", created_at)
            merged.setdefault("created_at_iso", now_iso())
            return merged

        pending = self.config.paths.approvals_pending
        pending.parent.mkdir(parents=True, exist_ok=True)
        with _locked(pending):
            rows = [row for row in jsonl.read_all(pending) if not _expired(row)]
            record: dict[str, Any] | None = None
            for index, row in enumerate(rows):
                row_id = str(row.get("approval_id") or row.get("id") or "")
                if row_id == aid and same_scope(row):
                    record = merge(row)
                    rows[index] = record
                    break
            if record is None:
                record = merge({
                    "approval_id": aid,
                    "id": aid,
                    "kind": "tool_permission_batch",
                    "state": "pending",
                    "created_at": created_at,
                    "created_at_iso": now_iso(),
                    "items": [],
                })
                rows.append(record)
            jsonl.write_all(pending, rows)

        try:
            from ..messaging.approval_prompts import build_prompt

            prompt = build_prompt(record).as_dict()
        except Exception:
            prompt = {
                "approval_id": aid,
                "text": request.reason,
                "buttons": [],
            }
        return {
            "kind": "approval_request",
            "approval_id": aid,
            "call_id": request.call_id,
            "skill_id": request.skill_id,
            "action": request.tool_name,
            "prompt": prompt,
            "record": record,
            "reason": request.reason,
            "status": "pending",
        }


class ToolApprovalCoordinator:
    """One resolver shared by the parent loop and every child runtime."""

    def __init__(
        self,
        config: Config,
        *,
        scope: ToolApprovalScope,
        turn_id: str,
        resume_approval_id: str = "",
    ) -> None:
        self.store = ToolApprovalStore(config)
        self.scope = scope
        self.turn_id = str(turn_id or "")
        self.resume_approval_id = str(resume_approval_id or "").strip()

    def resolve(
        self,
        call: ToolCall,
        descriptor: ToolDescriptor,
        decision: PermissionDecision,
    ) -> ToolApprovalResolution:
        reason = (
            decision.approval_reason
            or decision.reason
            or "approval required before this tool can run"
        )
        request = _Request(
            turn_id=str(call.turn_id or self.turn_id),
            call_id=str(call.id or ""),
            tool_name=str(call.name or descriptor.name or ""),
            skill_id=str(descriptor.namespace or "native"),
            arguments=dict(call.arguments or {}),
            caller=str(call.caller or ""),
            reason=str(reason),
            scope=self.scope,
            fingerprint=tool_permission_fingerprint(
                str(call.name or descriptor.name or ""),
                dict(call.arguments or {}),
            ),
        )
        verdict = self.store.consume(
            request,
            approval_id=self.resume_approval_id,
        )
        if verdict is not None:
            return ToolApprovalResolution(verdict=verdict)
        return ToolApprovalResolution(
            verdict=None,
            request=self.store.ensure_pending(request),
        )


def broadcast_tool_approval(config: Config, record: dict[str, Any]) -> None:
    """Broadcast one completed tool-approval batch after loop projection."""

    if str(record.get("kind") or "") not in {
        "tool_permission",
        "tool_permission_batch",
    }:
        return
    try:
        from ..trading.approval import _broadcast_approval

        _broadcast_approval(config, record)
    except Exception:
        return


__all__ = [
    "ToolApprovalCoordinator",
    "ToolApprovalResolution",
    "ToolApprovalResolver",
    "ToolApprovalScope",
    "ToolApprovalStore",
    "broadcast_tool_approval",
    "tool_permission_fingerprint",
]
