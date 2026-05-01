# Gateway platforms

Nerya now keeps a Hermes-aligned gateway platform catalog in
`nerya/messaging/platforms.py` and exposes it through:

```powershell
Invoke-RestMethod http://127.0.0.1:18317/gateway/platforms
```

Tracked Hermes platform ids:

- `local`
- `telegram`
- `discord`
- `whatsapp`
- `slack`
- `signal`
- `mattermost`
- `matrix`
- `homeassistant`
- `email`
- `sms`
- `dingtalk`
- `api_server`
- `webhook`
- `feishu`
- `wecom`
- `wecom_callback`
- `weixin`
- `bluebubbles`
- `qqbot`

## Universal inbound contract

Any platform adapter can normalize an incoming message into:

```json
{
  "platform": "slack",
  "chat_id": "C123",
  "user_id": "U123",
  "text": "review my strategy",
  "session_id": "optional-stable-session-id"
}
```

and POST it to:

```text
POST /gateway/inbound
```

The response includes `reply_text`, `turn_id`, `events`, `trace_text`, and
`plan`.  `events` is the user-visible decision trail: plan, subagents, think,
act, observe, close.

## Universal outbound contract

Use:

```text
POST /gateway/send
```

with `platform`/`channel` and `text`.  Native senders are used where present
(Telegram, Discord webhook, generic webhook). Other Hermes platform ids route
through `generic_platform.send`, which supports webhook-style config and records
outbox fallback when credentials are absent.

## Startup platform sync

When the API starts, Nerya scans `messages/channels.yml` for configured gateway
channels. Telegram channels with `bot_token_ref` or `token_ref` automatically
receive their Bot API menu via `setMyCommands`; operators do not need to run a
separate setup script. The menu uses `gateway_menu_commands()` in
`nerya/api/routes_gateway.py`, the same command registry that powers `/help` and
`/menu`, so the visible Telegram menu stays aligned with Hermes-style gateway
commands.

## In-progress state

Telegram keeps `sendChatAction=typing` alive until the agent turn completes.
Other platforms use native typing/status when implemented; otherwise configure a
`status_webhook_url` or `status_webhook_url_ref` in `messages/channels.yml` and
Nerya will emit gateway status events during the turn.

## Developing new platform adapters

Use the built-in `capability_developer` skill:

```text
capability_developer.list_lanes
capability_developer.propose_gateway_platform
```

The skill includes the platform-development reference at
`nerya/skills/builtin/capability_developer_skill/references/gateway-platform-development.md`.
All adapter work is emitted as an evolution proposal and is not applied without
operator review.
