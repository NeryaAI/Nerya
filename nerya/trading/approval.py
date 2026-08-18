"""Approval Gate. Writes pending approvals and checks before execution."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from ..core.config import Config
from ..core.errors import ApprovalPending
from ..core.ids import approval_id
from ..core import jsonl
from ..core.time import now_iso
from ..db.repositories import ApprovalRepository
from ..db.sqlite import connect
from .intents import TradeIntent
from .risk import RiskDecision


@dataclass
class ApprovalRecord:
    approval_id: str
    kind: str
    state: str           # pending / approved / rejected / expired
    created_at: float
    expires_at: float
    intent: dict[str, Any]
    risk: dict[str, Any]

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


class ApprovalGate:
    def __init__(self, config: Config):
        self.config = config
        self._con = None

    def _con_lazy(self):
        if self._con is None:
            self._con = connect(self.config.paths.db)
        return self._con

    def _record_payload(
        self,
        aid: str,
        intent: TradeIntent,
        decision: RiskDecision,
        *,
        market_snapshot: dict[str, Any] | None = None,
        plan: Any = None,
        created_at: float | None = None,
        expires_at: float | None = None,
    ) -> dict[str, Any]:
        meta = dict(intent.meta or {})
        try:
            from .accounts import get_account_profile

            profile = get_account_profile(self.config.paths, intent.account_id)
            execution_mode = str(profile.mode)
        except Exception:
            execution_mode = "unknown"
        record = {
            "approval_id": aid,
            "kind": "trade_intent",
            "state": "pending",
            "created_at": created_at if created_at is not None else time.time(),
            "expires_at": expires_at,
            "intent_id": intent.intent_id,
            "strategy_id": intent.strategy_id,
            "account_id": intent.account_id,
            "market": intent.market,
            "side": intent.side,
            "order_type": intent.order_type,
            "size": intent.size,
            "size_unit": intent.size_unit,
            "limit_price": intent.limit_price,
            "stop_price": intent.stop_price,
            "source": intent.source,
            "execution_mode": execution_mode,
            "risk_reasons": decision.reasons,
            "notional_usd": decision.estimated_notional_usd,
            "intent": intent.asdict(),
            "risk": decision.asdict(),
        }
        session_id = str(
            meta.get("agent_session_id")
            or meta.get("conversation_id")
            or ""
        ).strip()
        actor_id = str(meta.get("actor_id") or "").strip()
        turn_id = str(meta.get("turn_id") or "").strip()
        tool_call_id = str(meta.get("tool_call_id") or "").strip()
        if session_id:
            record["session_id"] = session_id
        if actor_id:
            record["actor_id"] = actor_id
            record["approval_actor_id"] = actor_id
        if turn_id:
            record["turn_id"] = turn_id
        if tool_call_id:
            record["tool_call_id"] = tool_call_id
        # Freeze the market snapshot and plan at escalation time so the
        # resume path can replay against the exact market state the risk
        # decision was made on (no drift between escalation and approval).
        if market_snapshot is not None:
            record["frozen_market_snapshot"] = dict(market_snapshot)
        if plan is not None:
            try:
                record["frozen_plan"] = plan.asdict()
            except Exception:
                pass
        return record

    def require(
        self,
        intent: TradeIntent,
        decision: RiskDecision,
        *,
        market_snapshot: dict[str, Any] | None = None,
        plan: Any = None,
    ) -> ApprovalRecord:
        expires_s = float(self.config.get("approvals.expire_seconds", 600))
        aid = approval_id()
        created_at = time.time()
        expires_at = created_at + expires_s
        repo = ApprovalRepository(self._con_lazy())
        record = self._record_payload(
            aid,
            intent,
            decision,
            market_snapshot=market_snapshot,
            plan=plan,
            created_at=created_at,
            expires_at=expires_at,
        )
        # Store the rich, redaction-safe approval record in SQLite too. The
        # JSONL queue drives the UI; the DB is the crash-recovery source used
        # by approval_resume when processes restart.
        payload = dict(record)
        repo.insert(id=aid, kind="trade_intent", expires_s=expires_s, payload=payload)
        jsonl.append(self.config.paths.approvals_pending, record)
        # Fan the new approval out to every configured messaging channel
        # that opted into the approvals topic so operators can resolve it
        # from wherever they were notified. Best-effort: a delivery
        # hiccup must never break the trading path itself.
        try:
            _broadcast_approval(self.config, record)
        except Exception:
            pass
        raise ApprovalPending(aid)

    def auto_approve(
        self,
        intent: TradeIntent,
        decision: RiskDecision,
        *,
        reason: str,
    ) -> ApprovalRecord:
        """Record an approval-gate signoff without creating a pending card.

        Strategy-originated order flows can opt into this when RiskGate
        escalates for policy reasons such as approval thresholds. The
        gate still writes the same DB/audit surfaces as a manual
        approval, but no operator-facing pending request is broadcast.
        """

        expires_s = float(self.config.get("approvals.expire_seconds", 600))
        aid = approval_id()
        created_at = time.time()
        repo = ApprovalRepository(self._con_lazy())
        payload = {"intent": intent.asdict(), "risk": decision.asdict()}
        repo.insert(id=aid, kind="trade_intent", expires_s=expires_s, payload=payload)
        repo.set_state(aid, "approved")
        record = self._record_payload(aid, intent, decision)
        record.update({
            "state": "approved",
            "auto": True,
            "auto_approved": True,
            "reason": reason,
            "state_ts": now_iso(),
        })
        jsonl.append(self.config.paths.approvals_approved, record)
        jsonl.append(self.config.paths.journal("trading"), {
            "kind": "approval.auto_approved",
            "ts": now_iso(),
            "approval_id": aid,
            "intent_id": intent.intent_id,
            "strategy_id": intent.strategy_id,
            "market": intent.market,
            "side": intent.side,
            "reason": reason,
            "risk_reasons": decision.reasons,
        })
        return ApprovalRecord(
            approval_id=aid,
            kind="trade_intent",
            state="approved",
            created_at=created_at,
            expires_at=created_at + expires_s,
            intent=intent.asdict(),
            risk=decision.asdict(),
        )

    def approve(self, aid: str) -> None:
        repo = ApprovalRepository(self._con_lazy())
        repo.set_state(aid, "approved")
        jsonl.append(self.config.paths.approvals_approved,
                     {"approval_id": aid, "state": "approved"})

    def reject(self, aid: str, reason: str) -> None:
        repo = ApprovalRepository(self._con_lazy())
        repo.set_state(aid, "rejected")
        jsonl.append(self.config.paths.approvals_rejected,
                     {"approval_id": aid, "state": "rejected", "reason": reason})

    def list_pending(self) -> list[dict[str, Any]]:
        repo = ApprovalRepository(self._con_lazy())
        now = time.time()
        out = []
        for row in repo.list_pending():
            if row["expires_at"] <= now:
                self.reject(row["id"], "expired")
                continue
            out.append(row)
        return out


def _broadcast_approval(config: Config, record: dict[str, Any]) -> None:
    """Fan a new approval out to every messaging channel that opted in.

    A channel opts in by setting ``approvals: true`` (or
    ``topics: [approvals, …]``) in
    ``workspace/messages/channels.yml``. Today we wire up Telegram —
    each opted-in channel receives the inline-keyboard approval card
    rendered by :func:`messaging.approval_prompts.build_prompt`. The
    same plumbing slots cleanly into Slack / Discord / Feishu later.
    """

    from ..core import yaml_io
    from ..messaging import telegram as _tg
    from ..messaging.approval_prompts import build_prompt
    from ..security.secrets import SecretVault

    paths = config.paths
    doc = yaml_io.load(paths.messages_channels, default={}) or {}
    channels: dict[str, Any] = doc.get("channels") or {}
    if not channels:
        return

    def _resolve_secret(ref: str) -> str | None:
        if not ref or not ref.startswith("vault://"):
            return None
        name = ref[len("vault://"):]
        try:
            vault = SecretVault.open(paths.vault_enc)
            return vault.resolve(name, required_scope="messaging")
        except Exception:
            return None

    def _wants_approvals(cfg: dict[str, Any]) -> bool:
        # Operators can hard-disable an existing chat by setting
        # ``approvals: false`` in their ``messages/channels.yml`` entry.
        if cfg.get("approvals") is False:
            return False
        topics = cfg.get("topics")
        if isinstance(topics, list) and topics:
            return "approvals" in topics
        # Treat approval fan-out as opt-out so a newly bound channel
        # receives approval prompts without extra configuration.
        return True

    prompt = build_prompt(record, actor_id=str(record.get("actor_id") or ""))
    aid = str(record.get("approval_id") or approval_id())

    for name, raw in channels.items():
        cfg = raw or {}
        if not isinstance(cfg, dict):
            continue
        if not _wants_approvals(cfg):
            continue
        kind = str(cfg.get("kind") or "").lower()
        if kind == "telegram":
            envelope: dict[str, Any] = {
                "message_id": f"approval-{aid}-{name}",
                "channel": name,
                "kind": "telegram",
                "approval_id": aid,
                "text": prompt.text,
                "reply_markup": prompt.telegram_reply_markup(),
                "buttons": [b.as_dict() for b in prompt.buttons],
                "event": {"kind": "approval.request", "approval_id": aid},
            }
            try:
                _tg.send(
                    paths.outbox_messages,
                    envelope,
                    channel_cfg=cfg,
                    resolve_secret=_resolve_secret,
                )
            except Exception:
                continue
        else:
            # Webhook/generic gateway clients receive a structured
            # approval card payload. Platforms that can render native
            # buttons use ``buttons``; plain text clients still show the
            # prompt and can POST the callback_data to /approvals/callback.
            from ..messaging import generic_platform as _generic
            try:
                _generic.send(
                    paths.outbox_messages,
                    {
                        "message_id": f"approval-{aid}-{name}",
                        "channel": name,
                        "kind": kind or name,
                        "approval_id": aid,
                        "text": prompt.text,
                        "buttons": [b.as_dict() for b in prompt.buttons],
                        "event": {"kind": "approval.request", "approval_id": aid},
                    },
                    channel_cfg={**cfg, "kind": kind or name},
                    resolve_secret=_resolve_secret,
                )
            except Exception:
                continue
