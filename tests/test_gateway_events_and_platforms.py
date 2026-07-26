"""Tests for gateway live-events ring buffer + per-platform configured check.

The companion bug from the operator: configuring a Slack/Feishu/WeCom channel
silently looked "configured" because the legacy
``_gateway_channel_configured`` only inspected webhook fields, but the runtime
couldn't actually carry a session because the platform-specific auth
fields (``signing_secret``, ``app_id``, ``app_secret``, ``verification_token``,
``corp_id``, ``agent_id``…) weren't populated. After this refactor, the
configured-check is driven by ``GatewayPlatformSpec.secret_fields`` so the
"ready" pill on the dashboard tells the truth.

The live-events buffer fixes the second half of the complaint: after this
refactor the dashboard can subscribe to a platform-agnostic
``GET /gateway/events`` cursor stream, and Hermes-style "agent is thinking →
acting → observing → done" status events fan out for every gateway turn.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from nerya.api import routes_gateway
from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.messaging.platforms import (
    PLATFORM_IDS,
    get_platform,
    list_platforms,
    require_platform,
)
from nerya.messaging.mirror import GatewayMirror


pytestmark = pytest.mark.smoke


def _client(tmp_path):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})
    return SimpleNamespace(config=cfg, skills=SimpleNamespace())


def _route_map():
    return {(method, path): handler for method, path, handler in routes_gateway.routes()}


# ----------------------------- platform catalog --------------------------- #


def test_every_platform_carries_docs_url_and_secret_fields_or_is_local():
    rows = list_platforms()
    by_id = {row["id"]: row for row in rows}
    # local, api_server, webhook are universal contracts — secret_fields may
    # be empty for local. Everything else must declare at least one
    # required secret field so the dashboard can render the form.
    optional_no_secrets = {"local"}
    for spec_id, row in by_id.items():
        assert "docs_url" in row, f"{spec_id} missing docs_url"
        assert "secret_fields" in row, f"{spec_id} missing secret_fields list"
        if spec_id in optional_no_secrets:
            continue
        required = [f for f in row["secret_fields"] if f["required"]]
        if spec_id in {"api_server", "webhook"}:
            # Generic wrappers — required field is the URL-or-bearer pair.
            continue
        assert required, f"{spec_id}: every platform should declare at least one required field"


def test_telegram_required_fields_are_bot_token_and_chat_id():
    spec = require_platform("telegram")
    required_keys = spec.required_field_keys()
    assert "bot_token" in required_keys
    assert "chat_id" in required_keys


def test_feishu_required_fields_match_open_platform_credentials():
    spec = require_platform("feishu")
    required_keys = set(spec.required_field_keys())
    assert {"app_id", "app_secret"}.issubset(required_keys)


def test_wecom_required_fields_match_corp_secret():
    spec = require_platform("wecom")
    required_keys = set(spec.required_field_keys())
    assert {"corp_id", "agent_id", "app_secret"}.issubset(required_keys)


def test_slack_only_requires_incoming_webhook_url():
    spec = require_platform("slack")
    required_keys = set(spec.required_field_keys())
    assert "incoming_webhook_url" in required_keys
    # signing_secret + bot_token are documented but NOT required (Slack send
    # only needs the incoming webhook for outbound).
    assert "signing_secret" not in required_keys
    assert "bot_token" not in required_keys


def test_secret_fields_exposed_via_platforms_route(tmp_path):
    client = _client(tmp_path)
    routes = _route_map()
    rows = routes[("GET", "/gateway/platforms")](client, {})["platforms"]
    by_id = {row["id"]: row for row in rows}
    feishu = by_id["feishu"]
    field_keys = [f["key"] for f in feishu["secret_fields"]]
    assert "app_id" in field_keys
    assert "app_secret" in field_keys
    assert "verification_token" in field_keys
    # Docs URL points at Feishu Open Platform.
    assert "feishu" in feishu["docs_url"].lower() or "lark" in feishu["docs_url"].lower()


# ----------------------------- configured checks ------------------------- #


def test_configured_check_returns_true_only_when_all_required_fields_set(tmp_path):
    client = _client(tmp_path)
    # An empty Feishu channel must NOT mark configured because both app_id
    # and app_secret are required.
    yaml_io.dump(
        client.config.paths.messages_channels,
        {"channels": {"feishu_ops": {"kind": "feishu"}}},
    )
    snap = routes_gateway.gateway_runtime_status(client)
    rows = {row["channel"]: row for row in snap["gateways"]["channels"]}
    assert rows["feishu_ops"]["configured"] is False

    # Setting only app_id is still incomplete.
    yaml_io.dump(
        client.config.paths.messages_channels,
        {"channels": {"feishu_ops": {"kind": "feishu", "app_id": "cli_xxx"}}},
    )
    snap = routes_gateway.gateway_runtime_status(client)
    rows = {row["channel"]: row for row in snap["gateways"]["channels"]}
    assert rows["feishu_ops"]["configured"] is False

    # Setting both required fields → configured.
    yaml_io.dump(
        client.config.paths.messages_channels,
        {"channels": {"feishu_ops": {
            "kind": "feishu",
            "app_id": "cli_xxx",
            "app_secret_ref": "vault://feishu_app_secret",
        }}},
    )
    snap = routes_gateway.gateway_runtime_status(client)
    rows = {row["channel"]: row for row in snap["gateways"]["channels"]}
    assert rows["feishu_ops"]["configured"] is True


def test_configured_check_telegram_still_requires_bot_token_and_chat_id(tmp_path):
    client = _client(tmp_path)
    yaml_io.dump(
        client.config.paths.messages_channels,
        {"channels": {"telegram": {"kind": "telegram", "bot_token_ref": "vault://x"}}},
    )
    rows = {row["channel"]: row
            for row in routes_gateway.gateway_runtime_status(client)["gateways"]["channels"]}
    assert rows["telegram"]["configured"] is False

    yaml_io.dump(
        client.config.paths.messages_channels,
        {"channels": {"telegram": {
            "kind": "telegram",
            "bot_token_ref": "vault://x",
            "chat_id": "12345",
        }}},
    )
    rows = {row["channel"]: row
            for row in routes_gateway.gateway_runtime_status(client)["gateways"]["channels"]}
    assert rows["telegram"]["configured"] is True


def test_configured_check_unknown_platform_falls_back_loosely(tmp_path):
    client = _client(tmp_path)
    yaml_io.dump(
        client.config.paths.messages_channels,
        {"channels": {"customwave": {
            "kind": "customwave",
            "webhook_url_ref": "vault://customwave_url",
        }}},
    )
    snap = routes_gateway.gateway_runtime_status(client)
    rows = {row["channel"]: row for row in snap["gateways"]["channels"]}
    # Unknown but has a webhook ref → loose fallback says configured.
    assert rows["customwave"]["configured"] is True


def test_every_known_platform_has_a_configured_check_or_no_fields():
    # Sanity: get_platform must return a spec for every advertised id.
    for pid in PLATFORM_IDS:
        spec = get_platform(pid)
        assert spec is not None, pid


# ----------------------------- live events ------------------------------- #


def test_gateway_events_endpoint_returns_empty_when_no_activity(tmp_path):
    routes_gateway.reset_gateway_events_for_tests()
    client = _client(tmp_path)
    routes = _route_map()
    res = routes[("GET", "/gateway/events")](client, {})
    assert res["ok"] is True
    assert res["events"] == []
    assert res["cursor"] == 0


def test_gateway_events_buffer_records_inbound_and_outbound(tmp_path, monkeypatch):
    routes_gateway.reset_gateway_events_for_tests()
    client = _client(tmp_path)
    routes = _route_map()

    class _Hooks:
        def register(self, _phase, _handler) -> None:
            return None

    class _FakeKernel:
        def __init__(self, *, config, skills):
            self.hooks = _Hooks()

        def run_turn(self, *, trigger, session_id):
            return SimpleNamespace(
                turn_id="turn-1",
                final_text="reply text",
                decision={},
                tool_trace=[],
                actions=[],
                blocks=[],
            )

    monkeypatch.setattr(routes_gateway, "AgentKernel", _FakeKernel)
    yaml_io.dump(
        client.config.paths.messages_channels,
        {"channels": {"discord_ops": {
            "kind": "discord",
            "webhook_url_ref": "vault://discord_url",
            "auto_reply": False,
        }}},
    )

    routes[("POST", "/gateway/inbound")](
        client,
        {
            "platform": "discord",
            "channel": "discord_ops",
            "chat_id": "ch-1",
            "user_id": "u1",
            "text": "/portfolio",
        },
    )
    res = routes[("GET", "/gateway/events")](client, {})
    kinds = [row["kind"] for row in res["events"]]
    assert "inbound" in kinds
    assert "outbound" in kinds
    # Cursor advances; second call with since=cursor returns nothing new.
    cursor = res["cursor"]
    assert cursor > 0
    res2 = routes[("GET", "/gateway/events")](client, {"since": cursor})
    assert res2["events"] == []
    assert res2["cursor"] == cursor


def test_gateway_events_buffer_filters_by_channel(tmp_path, monkeypatch):
    routes_gateway.reset_gateway_events_for_tests()
    client = _client(tmp_path)
    routes = _route_map()

    class _Hooks:
        def register(self, _phase, _handler) -> None:
            return None

    class _FakeKernel:
        def __init__(self, *, config, skills):
            self.hooks = _Hooks()

        def run_turn(self, *, trigger, session_id):
            return SimpleNamespace(
                turn_id="t",
                final_text="r",
                decision={},
                tool_trace=[],
                actions=[],
                blocks=[],
            )

    monkeypatch.setattr(routes_gateway, "AgentKernel", _FakeKernel)
    yaml_io.dump(
        client.config.paths.messages_channels,
        {"channels": {
            "discord_ops": {
                "kind": "discord",
                "webhook_url_ref": "vault://discord_url",
                "auto_reply": False,
            },
            "feishu_ops": {
                "kind": "feishu",
                "app_id": "cli_xxx",
                "app_secret_ref": "vault://feishu_secret",
                "auto_reply": False,
            },
        }},
    )
    routes[("POST", "/gateway/inbound")](
        client,
        {"platform": "discord", "channel": "discord_ops", "chat_id": "c", "user_id": "u", "text": "hi"},
    )
    routes[("POST", "/gateway/inbound")](
        client,
        {"platform": "feishu", "channel": "feishu_ops", "chat_id": "c2", "user_id": "u2", "text": "hi"},
    )
    discord_only = routes[("GET", "/gateway/events")](client, {"channel": "discord_ops"})
    assert all(row["channel"] == "discord_ops" for row in discord_only["events"])
    feishu_only = routes[("GET", "/gateway/events")](client, {"platform": "feishu"})
    assert all(row["platform"] == "feishu" for row in feishu_only["events"])


# ----------------------------- SSE streaming ------------------------------ #


def test_format_sse_emits_id_event_and_data_lines():
    frame = routes_gateway._format_sse({
        "seq": 7,
        "kind": "phase",
        "channel": "telegram",
        "phase": "thinking",
    })
    text = frame.decode("utf-8")
    # Order is required by the EventSource spec: id, event, data, blank.
    lines = text.split("\n")
    assert lines[0] == "id: 7"
    assert lines[1] == "event: phase"
    assert lines[2].startswith("data: ")
    payload = lines[2][len("data: "):]
    import json as _json
    body = _json.loads(payload)
    assert body["seq"] == 7
    assert body["kind"] == "phase"
    assert body["phase"] == "thinking"
    # Trailing blank line(s) terminate the SSE event.
    assert text.endswith("\n\n")


def test_gateway_events_stream_handler_returns_streaming_response_and_replays_buffer(tmp_path):
    routes_gateway.reset_gateway_events_for_tests()
    routes_gateway._gateway_events_record({
        "kind": "inbound", "platform": "telegram", "channel": "telegram",
        "chat_id": "c", "user_id": "u", "text": "hello",
    })
    routes_gateway._gateway_events_record({
        "kind": "outbound", "platform": "telegram", "channel": "telegram",
        "chat_id": "c", "text": "hi back",
    })
    # The handler returns a StreamingResponse marker. Iterate the
    # generator just enough to see the buffered replay (the loop is
    # otherwise a 30-minute long-poll; we never enter it).
    from nerya.api.local_server import StreamingResponse

    client = _client(tmp_path)
    routes = _route_map()
    handler = routes[("GET", "/gateway/events/stream")]
    resp = handler(client, {"since": 0})
    assert isinstance(resp, StreamingResponse)
    assert resp.content_type == "text/event-stream"
    gen = resp.generator
    # Drain frames until the ``: ready\n\n`` sentinel marks end-of-replay.
    seen = []
    for chunk in gen:
        if chunk == b": ready\n\n":
            break
        seen.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
        if len(seen) > 16:  # safety
            break
    gen.close()  # type: ignore[attr-defined]
    text = b"".join(seen).decode("utf-8")
    assert "event: inbound" in text
    assert "event: outbound" in text
    assert "id: 1" in text and "id: 2" in text


def test_gateway_events_stream_route_registered():
    routes = _route_map()
    assert ("GET", "/gateway/events/stream") in routes


# ----------------------------- visibility helpers --------------------------- #


def test_record_gateway_error_appends_an_error_event_with_hint(tmp_path):
    """Operator-actionable error events are how the dashboard surfaces
    silent drops. Without this, a chat_id mismatch or invalid bot_token
    just looks like 'nothing is happening' from the operator's seat.
    """
    routes_gateway.reset_gateway_events_for_tests()
    routes_gateway._record_gateway_error(
        platform="telegram",
        channel="telegram",
        reason="chat_not_allowed",
        chat_id="999",
        detail="received 999, configured 12345",
        hint="Use @userinfobot to discover the right chat_id.",
    )
    routes = _route_map()
    res = routes[("GET", "/gateway/events")](_client(tmp_path), {})
    rows = res["events"]
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "error"
    assert row["reason"] == "chat_not_allowed"
    assert row["chat_id"] == "999"
    assert "userinfobot" in row["hint"]


def test_record_gateway_heartbeat_throttles_to_30_seconds(monkeypatch):
    """Heartbeats prove "the poller is alive" without flooding the
    event ring. We throttle to one per ~30 s per channel — without the
    throttle a 1-second poll loop would push 86,400 heartbeats/day per
    bot, drowning out every interesting event.
    """
    routes_gateway.reset_gateway_events_for_tests()
    fake_now = [1000.0]
    monkeypatch.setattr(
        routes_gateway.time, "time", lambda: fake_now[0]
    )
    first = routes_gateway._record_gateway_heartbeat(platform="telegram", channel="telegram")
    assert first is not None
    assert first["kind"] == "heartbeat"
    fake_now[0] += 5.0  # 5 s later — throttled
    again = routes_gateway._record_gateway_heartbeat(platform="telegram", channel="telegram")
    assert again is None
    fake_now[0] += 31.0  # 36 s after first — emits
    third = routes_gateway._record_gateway_heartbeat(platform="telegram", channel="telegram")
    assert third is not None


def test_telegram_diagnose_route_registered():
    routes = _route_map()
    assert ("POST", "/gateway/telegram/diagnose") in routes


def test_telegram_diagnose_reports_missing_token(tmp_path):
    """When operator hasn't saved a bot_token yet, diagnose returns a
    crisp 'missing_token' verdict instead of trying to call Telegram.
    """
    client = _client(tmp_path)
    yaml_io.dump(
        client.config.paths.messages_channels,
        {"channels": {"telegram": {"kind": "telegram"}}},
    )
    routes = _route_map()
    res = routes[("POST", "/gateway/telegram/diagnose")](client, {"channel": "telegram"})
    assert res["ok"] is False
    assert "bot_token" in res.get("error", "")
    assert res["configured"]["bot_token_ref"] is False


def test_run_gateway_turn_records_phase_events_for_non_telegram_platform(tmp_path, monkeypatch):
    """The user wants typing/phase animations to land for every gateway,
    not only Telegram. Confirm that running a turn through Discord (which
    Hermes treats as a peer of Telegram) actually surfaces phase events
    in the live ring buffer so the dashboard can animate "thinking → tool
    → reply" for every platform.
    """
    routes_gateway.reset_gateway_events_for_tests()
    client = _client(tmp_path)
    routes = _route_map()

    class _Hooks:
        def __init__(self):
            self._handlers: list = []

        def register(self, _phase, handler) -> None:
            self._handlers.append(handler)

        def fire(self, ctx) -> None:
            for h in self._handlers:
                h(ctx)

    class _FakeKernel:
        def __init__(self, *, config, skills):
            self.hooks = _Hooks()

        def run_turn(self, *, trigger, session_id):
            # Imitate kernel emitting phase events through hooks.
            from types import SimpleNamespace as _NS
            self.hooks.fire(_NS(phase="after_think", data={}, iteration=1))
            self.hooks.fire(_NS(phase="after_act", data={}, iteration=1))
            return _NS(
                turn_id="t-abc",
                final_text="reply text",
                decision={},
                tool_trace=[],
                actions=[],
                blocks=[],
            )

    monkeypatch.setattr(routes_gateway, "AgentKernel", _FakeKernel)
    yaml_io.dump(
        client.config.paths.messages_channels,
        {"channels": {"discord_ops": {
            "kind": "discord",
            "webhook_url_ref": "vault://discord_url",
            "auto_reply": False,
        }}},
    )
    routes[("POST", "/gateway/inbound")](
        client,
        {
            "platform": "discord",
            "channel": "discord_ops",
            "chat_id": "ch-1",
            "user_id": "u1",
            "text": "hello",
        },
    )
    res = routes[("GET", "/gateway/events")](client, {})
    kinds = [row["kind"] for row in res["events"]]
    assert "inbound" in kinds
    assert "phase" in kinds
    assert "outbound" in kinds
    phase_rows = [row for row in res["events"] if row["kind"] == "phase"]
    assert {row.get("platform") for row in phase_rows} == {"discord"}
    assert {row.get("channel") for row in phase_rows} == {"discord_ops"}
    # Phase event surfaces operator-friendly text.
    for row in phase_rows:
        assert row.get("text"), "phase events must carry hermes-style status text"


def test_trade_notifications_count_feishu_with_app_credentials(tmp_path):
    """Spec-aware fallback in trade-notification fan-out — a Feishu
    channel with app_id+app_secret (the real auth) should be eligible
    for trade fan-out even when no group-bot webhook URL is set.
    """
    from nerya.core.config import Config as _Config
    from nerya.core.paths import WorkspacePaths as _Paths
    from nerya.messaging import trade_notifications

    cfg = _Config(paths=_Paths(root=tmp_path), data={})
    yaml_io.dump(
        cfg.paths.messages_channels,
        {"channels": {"feishu_trade": {
            "kind": "feishu",
            "app_id": "cli_xxx",
            "app_secret_ref": "vault://feishu_secret",
        }}},
    )
    channels = trade_notifications._trade_notification_channels(cfg)
    names = {ch for ch, _ in channels}
    assert "feishu_trade" in names


# ---------------------- first-poll backlog drain -------------------------- #


def _write_telegram_channel(client, *, chat_id: str = "100") -> None:
    yaml_io.dump(
        client.config.paths.messages_channels,
        {"channels": {"telegram": {
            "kind": "telegram",
            "bot_token_ref": "vault://tg_bot_token",
            "chat_id": chat_id,
            "auto_reply": False,
        }}},
    )


def test_first_poll_drains_backlog_and_only_dispatches_latest(tmp_path, monkeypatch):
    """The operator's exact ask: when the poller comes back online after
    a stretch of being disabled, Telegram replays every queued message.
    Without a drain we would ``_handle_text`` each one in turn — burning
    rate-limit and answering a stale conversation. The first tick should
    keep only the last update, advance the offset past everything, and
    record an info/backlog_drained event so the dashboard explains it.
    """
    routes_gateway.reset_gateway_events_for_tests()
    # Make sure the channel is marked as "first-poll pending" the way
    # ``launch_telegram_pollers`` would.
    with routes_gateway._TELEGRAM_FIRST_POLL_LOCK:
        routes_gateway._TELEGRAM_FIRST_POLL_PENDING.clear()
        routes_gateway._TELEGRAM_FIRST_POLL_PENDING.add("telegram")

    client = _client(tmp_path)
    _write_telegram_channel(client, chat_id="100")

    queued_updates = [
        {"update_id": 11, "message": {"chat": {"id": 100}, "from": {"id": 7},
                                      "text": "stale 1"}},
        {"update_id": 12, "message": {"chat": {"id": 100}, "from": {"id": 7},
                                      "text": "stale 2"}},
        {"update_id": 13, "message": {"chat": {"id": 100}, "from": {"id": 7},
                                      "text": "latest please reply"}},
    ]

    monkeypatch.setattr(
        routes_gateway.telegram, "get_updates",
        lambda **_kw: {"ok": True, "updates": queued_updates},
    )
    handled: list[str] = []

    def _fake_handle_text(client, cfg, chat_id, text, update_id, identity):
        handled.append(text)
        return {"ok": True, "chat_id": chat_id}

    monkeypatch.setattr(routes_gateway, "_handle_text", _fake_handle_text)

    result = routes_gateway._telegram_poll_tick(client, "telegram")

    assert result["ok"] is True
    assert handled == ["latest please reply"], (
        f"only the newest message should be dispatched on the first tick, got {handled}"
    )
    # Offset advanced past every queued update so Telegram never replays them.
    assert result["offset"] == 14

    # Ring buffer carries an info/backlog_drained event for the operator.
    drain_events = [
        ev for ev in routes_gateway._gateway_events_snapshot(limit=50)
        if ev.get("kind") == "info" and ev.get("reason") == "backlog_drained"
    ]
    assert drain_events, "must surface a backlog_drained info event"
    assert drain_events[0]["drained_count"] == 2
    assert drain_events[0]["channel"] == "telegram"


def test_telegram_poll_downloads_photo_only_message_for_agent(tmp_path, monkeypatch):
    routes_gateway.reset_gateway_events_for_tests()
    with routes_gateway._TELEGRAM_FIRST_POLL_LOCK:
        routes_gateway._TELEGRAM_FIRST_POLL_PENDING.clear()

    client = _client(tmp_path)
    _write_telegram_channel(client, chat_id="100")
    queued_updates = [
        {
            "update_id": 41,
            "message": {
                "message_id": 5,
                "chat": {"id": 100},
                "from": {"id": 7, "username": "alice"},
                "photo": [
                    {"file_id": "small-photo", "file_size": 12},
                    {"file_id": "large-photo", "file_size": 16},
                ],
            },
        }
    ]
    monkeypatch.setattr(
        routes_gateway.telegram, "get_updates",
        lambda **_kw: {"ok": True, "updates": queued_updates},
    )
    monkeypatch.setattr(
        routes_gateway.telegram,
        "download_inbound_file",
        lambda **_kw: {
            "ok": True,
            "file_path": "photos/file_1.jpg",
            "file_size": 16,
            "data": base64.b64encode(b"photo-bytes").decode("ascii"),
            "content_type": "image/jpeg",
        },
    )
    handled: list[dict[str, object]] = []

    def _fake_handle_text(client, cfg, chat_id, text, update_id, identity, attachments=None):  # noqa: ANN001
        handled.append({
            "text": text,
            "attachments": attachments or [],
            "chat_id": chat_id,
            "update_id": update_id,
        })
        return {"ok": True, "chat_id": chat_id}

    monkeypatch.setattr(routes_gateway, "_handle_text", _fake_handle_text)

    result = routes_gateway._telegram_poll_tick(client, "telegram")

    assert result["ok"] is True
    assert handled and handled[0]["text"] == "Please review the attached file(s)."
    attachments = handled[0]["attachments"]
    assert isinstance(attachments, list)
    assert attachments[0]["kind"] == "image"
    assert attachments[0]["artifact_uri"].startswith("nerya://artifact/attachments/uploads/")
    assert "data" not in attachments[0]
    assert (tmp_path / "artifacts" / "attachments").exists()


def test_telegram_poll_route_runs_shared_turn_pipeline(tmp_path, monkeypatch):
    routes_gateway.reset_gateway_events_for_tests()
    routes_gateway.get_default_buffer().clear()
    with routes_gateway._TELEGRAM_FIRST_POLL_LOCK:
        routes_gateway._TELEGRAM_FIRST_POLL_PENDING.clear()

    client = _client(tmp_path)
    _write_telegram_channel(client, chat_id="100")
    secret = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
    calls: list[dict[str, object]] = []
    sent: list[dict[str, object]] = []
    updates = [{
        "update_id": 51,
        "message": {
            "chat": {"id": 100, "type": "group"},
            "from": {"id": 7, "username": "alice"},
            "text": f"use {secret}",
        },
    }]

    class _Hooks:
        def register(self, _phase, _handler) -> None:
            return None

    class _FakeKernel:
        def __init__(self, *, config, skills):
            self.hooks = _Hooks()

        def run_turn(self, *, trigger, session_id):
            calls.append({"trigger": trigger, "session_id": session_id})
            return SimpleNamespace(
                turn_id="turn-telegram",
                final_text="shared pipeline reply",
                decision={},
                tool_trace=[],
                actions=[],
                blocks=[{
                    "role": "assistant",
                    "block": {"kind": "thinking", "text": "checked request"},
                }],
            )

    def _fake_send(outbox_messages, message, **_kwargs):
        message["delivered"] = True
        message["status"] = 200
        sent.append(dict(message))
        return outbox_messages / "telegram-test.json"

    monkeypatch.setattr(routes_gateway, "AgentKernel", _FakeKernel)
    monkeypatch.setattr(
        routes_gateway.telegram,
        "get_updates",
        lambda **_kwargs: {"ok": True, "updates": updates},
    )
    monkeypatch.setattr(routes_gateway.telegram, "send", _fake_send)
    monkeypatch.setattr(
        routes_gateway.telegram,
        "send_chat_action",
        lambda **_kwargs: {"ok": True},
    )

    result = _route_map()[("POST", "/gateway/telegram/poll")](
        client, {"channel": "telegram"}
    )

    turn = result["processed"][0]
    assert turn["turn_id"] == "turn-telegram"
    assert turn["session_key"] == "telegram:100:user:7"
    assert turn["delivery"]["delivered"] is True
    assert len(turn["secrets_captured"]) == 1
    assert [row["text"] for row in sent] == [
        turn["captured_notice"],
        turn["trace_text"],
        "shared pipeline reply",
    ]
    assert all(secret not in str(row) for row in sent)

    trigger = calls[0]["trigger"]
    assert trigger["source"] == "telegram"
    assert trigger["payload"]["text"].startswith("use <<NERYA_SECRET:")
    assert trigger["payload"]["user_id"] == "7"

    mirror = GatewayMirror(client.config.paths).replay(channel="telegram")
    assert [entry.direction for entry in mirror] == ["in", "out"]
    assert mirror[0].payload["update_id"] == 51
    assert mirror[1].payload["delivery"]["delivered"] is True

    state = routes_gateway._load_state(client)
    assert state["active_sessions"][turn["session_key"]] == turn["session_id"]
    events = _route_map()[("GET", "/gateway/events")](client, {})["events"]
    inbound = next(row for row in events if row["kind"] == "inbound")
    outbound = next(row for row in events if row["kind"] == "outbound")
    assert inbound["update_id"] == 51
    assert outbound["delivered"] is True

    updates[:] = [{
        "update_id": 52,
        "message": {
            "chat": {"id": 100, "type": "group"},
            "from": {"id": 7, "username": "alice"},
            "text": "/help",
        },
    }]
    command_result = _route_map()[("POST", "/gateway/telegram/poll")](
        client, {"channel": "telegram"}
    )["processed"][0]
    assert command_result["command"] == "/help"
    assert command_result["delivery"]["delivered"] is True
    assert sent[-1]["text"] == command_result["reply_text"]
    assert len(calls) == 1


def test_second_poll_processes_every_update_normally(tmp_path, monkeypatch):
    """The drain is one-shot. After the first tick consumes the pending
    flag, the poller goes back to dispatching every update so live
    sessions are not silently truncated when the user types fast.
    """
    routes_gateway.reset_gateway_events_for_tests()
    with routes_gateway._TELEGRAM_FIRST_POLL_LOCK:
        routes_gateway._TELEGRAM_FIRST_POLL_PENDING.clear()
        # Note: NOT adding "telegram" — simulating "first tick already happened".

    client = _client(tmp_path)
    _write_telegram_channel(client, chat_id="100")

    queued_updates = [
        {"update_id": 21, "message": {"chat": {"id": 100}, "from": {"id": 7},
                                      "text": "first"}},
        {"update_id": 22, "message": {"chat": {"id": 100}, "from": {"id": 7},
                                      "text": "second"}},
    ]
    monkeypatch.setattr(
        routes_gateway.telegram, "get_updates",
        lambda **_kw: {"ok": True, "updates": queued_updates},
    )
    handled: list[str] = []
    monkeypatch.setattr(
        routes_gateway, "_handle_text",
        lambda c, cfg, cid, txt, uid, ident: handled.append(txt) or {"ok": True},
    )

    result = routes_gateway._telegram_poll_tick(client, "telegram")

    assert handled == ["first", "second"]
    assert result["offset"] == 23
    drain_events = [
        ev for ev in routes_gateway._gateway_events_snapshot(limit=50)
        if ev.get("kind") == "info" and ev.get("reason") == "backlog_drained"
    ]
    assert not drain_events, "no drain event when no first-poll-pending flag"


def test_first_poll_with_single_update_dispatches_normally(tmp_path, monkeypatch):
    """Edge case: only one queued update on the first tick. We must NOT
    mark this as 'drained' — there is nothing to drop and replying to
    that single message is exactly the desired behaviour.
    """
    routes_gateway.reset_gateway_events_for_tests()
    with routes_gateway._TELEGRAM_FIRST_POLL_LOCK:
        routes_gateway._TELEGRAM_FIRST_POLL_PENDING.clear()
        routes_gateway._TELEGRAM_FIRST_POLL_PENDING.add("telegram")

    client = _client(tmp_path)
    _write_telegram_channel(client, chat_id="100")

    queued = [
        {"update_id": 31, "message": {"chat": {"id": 100}, "from": {"id": 7},
                                      "text": "hi"}},
    ]
    monkeypatch.setattr(
        routes_gateway.telegram, "get_updates",
        lambda **_kw: {"ok": True, "updates": queued},
    )
    handled: list[str] = []
    monkeypatch.setattr(
        routes_gateway, "_handle_text",
        lambda c, cfg, cid, txt, uid, ident: handled.append(txt) or {"ok": True},
    )

    result = routes_gateway._telegram_poll_tick(client, "telegram")

    assert handled == ["hi"]
    assert result["offset"] == 32
    drain_events = [
        ev for ev in routes_gateway._gateway_events_snapshot(limit=50)
        if ev.get("kind") == "info" and ev.get("reason") == "backlog_drained"
    ]
    assert not drain_events
    # First-poll flag was consumed even with only one update so the next
    # tick definitely does not drain.
    with routes_gateway._TELEGRAM_FIRST_POLL_LOCK:
        assert "telegram" not in routes_gateway._TELEGRAM_FIRST_POLL_PENDING


def test_stop_pollers_rearms_first_poll_drain():
    """Stopping pollers must clear the pending set so a fresh start
    re-enables the drain. Otherwise a hot-reload after a long offline
    window would skip the drain.
    """
    with routes_gateway._TELEGRAM_FIRST_POLL_LOCK:
        routes_gateway._TELEGRAM_FIRST_POLL_PENDING.clear()
        routes_gateway._TELEGRAM_FIRST_POLL_PENDING.add("telegram")
        routes_gateway._TELEGRAM_FIRST_POLL_PENDING.add("telegram-vip")

    routes_gateway.stop_telegram_pollers()

    with routes_gateway._TELEGRAM_FIRST_POLL_LOCK:
        assert routes_gateway._TELEGRAM_FIRST_POLL_PENDING == set()
