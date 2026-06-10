"""Message dispatch pipeline — renders templates, enforces rate limits,
routes to channel transports, and records the delivery."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core import jsonl, yaml_io
from ..core.config import Config
from ..core.ids import message_id
from ..core.time import now_iso
from . import dashboard, discord, generic_platform, telegram, webhook
from .platforms import PLATFORM_IDS
from .rate_limits import RateLimiter
from .templates import render
from .transport import MessagingTransport, UrllibMessagingTransport


_CHANNEL_SENDERS = {
    "dashboard": dashboard.send,
    "local": dashboard.send,
    "telegram": telegram.send,
    "discord": discord.send,
    "webhook": webhook.send,
}
for _platform_id in PLATFORM_IDS:
    _CHANNEL_SENDERS.setdefault(_platform_id, generic_platform.send)


@dataclass
class MessagePipeline:
    config: Config
    rate_limiter: RateLimiter = field(default_factory=RateLimiter)
    transport: MessagingTransport | None = None
    resolve_secret: Callable[[str], str | None] | None = None

    def send(self, *, channel: str, text: str,
             strategy_id: str | None = None,
             template: str | None = None,
             context: dict[str, Any] | None = None,
             attachments: list[dict[str, Any]] | None = None,
             severity: str | None = None) -> dict[str, Any]:
        channels_doc = yaml_io.load(self.config.paths.messages_channels, default={}) or {}
        context = context or {}
        effective_severity = _clean_severity(
            severity
            or context.get("severity")
            or context.get("priority")
            or context.get("level")
        )
        routed_channels = _severity_route_channels(channels_doc, effective_severity)
        if routed_channels is not None:
            return self._send_routed(
                channels_doc=channels_doc,
                original_channel=channel,
                channels=routed_channels,
                text=text,
                strategy_id=strategy_id,
                template=template,
                context=context,
                attachments=attachments,
                severity=effective_severity,
            )
        return self._send_one(
            channels_doc=channels_doc,
            channel=channel,
            text=text,
            strategy_id=strategy_id,
            template=template,
            context=context,
            attachments=attachments,
            severity=effective_severity,
        )

    def _send_routed(
        self,
        *,
        channels_doc: dict[str, Any],
        original_channel: str,
        channels: list[str],
        text: str,
        strategy_id: str | None,
        template: str | None,
        context: dict[str, Any],
        attachments: list[dict[str, Any]] | None,
        severity: str | None,
    ) -> dict[str, Any]:
        if not channels:
            mid = message_id()
            msg = {
                "message_id": mid,
                "channel": original_channel,
                "kind": "severity_route",
                "text": text,
                "strategy_id": strategy_id,
                "ts": now_iso(),
                "delivered": False,
                "rate_limited": False,
                "suppressed": True,
                "severity": severity,
                "channels": [],
                "attachments": list(attachments or []),
            }
            jsonl.append(
                self.config.paths.journal("messages"),
                _journal_record("message.suppressed", msg),
            )
            return msg

        deliveries = [
            self._send_one(
                channels_doc=channels_doc,
                channel=target,
                text=text,
                strategy_id=strategy_id,
                template=template,
                context=context,
                attachments=attachments,
                severity=severity,
            )
            for target in channels
        ]
        if len(deliveries) == 1:
            row = dict(deliveries[0])
            row["channels"] = channels
            row["routed_from"] = original_channel
            return row
        return {
            "message_id": ",".join(str(row.get("message_id") or "") for row in deliveries),
            "channel": original_channel,
            "channels": channels,
            "severity": severity,
            "delivered": all(bool(row.get("delivered")) for row in deliveries),
            "rate_limited": any(bool(row.get("rate_limited")) for row in deliveries),
            "deliveries": deliveries,
        }

    def _send_one(
        self,
        *,
        channels_doc: dict[str, Any],
        channel: str,
        text: str,
        strategy_id: str | None = None,
        template: str | None = None,
        context: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        severity: str | None = None,
    ) -> dict[str, Any]:
        channel_cfg = (channels_doc.get("channels") or {}).get(channel) or {}
        kind = channel_cfg.get("kind", channel)
        if template:
            text = render(template, context or {})
        allowed = self.rate_limiter.allow(channel)
        mid = message_id()
        msg = {
            "message_id": mid, "channel": channel, "kind": kind,
            "text": text, "strategy_id": strategy_id,
            "ts": now_iso(),
            "delivered": False,
            "rate_limited": not allowed,
            "attachments": list(attachments or []),
        }
        if severity:
            msg["severity"] = severity
        if not allowed:
            jsonl.append(
                self.config.paths.journal("messages"),
                _journal_record("message.rate_limited", msg),
            )
            return msg
        sender = _CHANNEL_SENDERS.get(kind, dashboard.send)
        resolver = self.resolve_secret or self._default_resolver
        out_path: Path = _invoke_sender(
            sender=sender,
            outbox=self.config.paths.outbox_messages,
            message=msg,
            channel_cfg=channel_cfg,
            resolver=resolver,
            transport=self.transport or UrllibMessagingTransport(),
        )
        msg["delivered"] = bool(msg.get("delivered", True))
        msg["outbox_path"] = str(out_path)
        jsonl.append(
            self.config.paths.journal("messages"),
            _journal_record("message.sent", msg),
        )
        return msg

    def _default_resolver(self, ref: str) -> str | None:
        """Resolve a ``vault://name`` ref with the workspace vault."""
        if not ref or not isinstance(ref, str) or not ref.startswith("vault://"):
            return None
        name = ref[len("vault://"):]
        try:
            from ..security.secrets import SecretVault
            vault_path = self.config.paths.vault_enc
            if not vault_path.exists():
                return None
            vault = SecretVault.open(vault_path)
            return vault.resolve(name, required_scope="messaging")
        except Exception:
            return None


def _invoke_sender(*, sender, outbox, message, channel_cfg, resolver, transport) -> Path:
    """Call a channel sender, with fallback for dashboard (no extras)."""
    try:
        return sender(outbox, message,
                       channel_cfg=channel_cfg,
                       resolve_secret=resolver,
                       transport=transport)
    except TypeError:
        # dashboard.send has the legacy signature
        return sender(outbox, message)


def _clean_severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text


def _severity_route_channels(
    channels_doc: dict[str, Any],
    severity: str | None,
) -> list[str] | None:
    if not severity:
        return None
    routes = channels_doc.get("severity_routes")
    if not isinstance(routes, dict):
        routing = channels_doc.get("routing")
        if isinstance(routing, dict):
            routes = routing.get("severity")
    if not isinstance(routes, dict):
        return None
    value = routes.get(severity)
    if value is None:
        value = routes.get("*")
    if value is None:
        return None
    if isinstance(value, str):
        if value.strip().lower() in {"", "none", "silent", "drop", "suppress"}:
            return []
        return [value.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _journal_record(event_kind: str, message: dict[str, Any]) -> dict[str, Any]:
    row = dict(message)
    row["channel_kind"] = row.get("kind")
    row["kind"] = event_kind
    return row
