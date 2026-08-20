"""Transport-neutral approval state transitions.

HTTP, ACP and gateway callbacks should differ only in authentication and wire
format. Owner binding, least-privilege scope, expiry, JSONL movement, SQLite
state and approved-domain resume all live here.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any

from .core import jsonl
from .core.config import Config
from .core.time import now_iso


_NATIVE_TOOL_KINDS = frozenset({"tool_permission", "tool_permission_batch"})
_FINANCIAL_KINDS = frozenset({"trade_intent", "wallet_swap"})


@contextmanager
def _locked(path):
    lock_path = path.with_name(f".{path.name}.lock")
    with jsonl._open_append(lock_path):  # noqa: SLF001
        yield


class ApprovalService:
    def __init__(self, config: Config):
        self.config = config

    @staticmethod
    def is_native_tool(record: dict[str, Any]) -> bool:
        return str(record.get("kind") or "").strip() in _NATIVE_TOOL_KINDS

    @staticmethod
    def required_scope(record: dict[str, Any]) -> str:
        return (
            "approve:trade"
            if str(record.get("kind") or "").strip() in _FINANCIAL_KINDS
            else "approve:tool"
        )

    @classmethod
    def owner_actor_id(cls, record: dict[str, Any]) -> str:
        for key in ("approval_actor_id", "actor_id"):
            actor_id = str(record.get(key) or "").strip()
            if actor_id:
                return actor_id
        if cls.is_native_tool(record):
            return str(record.get("requester_actor_id") or "").strip()
        return ""

    @staticmethod
    def expired(record: dict[str, Any], *, now: float | None = None) -> bool:
        if "expires_at" not in record or record.get("expires_at") in (None, ""):
            return False
        try:
            return float(record["expires_at"]) <= float(
                now if now is not None else time.time()
            )
        except (TypeError, ValueError):
            return True

    @classmethod
    def can_resolve(
        cls,
        record: dict[str, Any],
        actor_id: str,
        *,
        operator_authorized: bool = False,
    ) -> bool:
        actor_id = str(actor_id or "").strip()
        if not actor_id:
            return False
        owner = cls.owner_actor_id(record)
        if cls.is_native_tool(record):
            return bool(owner) and (operator_authorized or actor_id == owner)
        return not owner or operator_authorized or actor_id == owner

    @classmethod
    def trusted_operator(
        cls,
        payload: dict[str, Any],
        actor_id: str,
        record: dict[str, Any],
    ) -> bool:
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
            scopes = {
                str(part).strip()
                for part in raw_scopes
                if str(part).strip()
            }
        else:
            scopes = set()
        return "api:all" in scopes or cls.required_scope(record) in scopes

    def pending(self) -> list[dict[str, Any]]:
        path = self.config.paths.approvals_pending
        if not path.exists():
            return []
        now = time.time()
        return [
            record
            for record in jsonl.read_all(path)
            if (not record.get("state") or record["state"] == "pending")
            and not self.expired(record, now=now)
        ]

    def find(self, approval_id: str) -> dict[str, Any] | None:
        approval_id = str(approval_id or "").strip()
        for record in self.pending():
            if (
                str(record.get("approval_id") or "") == approval_id
                or str(record.get("id") or "") == approval_id
            ):
                return record
        return None

    def move(
        self,
        approval_id: str,
        *,
        state: str,
        note: str = "",
        resolver_actor_id: str = "",
        operator_authorized: bool = False,
    ) -> dict[str, Any] | None:
        if state not in {"approved", "rejected"}:
            raise ValueError(f"unsupported approval state: {state}")
        paths = self.config.paths
        source = paths.approvals_pending
        if not source.exists():
            return None
        with _locked(source):
            rows = jsonl.read_all(source)
            kept: list[dict[str, Any]] = []
            moved: dict[str, Any] | None = None
            for record in rows:
                matches = (
                    str(record.get("approval_id") or "") == approval_id
                    or str(record.get("id") or "") == approval_id
                )
                if moved is None and matches:
                    if self.expired(record):
                        return None
                    if not self.can_resolve(
                        record,
                        resolver_actor_id,
                        operator_authorized=operator_authorized,
                    ):
                        return None
                    record["state"] = state
                    record["state_ts"] = now_iso()
                    if note:
                        record["state_note"] = note
                    if resolver_actor_id:
                        record["resolved_by_actor_id"] = str(
                            resolver_actor_id
                        )
                    moved = record
                    continue
                kept.append(record)
            if moved is None:
                return None
            jsonl.write_all(source, kept)
            target = (
                paths.approvals_approved
                if state == "approved"
                else paths.approvals_rejected
            )
            with _locked(target):
                jsonl.append(target, moved)

        try:
            from .db.repositories import ApprovalRepository
            from .db.sqlite import connect

            connection = connect(paths.db)
            ApprovalRepository(connection).set_state(approval_id, state)
            connection.close()
        except Exception:
            pass
        return moved

    def publish_resolution(
        self,
        approval_id: str,
        *,
        state: str,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        resume_result: dict[str, Any] | None = None
        resolved = record or {}
        try:
            from .agent.streaming import get_default_bus

            get_default_bus().publish(
                "approval.resolved",
                approval_id=approval_id,
                state=state,
                resolver_actor_id=resolved.get("resolved_by_actor_id"),
                approval_kind=resolved.get("kind"),
                session_id=resolved.get("session_id"),
                strategy_id=resolved.get("strategy_id"),
                record=resolved,
            )
        except Exception:
            pass

        kind = str(resolved.get("kind") or "").strip()
        if str(state or "").lower() != "approved":
            return None
        try:
            if kind == "trade_intent":
                from .trading import approval_resume

                if not getattr(
                    approval_resume,
                    "_resume_subscriber_registered",
                    False,
                ):
                    resume_result = approval_resume.resume_approved(
                        self.config,
                        approval_id,
                    )
            elif kind == "wallet_swap":
                from .wallet import swap_approval

                resume_result = swap_approval.resume_approved(
                    self.config,
                    approval_id,
                )
        except Exception as exc:  # pragma: no cover - callback still resolves
            resume_result = {
                "ok": False,
                "approval_id": approval_id,
                "error": f"approval_resume_dispatch_failed:{exc}",
            }
        return resume_result


__all__ = ["ApprovalService"]
