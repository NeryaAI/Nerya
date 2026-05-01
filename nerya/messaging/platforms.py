"""Hermes-aligned gateway platform catalog for Nerya.

The catalog is the contract shared by the runtime, dashboard, and the
capability-development skill.  Platform-specific adapters can be native
(Telegram), webhook-backed (Discord/Slack/Mattermost/etc.), or scaffold-only
until an operator installs the concrete adapter proposal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class GatewayPlatformSpec:
    """Description of a gateway platform.

    ``support_level`` is the Plan 23 §4 capability claim — it tells the
    dashboard / docs whether this entry is a real adapter or just a
    catalog placeholder. Allowed values mirror the plan's recommendation:

    - ``catalog_only``    — listed for parity with Hermes, no adapter wired.
    - ``send_only``       — Nerya can send via this platform; inbound is
      proxied through ``/gateway/inbound`` only.
    - ``inbound_webhook`` — Nerya accepts webhook inbound + sends.
    - ``full_duplex``     — bi-directional native adapter.
    - ``tested``          — full_duplex with regression coverage.

    ``support_level`` is filled in by callers; ``status`` keeps the legacy
    Hermes-style label for compatibility (``native`` / ``webhook`` /
    ``scaffold``) — UIs that render the new field can ignore the old one.
    """

    id: str
    title: str
    hermes_id: str
    status: str
    inbound: str
    outbound: str
    typing: str
    menu: str
    attachments: bool = False
    voice: bool = False
    notes: str = ""
    config_refs: tuple[str, ...] = field(default_factory=tuple)
    support_level: str = "catalog_only"

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


_HERMES_PLATFORMS: tuple[GatewayPlatformSpec, ...] = (
    GatewayPlatformSpec("local", "Local / Dashboard", "local", "native", "http", "dashboard", "events", "dashboard", notes="Dashboard chat + local API.", support_level="full_duplex"),
    GatewayPlatformSpec("telegram", "Telegram", "telegram", "native", "polling", "bot_api", "sendChatAction", "setMyCommands", attachments=True, voice=True, config_refs=("bot_token_ref", "chat_id"), support_level="tested"),
    GatewayPlatformSpec("discord", "Discord", "discord", "webhook", "generic_inbound", "webhook", "status_webhook", "slash_commands_scaffold", attachments=True, config_refs=("webhook_url_ref", "webhook_url"), support_level="send_only"),
    GatewayPlatformSpec("whatsapp", "WhatsApp", "whatsapp", "scaffold", "generic_inbound", "webhook_or_bridge", "status_webhook", "commands_scaffold", attachments=True, voice=True, support_level="catalog_only"),
    GatewayPlatformSpec("slack", "Slack", "slack", "webhook", "generic_inbound", "incoming_webhook", "status_webhook", "slash_commands_scaffold", attachments=True, config_refs=("webhook_url_ref", "webhook_url"), support_level="send_only"),
    GatewayPlatformSpec("signal", "Signal", "signal", "scaffold", "generic_inbound", "http_bridge", "status_webhook", "commands_scaffold", attachments=True, voice=True, support_level="catalog_only"),
    GatewayPlatformSpec("mattermost", "Mattermost", "mattermost", "webhook", "generic_inbound", "incoming_webhook", "status_webhook", "slash_commands_scaffold", attachments=True, support_level="send_only"),
    GatewayPlatformSpec("matrix", "Matrix", "matrix", "scaffold", "generic_inbound", "http_bridge", "status_webhook", "commands_scaffold", attachments=True, support_level="catalog_only"),
    GatewayPlatformSpec("homeassistant", "Home Assistant", "homeassistant", "webhook", "generic_inbound", "webhook", "status_webhook", "service_scaffold", support_level="inbound_webhook"),
    GatewayPlatformSpec("email", "Email", "email", "scaffold", "generic_inbound", "smtp_or_api", "none", "none", attachments=True, support_level="catalog_only"),
    GatewayPlatformSpec("sms", "SMS", "sms", "scaffold", "generic_inbound", "twilio_or_webhook", "none", "commands_scaffold", support_level="catalog_only"),
    GatewayPlatformSpec("dingtalk", "DingTalk", "dingtalk", "webhook", "generic_inbound", "robot_webhook", "status_webhook", "commands_scaffold", attachments=True, support_level="send_only"),
    GatewayPlatformSpec("api_server", "API Server", "api_server", "native", "http", "http", "events", "openapi", notes="Universal /gateway/inbound + /gateway/send contract.", support_level="full_duplex"),
    GatewayPlatformSpec("webhook", "Generic Webhook", "webhook", "native", "http", "json_webhook", "status_webhook", "none", support_level="full_duplex"),
    GatewayPlatformSpec("feishu", "Feishu", "feishu", "webhook", "generic_inbound", "bot_webhook", "status_webhook", "commands_scaffold", attachments=True, support_level="send_only"),
    GatewayPlatformSpec("wecom", "WeCom", "wecom", "webhook", "generic_inbound", "bot_webhook", "status_webhook", "commands_scaffold", attachments=True, support_level="send_only"),
    GatewayPlatformSpec("wecom_callback", "WeCom Callback", "wecom_callback", "scaffold", "generic_inbound", "callback_api", "status_webhook", "commands_scaffold", attachments=True, support_level="catalog_only"),
    GatewayPlatformSpec("weixin", "Weixin", "weixin", "scaffold", "generic_inbound", "official_account_api", "status_webhook", "commands_scaffold", attachments=True, voice=True, support_level="catalog_only"),
    GatewayPlatformSpec("bluebubbles", "BlueBubbles", "bluebubbles", "scaffold", "generic_inbound", "http_bridge", "status_webhook", "none", attachments=True, support_level="catalog_only"),
    GatewayPlatformSpec("qqbot", "QQ Bot", "qqbot", "scaffold", "generic_inbound", "qqbot_api", "status_webhook", "commands_scaffold", attachments=True, support_level="catalog_only"),
)

PLATFORM_IDS: tuple[str, ...] = tuple(p.id for p in _HERMES_PLATFORMS)


def list_platforms() -> list[dict[str, Any]]:
    return [p.asdict() for p in _HERMES_PLATFORMS]


def get_platform(platform_id: str) -> GatewayPlatformSpec | None:
    key = str(platform_id or "").strip().lower()
    for spec in _HERMES_PLATFORMS:
        if key in {spec.id, spec.hermes_id}:
            return spec
    return None


def require_platform(platform_id: str) -> GatewayPlatformSpec:
    spec = get_platform(platform_id)
    if spec is None:
        raise ValueError(f"unknown gateway platform: {platform_id!r}")
    return spec


def channel_kind_for(platform_id: str) -> str:
    spec = require_platform(platform_id)
    return spec.id
