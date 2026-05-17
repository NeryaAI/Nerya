"""Gateway platform catalog for Nerya.

The catalog is the contract shared by the runtime, dashboard, and the
capability-development skill.  Platform-specific adapters can be native
(Telegram), webhook-backed (Discord/Slack/Mattermost/etc.), or scaffold-only
until an operator installs the concrete adapter proposal.

Each spec also carries:

- ``docs_url`` — the canonical official-bot setup documentation. The dashboard
  surfaces this as an inline "Setup docs ↗" link next to the platform
  dropdown so the operator never has to alt-tab to find BotFather, Slack API,
  Feishu Open Platform, etc.
- ``secret_fields`` — the per-platform credential fields that must be
  populated before the channel can carry a real session. The dashboard uses
  this to render the right form for each platform, and the runtime uses it
  in :func:`nerya.api.routes_gateway._gateway_channel_configured` to mark a
  channel as "configured" only when ALL of its required fields are present.
- ``setup_steps`` — short ordered checklist (3-6 bullets) shown as a
  collapsible "Setup checklist" beside the form. Operators can read it
  without leaving the dashboard.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class GatewaySecretField:
    """One auth/identity field expected by a gateway platform.

    ``key`` is the YAML field stored under ``messages/channels.yml`` for the
    plaintext value (e.g. ``bot_token``). ``ref_key`` is the corresponding
    vault-pointer field (``bot_token_ref``). ``kind`` is one of
    ``secret`` / ``url`` / ``opaque`` / ``id`` and decides how the dashboard
    masks the input. ``required`` decides whether the channel can be marked
    "configured" without it.
    """

    key: str
    ref_key: str
    label: str
    kind: str = "secret"
    required: bool = True
    placeholder: str = ""
    description: str = ""

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GatewayPlatformSpec:
    """Description of a gateway platform.

    ``support_level`` is the capability claim — it tells the
    dashboard whether this entry is a real adapter or just a
    catalog placeholder. Allowed values describe the runtime support model:

    - ``catalog_only``    — listed as a placeholder, no adapter wired.
    - ``send_only``       — Nerya can send via this platform; inbound is
      proxied through ``/gateway/inbound`` only.
    - ``inbound_webhook`` — Nerya accepts webhook inbound + sends.
    - ``full_duplex``     — bi-directional native adapter.
    - ``tested``          — full_duplex with regression coverage.

    ``support_level`` is filled in by callers; ``status`` keeps the legacy
    legacy label for compatibility (``native`` / ``webhook`` /
    ``scaffold``) — UIs that render the new field can ignore the old one.
    """

    id: str
    title: str
    alias_id: str
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
    docs_url: str = ""
    secret_fields: tuple[GatewaySecretField, ...] = field(default_factory=tuple)
    setup_steps: tuple[str, ...] = field(default_factory=tuple)

    def asdict(self) -> dict[str, Any]:
        data = asdict(self)
        data["secret_fields"] = [f.asdict() for f in self.secret_fields]
        return data

    def required_field_keys(self) -> tuple[str, ...]:
        return tuple(f.key for f in self.secret_fields if f.required)

    def required_ref_keys(self) -> tuple[str, ...]:
        return tuple(f.ref_key for f in self.secret_fields if f.required)


# ---------------------------------------------------------- field templates


def _bot_token() -> GatewaySecretField:
    return GatewaySecretField(
        key="bot_token",
        ref_key="bot_token_ref",
        label="Bot Token",
        kind="secret",
        placeholder="123456:ABCdef...",
        description="Token issued by the platform's bot management page.",
    )


def _webhook(label: str = "Incoming Webhook URL", *, description: str = "",
             required: bool = True) -> GatewaySecretField:
    return GatewaySecretField(
        key="webhook_url",
        ref_key="webhook_url_ref",
        label=label,
        kind="url",
        required=required,
        placeholder="https://...",
        description=description,
    )


def _slack_webhook() -> GatewaySecretField:
    return GatewaySecretField(
        key="incoming_webhook_url",
        ref_key="incoming_webhook_url_ref",
        label="Incoming Webhook URL",
        kind="url",
        placeholder="https://hooks.slack.com/services/T0/B0/xxx",
        description="From Slack App → Incoming Webhooks. Used for outbound.",
    )


def _slack_signing() -> GatewaySecretField:
    return GatewaySecretField(
        key="signing_secret",
        ref_key="signing_secret_ref",
        label="Signing Secret",
        kind="secret",
        required=False,
        placeholder="32-char hex",
        description="From Slack App → Basic Information. Verifies inbound events.",
    )


def _slack_bot_token() -> GatewaySecretField:
    return GatewaySecretField(
        key="bot_token",
        ref_key="bot_token_ref",
        label="Bot User OAuth Token",
        kind="secret",
        required=False,
        placeholder="xoxb-...",
        description="From Slack App → OAuth & Permissions. Required for chat.postMessage.",
    )


def _feishu_app_id() -> GatewaySecretField:
    return GatewaySecretField(
        key="app_id",
        ref_key="app_id_ref",
        label="App ID",
        kind="opaque",
        placeholder="cli_a1b2c3d4...",
        description="From Feishu Open Platform → 凭证与基础信息.",
    )


def _feishu_app_secret() -> GatewaySecretField:
    return GatewaySecretField(
        key="app_secret",
        ref_key="app_secret_ref",
        label="App Secret",
        kind="secret",
        placeholder="32-char secret",
        description="From Feishu Open Platform → 凭证与基础信息.",
    )


def _feishu_verification() -> GatewaySecretField:
    return GatewaySecretField(
        key="verification_token",
        ref_key="verification_token_ref",
        label="Verification Token",
        kind="secret",
        required=False,
        placeholder="optional",
        description="Used to verify inbound event subscriptions.",
    )


def _wecom_corp_id() -> GatewaySecretField:
    return GatewaySecretField(
        key="corp_id",
        ref_key="corp_id_ref",
        label="Corp ID",
        kind="opaque",
        placeholder="ww1234...",
        description="From WeCom Admin → 企业信息.",
    )


def _wecom_agent_id() -> GatewaySecretField:
    return GatewaySecretField(
        key="agent_id",
        ref_key="agent_id_ref",
        label="Agent ID",
        kind="opaque",
        placeholder="1000002",
        description="From WeCom Admin → 应用 → 自建应用.",
    )


def _wecom_secret() -> GatewaySecretField:
    return GatewaySecretField(
        key="app_secret",
        ref_key="app_secret_ref",
        label="App Secret",
        kind="secret",
        placeholder="agent secret",
        description="The app's Secret in WeCom admin.",
    )


def _wecom_robot_url() -> GatewaySecretField:
    return GatewaySecretField(
        key="webhook_url",
        ref_key="webhook_url_ref",
        label="Group Robot Webhook URL",
        kind="url",
        required=False,
        placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...",
        description="Optional 群机器人 inbound URL for outbound messages.",
    )


def _dingtalk_robot() -> GatewaySecretField:
    return GatewaySecretField(
        key="webhook_url",
        ref_key="webhook_url_ref",
        label="Robot Webhook URL",
        kind="url",
        placeholder="https://oapi.dingtalk.com/robot/send?access_token=...",
        description="From DingTalk group → 智能群助手 → 添加机器人.",
    )


def _dingtalk_secret() -> GatewaySecretField:
    return GatewaySecretField(
        key="signing_secret",
        ref_key="signing_secret_ref",
        label="Signing Secret",
        kind="secret",
        required=False,
        placeholder="SEC...",
        description="Required when 加签 is enabled on the robot.",
    )


def _whatsapp_token() -> GatewaySecretField:
    return GatewaySecretField(
        key="bot_token",
        ref_key="bot_token_ref",
        label="Cloud API Access Token",
        kind="secret",
        placeholder="EAAJ...",
        description="From Meta for Developers → WhatsApp → API Setup.",
    )


def _whatsapp_phone_id() -> GatewaySecretField:
    return GatewaySecretField(
        key="phone_number_id",
        ref_key="phone_number_id_ref",
        label="Phone Number ID",
        kind="id",
        placeholder="111122223333444",
        description="From WhatsApp → API Setup → From phone number ID.",
    )


def _whatsapp_business_id() -> GatewaySecretField:
    return GatewaySecretField(
        key="business_account_id",
        ref_key="business_account_id_ref",
        label="WABA ID",
        kind="id",
        required=False,
        placeholder="000011112222333",
        description="WhatsApp Business Account ID (optional, for templates).",
    )


def _bridge_url(label: str = "Bridge URL", description: str = "") -> GatewaySecretField:
    return GatewaySecretField(
        key="bridge_url",
        ref_key="bridge_url_ref",
        label=label,
        kind="url",
        placeholder="https://bridge.local",
        description=description or "Self-hosted bridge HTTP endpoint.",
    )


def _matrix_token() -> GatewaySecretField:
    return GatewaySecretField(
        key="bot_token",
        ref_key="bot_token_ref",
        label="Access Token",
        kind="secret",
        placeholder="syt_...",
        description="From Matrix client → Settings → Help & About → Advanced.",
    )


def _matrix_homeserver() -> GatewaySecretField:
    return GatewaySecretField(
        key="homeserver_url",
        ref_key="homeserver_url_ref",
        label="Homeserver URL",
        kind="url",
        placeholder="https://matrix.org",
        description="The homeserver this access token belongs to.",
    )


def _matrix_user() -> GatewaySecretField:
    return GatewaySecretField(
        key="user_id",
        ref_key="user_id_ref",
        label="Bot User ID",
        kind="id",
        required=False,
        placeholder="@nerya:matrix.org",
        description="The Matrix user the bot signs in as.",
    )


def _email_smtp() -> GatewaySecretField:
    return GatewaySecretField(
        key="smtp_url",
        ref_key="smtp_url_ref",
        label="SMTP URL",
        kind="url",
        placeholder="smtps://user:password@smtp.example.com:465",
        description="Outbound SMTP. Use smtps:// for SSL or smtp:// for STARTTLS.",
    )


def _email_imap() -> GatewaySecretField:
    return GatewaySecretField(
        key="imap_url",
        ref_key="imap_url_ref",
        label="IMAP URL",
        kind="url",
        required=False,
        placeholder="imaps://user:password@imap.example.com:993",
        description="Optional inbound polling.",
    )


def _sms_account_sid() -> GatewaySecretField:
    return GatewaySecretField(
        key="account_sid",
        ref_key="account_sid_ref",
        label="Account SID",
        kind="id",
        placeholder="AC...",
        description="Twilio Account SID.",
    )


def _sms_auth_token() -> GatewaySecretField:
    return GatewaySecretField(
        key="auth_token",
        ref_key="auth_token_ref",
        label="Auth Token",
        kind="secret",
        placeholder="32-char token",
        description="Twilio Auth Token.",
    )


def _sms_from() -> GatewaySecretField:
    return GatewaySecretField(
        key="from_number",
        ref_key="from_number_ref",
        label="From Number",
        kind="id",
        placeholder="+15551234567",
        description="Twilio phone number for outbound SMS.",
    )


def _qq_bot_app_id() -> GatewaySecretField:
    return GatewaySecretField(
        key="app_id",
        ref_key="app_id_ref",
        label="App ID",
        kind="opaque",
        placeholder="QQ Bot App ID",
        description="From QQ Bot Open Platform → Application → Basic Info.",
    )


def _qq_bot_app_secret() -> GatewaySecretField:
    return GatewaySecretField(
        key="app_secret",
        ref_key="app_secret_ref",
        label="App Secret",
        kind="secret",
        placeholder="App Secret",
        description="From QQ Bot Open Platform → Application → Basic Info.",
    )


def _bluebubbles_url() -> GatewaySecretField:
    return GatewaySecretField(
        key="bridge_url",
        ref_key="bridge_url_ref",
        label="Server URL",
        kind="url",
        placeholder="http://blue.local:1234",
        description="BlueBubbles server URL on the Mac running BlueBubbles.app.",
    )


def _bluebubbles_password() -> GatewaySecretField:
    return GatewaySecretField(
        key="bot_token",
        ref_key="bot_token_ref",
        label="Server Password",
        kind="secret",
        placeholder="server password",
        description="BlueBubbles server password from the Mac app.",
    )


def _signal_phone() -> GatewaySecretField:
    return GatewaySecretField(
        key="phone_number",
        ref_key="phone_number_ref",
        label="Phone Number",
        kind="id",
        placeholder="+15551234567",
        description="Signal-bound phone number registered with signal-cli.",
    )


def _ha_token() -> GatewaySecretField:
    return GatewaySecretField(
        key="bot_token",
        ref_key="bot_token_ref",
        label="Long-lived Access Token",
        kind="secret",
        placeholder="ha_...",
        description="Home Assistant → Profile → Long-Lived Access Tokens.",
    )


def _ha_url() -> GatewaySecretField:
    return GatewaySecretField(
        key="url",
        ref_key="url_ref",
        label="Home Assistant URL",
        kind="url",
        placeholder="https://homeassistant.local:8123",
        description="Base URL of your Home Assistant instance.",
    )


def _generic_webhook() -> GatewaySecretField:
    return GatewaySecretField(
        key="url",
        ref_key="url_ref",
        label="Webhook URL",
        kind="url",
        placeholder="https://...",
        description="Generic JSON webhook the bot will POST replies to.",
    )


def _status_webhook() -> GatewaySecretField:
    return GatewaySecretField(
        key="status_webhook_url",
        ref_key="status_webhook_url_ref",
        label="Status Webhook URL",
        kind="url",
        required=False,
        placeholder="https://...",
        description="Optional. Receives 'agent thinking', 'tool ran', etc. status pings.",
    )


_GATEWAY_PLATFORMS: tuple[GatewayPlatformSpec, ...] = (
    GatewayPlatformSpec(
        id="local",
        title="Local / Dashboard",
        alias_id="local",
        status="native",
        inbound="http",
        outbound="dashboard",
        typing="events",
        menu="dashboard",
        notes="Dashboard chat + local API.",
        support_level="full_duplex",
        docs_url="",
        secret_fields=(),
        setup_steps=(),
    ),
    GatewayPlatformSpec(
        id="telegram",
        title="Telegram",
        alias_id="telegram",
        status="native",
        inbound="polling",
        outbound="bot_api",
        typing="sendChatAction",
        menu="setMyCommands",
        attachments=True,
        voice=True,
        config_refs=("bot_token_ref", "chat_id"),
        support_level="tested",
        docs_url="https://core.telegram.org/bots/tutorial",
        secret_fields=(
            _bot_token(),
            GatewaySecretField(
                key="chat_id",
                ref_key="chat_id_ref",
                label="Chat ID",
                kind="id",
                placeholder="123456789 or -1001234567890",
                description="Numeric chat to bind. Use @userinfobot to find yours.",
            ),
            _status_webhook(),
        ),
        setup_steps=(
            "Open @BotFather on Telegram and send /newbot to obtain a bot token.",
            "Send /setprivacy and choose Disable so the bot sees group messages.",
            "Send /setcommands and paste the menu Nerya prints in /gateway.",
            "Open @userinfobot to discover your chat_id, paste both into the form below.",
        ),
    ),
    GatewayPlatformSpec(
        id="discord",
        title="Discord",
        alias_id="discord",
        status="webhook",
        inbound="generic_inbound",
        outbound="webhook",
        typing="status_webhook",
        menu="slash_commands_scaffold",
        attachments=True,
        config_refs=("webhook_url_ref", "webhook_url"),
        support_level="send_only",
        docs_url="https://discord.com/developers/docs/resources/webhook",
        secret_fields=(
            _webhook("Channel Webhook URL", description="From Discord → Channel → Edit → Integrations → Webhooks."),
            _status_webhook(),
        ),
        setup_steps=(
            "In Discord, open Server Settings → Integrations → Webhooks and click New Webhook.",
            "Pick a channel, copy the Webhook URL.",
            "Paste it into the Webhook URL field below and click Save.",
        ),
    ),
    GatewayPlatformSpec(
        id="whatsapp",
        title="WhatsApp",
        alias_id="whatsapp",
        status="scaffold",
        inbound="generic_inbound",
        outbound="webhook_or_bridge",
        typing="status_webhook",
        menu="commands_scaffold",
        attachments=True,
        voice=True,
        support_level="inbound_webhook",
        docs_url="https://developers.facebook.com/docs/whatsapp/cloud-api/get-started",
        secret_fields=(
            _whatsapp_token(),
            _whatsapp_phone_id(),
            _whatsapp_business_id(),
            _status_webhook(),
        ),
        setup_steps=(
            "Create a Meta for Developers app of type 'Business' and add the WhatsApp product.",
            "From WhatsApp → API Setup, copy the temporary access token + Phone Number ID.",
            "Configure the webhook URL pointing to /gateway/inbound on this Nerya instance.",
            "Subscribe to the messages event and paste the credentials below.",
        ),
    ),
    GatewayPlatformSpec(
        id="slack",
        title="Slack",
        alias_id="slack",
        status="webhook",
        inbound="generic_inbound",
        outbound="incoming_webhook",
        typing="status_webhook",
        menu="slash_commands_scaffold",
        attachments=True,
        config_refs=("webhook_url_ref", "webhook_url"),
        support_level="send_only",
        docs_url="https://api.slack.com/apps",
        secret_fields=(
            _slack_webhook(),
            _slack_bot_token(),
            _slack_signing(),
            _status_webhook(),
        ),
        setup_steps=(
            "Visit https://api.slack.com/apps and click Create New App → From scratch.",
            "Add Incoming Webhooks (for outbound) and copy the channel webhook URL.",
            "Optionally enable Event Subscriptions and point Request URL at /gateway/inbound.",
            "Add the bot's OAuth scopes: chat:write, app_mentions:read, channels:history.",
        ),
    ),
    GatewayPlatformSpec(
        id="signal",
        title="Signal",
        alias_id="signal",
        status="scaffold",
        inbound="generic_inbound",
        outbound="http_bridge",
        typing="status_webhook",
        menu="commands_scaffold",
        attachments=True,
        voice=True,
        support_level="inbound_webhook",
        docs_url="https://github.com/AsamK/signal-cli/wiki/HTTP-API",
        secret_fields=(
            _bridge_url("signal-cli HTTP API URL", description="Local signal-cli daemon HTTP endpoint."),
            _signal_phone(),
            _status_webhook(),
        ),
        setup_steps=(
            "Install signal-cli and register your phone number per its README.",
            "Run signal-cli daemon --http <port> to expose the HTTP API.",
            "Paste the daemon URL + your registered phone number below.",
        ),
    ),
    GatewayPlatformSpec(
        id="mattermost",
        title="Mattermost",
        alias_id="mattermost",
        status="webhook",
        inbound="generic_inbound",
        outbound="incoming_webhook",
        typing="status_webhook",
        menu="slash_commands_scaffold",
        attachments=True,
        support_level="send_only",
        docs_url="https://developers.mattermost.com/integrate/webhooks/incoming/",
        secret_fields=(
            _webhook("Incoming Webhook URL", description="From System Console → Integrations → Incoming Webhooks."),
            _status_webhook(),
        ),
        setup_steps=(
            "In Mattermost, open Integrations → Incoming Webhooks → Add Incoming Webhook.",
            "Pick the channel, copy the URL, paste below.",
        ),
    ),
    GatewayPlatformSpec(
        id="matrix",
        title="Matrix",
        alias_id="matrix",
        status="scaffold",
        inbound="generic_inbound",
        outbound="http_bridge",
        typing="status_webhook",
        menu="commands_scaffold",
        attachments=True,
        support_level="inbound_webhook",
        docs_url="https://spec.matrix.org/latest/client-server-api/",
        secret_fields=(
            _matrix_homeserver(),
            _matrix_token(),
            _matrix_user(),
            GatewaySecretField(
                key="room_id",
                ref_key="room_id_ref",
                label="Room ID",
                kind="id",
                required=False,
                placeholder="!abc:matrix.org",
                description="Optional default room. Otherwise inbound determines the room.",
            ),
            _status_webhook(),
        ),
        setup_steps=(
            "Create a Matrix account for the bot on your homeserver.",
            "Sign in once via Element to obtain a long-lived access token (Settings → Help & About).",
            "Paste the homeserver URL + access token below.",
        ),
    ),
    GatewayPlatformSpec(
        id="homeassistant",
        title="Home Assistant",
        alias_id="homeassistant",
        status="webhook",
        inbound="generic_inbound",
        outbound="webhook",
        typing="status_webhook",
        menu="service_scaffold",
        support_level="inbound_webhook",
        docs_url="https://www.home-assistant.io/docs/authentication/",
        secret_fields=(
            _ha_url(),
            _ha_token(),
            _status_webhook(),
        ),
        setup_steps=(
            "In Home Assistant, open your Profile → Long-Lived Access Tokens → Create.",
            "Paste the token + your HA URL below.",
        ),
    ),
    GatewayPlatformSpec(
        id="email",
        title="Email",
        alias_id="email",
        status="scaffold",
        inbound="generic_inbound",
        outbound="smtp_or_api",
        typing="none",
        menu="none",
        attachments=True,
        support_level="inbound_webhook",
        docs_url="https://datatracker.ietf.org/doc/html/rfc8314",
        secret_fields=(
            _email_smtp(),
            _email_imap(),
            _status_webhook(),
        ),
        setup_steps=(
            "Pick an account that supports SMTP+IMAP (Gmail App Password works).",
            "Build smtps://user:password@smtp.host:port — paste below.",
            "Optionally add an imaps://… URL to poll inbound mail.",
        ),
    ),
    GatewayPlatformSpec(
        id="sms",
        title="SMS (Twilio)",
        alias_id="sms",
        status="scaffold",
        inbound="generic_inbound",
        outbound="twilio_or_webhook",
        typing="none",
        menu="commands_scaffold",
        support_level="inbound_webhook",
        docs_url="https://www.twilio.com/docs/messaging/quickstart",
        secret_fields=(
            _sms_account_sid(),
            _sms_auth_token(),
            _sms_from(),
            _status_webhook(),
        ),
        setup_steps=(
            "Create a Twilio account and buy a phone number.",
            "From Console → Account info, copy Account SID + Auth Token.",
            "Configure the number's Messaging webhook → /gateway/inbound on this instance.",
        ),
    ),
    GatewayPlatformSpec(
        id="dingtalk",
        title="DingTalk",
        alias_id="dingtalk",
        status="webhook",
        inbound="generic_inbound",
        outbound="robot_webhook",
        typing="status_webhook",
        menu="commands_scaffold",
        attachments=True,
        support_level="send_only",
        docs_url="https://open.dingtalk.com/document/robots/custom-robot-access",
        secret_fields=(
            _dingtalk_robot(),
            _dingtalk_secret(),
            _status_webhook(),
        ),
        setup_steps=(
            "In a DingTalk group, open 群设置 → 智能群助手 → 添加机器人 → 自定义.",
            "Copy the Webhook URL; if 加签 is on, copy the SEC… secret too.",
        ),
    ),
    GatewayPlatformSpec(
        id="api_server",
        title="API Server",
        alias_id="api_server",
        status="native",
        inbound="http",
        outbound="http",
        typing="events",
        menu="openapi",
        notes="Universal /gateway/inbound + /gateway/send contract.",
        support_level="full_duplex",
        docs_url="https://docs.nerya.dev/gateway",
        secret_fields=(
            GatewaySecretField(
                key="auth_header",
                ref_key="auth_header_ref",
                label="Bearer Token",
                kind="secret",
                required=False,
                placeholder="Bearer eyJ...",
                description="Optional auth header upstream callers must send.",
            ),
            _status_webhook(),
        ),
        setup_steps=(
            "Point your service at POST /gateway/inbound with platform=api_server.",
            "Optional: set a Bearer token below to require auth on inbound.",
        ),
    ),
    GatewayPlatformSpec(
        id="webhook",
        title="Generic Webhook",
        alias_id="webhook",
        status="native",
        inbound="http",
        outbound="json_webhook",
        typing="status_webhook",
        menu="none",
        support_level="full_duplex",
        docs_url="https://docs.nerya.dev/gateway",
        secret_fields=(
            _generic_webhook(),
            _status_webhook(),
            GatewaySecretField(
                key="auth_header",
                ref_key="auth_header_ref",
                label="Auth Header",
                kind="secret",
                required=False,
                placeholder="Authorization: Bearer ...",
                description="Optional Authorization header forwarded with every send.",
            ),
        ),
        setup_steps=(
            "Build any HTTP service that accepts JSON {text, channel, …} POSTs.",
            "Paste its URL below.",
        ),
    ),
    GatewayPlatformSpec(
        id="feishu",
        title="Feishu (Lark)",
        alias_id="feishu",
        status="webhook",
        inbound="generic_inbound",
        outbound="bot_webhook",
        typing="status_webhook",
        menu="commands_scaffold",
        attachments=True,
        support_level="send_only",
        docs_url="https://open.feishu.cn/document/home/develop-a-bot-in-5-minutes/create-an-app",
        secret_fields=(
            _feishu_app_id(),
            _feishu_app_secret(),
            _feishu_verification(),
            _webhook(
                "Custom Bot Webhook URL",
                description="Optional 群机器人 inbound URL for outbound replies.",
                required=False,
            ),
            _status_webhook(),
        ),
        setup_steps=(
            "Open https://open.feishu.cn/app and create a 自建应用.",
            "From 凭证与基础信息 copy App ID + App Secret.",
            "Open 事件与回调 → 事件订阅, set Request URL to /gateway/inbound, copy Verification Token.",
            "Subscribe to im.message.receive_v1.",
        ),
    ),
    GatewayPlatformSpec(
        id="wecom",
        title="WeCom (企业微信)",
        alias_id="wecom",
        status="webhook",
        inbound="generic_inbound",
        outbound="bot_webhook",
        typing="status_webhook",
        menu="commands_scaffold",
        attachments=True,
        support_level="send_only",
        docs_url="https://developer.work.weixin.qq.com/document/path/90664",
        secret_fields=(
            _wecom_corp_id(),
            _wecom_agent_id(),
            _wecom_secret(),
            _wecom_robot_url(),
            _status_webhook(),
        ),
        setup_steps=(
            "Open WeCom Admin → 应用管理 → 自建 → 创建应用.",
            "Copy Corp ID, Agent ID, Secret.",
            "Optional: 群机器人 → 添加机器人 to get an outbound webhook URL.",
        ),
    ),
    GatewayPlatformSpec(
        id="wecom_callback",
        title="WeCom Callback",
        alias_id="wecom_callback",
        status="scaffold",
        inbound="generic_inbound",
        outbound="callback_api",
        typing="status_webhook",
        menu="commands_scaffold",
        attachments=True,
        support_level="inbound_webhook",
        docs_url="https://developer.work.weixin.qq.com/document/path/90930",
        secret_fields=(
            _wecom_corp_id(),
            _wecom_agent_id(),
            GatewaySecretField(
                key="callback_token",
                ref_key="callback_token_ref",
                label="Callback Token",
                kind="secret",
                placeholder="callback Token",
                description="From WeCom Admin → 应用 → 接收消息 → API 接收消息.",
            ),
            GatewaySecretField(
                key="encoding_aes_key",
                ref_key="encoding_aes_key_ref",
                label="Encoding AES Key",
                kind="secret",
                placeholder="43-char EncodingAESKey",
                description="From WeCom Admin → 应用 → 接收消息 → API 接收消息.",
            ),
            _status_webhook(),
        ),
        setup_steps=(
            "In WeCom Admin → 应用 → 接收消息, configure URL to /gateway/inbound.",
            "Copy the auto-generated Token + EncodingAESKey, paste below.",
        ),
    ),
    GatewayPlatformSpec(
        id="weixin",
        title="Weixin (微信公众号)",
        alias_id="weixin",
        status="scaffold",
        inbound="generic_inbound",
        outbound="official_account_api",
        typing="status_webhook",
        menu="commands_scaffold",
        attachments=True,
        voice=True,
        support_level="inbound_webhook",
        docs_url="https://developers.weixin.qq.com/doc/offiaccount/Basic_Information/Get_the_access_token.html",
        secret_fields=(
            GatewaySecretField(
                key="app_id",
                ref_key="app_id_ref",
                label="AppID",
                kind="opaque",
                placeholder="wx...",
                description="From 微信公众平台 → 设置与开发 → 基本配置.",
            ),
            GatewaySecretField(
                key="app_secret",
                ref_key="app_secret_ref",
                label="AppSecret",
                kind="secret",
                description="From 微信公众平台 → 设置与开发 → 基本配置.",
            ),
            GatewaySecretField(
                key="callback_token",
                ref_key="callback_token_ref",
                label="Token",
                kind="secret",
                placeholder="服务器配置 Token",
                description="From 设置与开发 → 服务器配置.",
            ),
            GatewaySecretField(
                key="encoding_aes_key",
                ref_key="encoding_aes_key_ref",
                label="EncodingAESKey",
                kind="secret",
                required=False,
                description="Required when 安全模式 is on.",
            ),
            _status_webhook(),
        ),
        setup_steps=(
            "On 微信公众平台 → 设置与开发 → 基本配置, copy AppID + AppSecret.",
            "Open 服务器配置, set URL to /gateway/inbound, generate Token.",
            "If 安全模式, also copy EncodingAESKey.",
        ),
    ),
    GatewayPlatformSpec(
        id="bluebubbles",
        title="BlueBubbles (iMessage)",
        alias_id="bluebubbles",
        status="scaffold",
        inbound="generic_inbound",
        outbound="http_bridge",
        typing="status_webhook",
        menu="none",
        attachments=True,
        support_level="inbound_webhook",
        docs_url="https://docs.bluebubbles.app/server/installation",
        secret_fields=(
            _bluebubbles_url(),
            _bluebubbles_password(),
            _status_webhook(),
        ),
        setup_steps=(
            "Install BlueBubbles Server on a Mac signed in to iMessage.",
            "Set the server URL + password (use Tailscale/Ngrok for remote access).",
        ),
    ),
    GatewayPlatformSpec(
        id="qqbot",
        title="QQ Bot",
        alias_id="qqbot",
        status="scaffold",
        inbound="generic_inbound",
        outbound="qqbot_api",
        typing="status_webhook",
        menu="commands_scaffold",
        attachments=True,
        support_level="inbound_webhook",
        docs_url="https://bot.q.qq.com/wiki/develop/api/",
        secret_fields=(
            _qq_bot_app_id(),
            _qq_bot_app_secret(),
            _status_webhook(),
        ),
        setup_steps=(
            "Visit https://q.qq.com → 沙箱应用 to register the bot.",
            "Copy AppID + AppSecret, configure 回调地址 to /gateway/inbound.",
        ),
    ),
)

PLATFORM_IDS: tuple[str, ...] = tuple(p.id for p in _GATEWAY_PLATFORMS)


def list_platforms() -> list[dict[str, Any]]:
    return [p.asdict() for p in _GATEWAY_PLATFORMS]


def get_platform(platform_id: str) -> GatewayPlatformSpec | None:
    key = str(platform_id or "").strip().lower()
    for spec in _GATEWAY_PLATFORMS:
        if key in {spec.id, spec.alias_id}:
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


def all_secret_field_keys() -> dict[str, str]:
    """Map every secret/url ``key`` → its ``ref_key`` across the catalog.

    Used by the gateway routes when persisting an upsert payload — anything
    in this map is vaulted automatically. ``id`` / ``opaque`` fields stay
    in plaintext YAML (e.g. ``chat_id``, ``app_id``, ``corp_id``) because
    they are public identifiers, not credentials.
    """
    mapping: dict[str, str] = {}
    for spec in _GATEWAY_PLATFORMS:
        for f in spec.secret_fields:
            if f.kind in {"secret", "url"}:
                mapping[f.key] = f.ref_key
    # Legacy aliases still accepted by /gateway/config/upsert.
    mapping.setdefault("token", "token_ref")
    return mapping


def all_safe_field_keys() -> set[str]:
    """Public (non-secret) channel keys the dashboard form may set."""
    keys = {
        "enabled",
        "disabled",
        "chat_id",
        "mode",
        "polling",
        "trade_notifications",
        "approvals",
        "topics",
        "parse_mode",
        "markdown",
        "disable_web_page_preview",
        "username",
        "avatar_url",
        "timeout_s",
        "label",
        "description",
        "auto_reply",
        "allow_unknown_users",
        "allowed_chat_ids",
        "allowed_user_ids",
        "allowed_users",
        "denied_user_ids",
        "group_sessions_per_user",
        "thread_sessions_per_user",
    }
    for spec in _GATEWAY_PLATFORMS:
        for f in spec.secret_fields:
            if f.kind in {"id", "opaque"}:
                keys.add(f.key)
    return keys
