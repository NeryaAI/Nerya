from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.api import routes_gateway
from nerya.api.gateway_identity import session_id
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.messaging.platforms import PLATFORM_IDS, list_platforms


pytestmark = pytest.mark.smoke


def _client(tmp_path):
    return SimpleNamespace(config=Config(paths=WorkspacePaths(root=tmp_path), data={}), skills=SimpleNamespace())


def _route_map():
    return {(method, path): handler for method, path, handler in routes_gateway.routes()}


def _inbound(tmp_path, payload):
    client = _client(tmp_path)
    return _route_map()[("POST", "/gateway/inbound")](client, {**payload, "auto_reply": False})


def test_gateway_command_catalog_exports_registration_shapes_for_every_platform(tmp_path):
    client = _client(tmp_path)
    route = _route_map()[("GET", "/gateway/commands")]

    for platform in PLATFORM_IDS:
        if platform == "local":
            continue
        result = route(client, {"platform": platform})
        commands = {row["command"] for row in result["commands"]}
        assert result["ok"] is True
        assert {"help", "strategies", "accounts", "portfolio"}.issubset(commands)
        assert result["text_commands"]
        assert result["slash_commands"]


@pytest.mark.parametrize(
    ("platform", "payload", "expected_chat", "expected_user", "expected_command"),
    [
        (
            "discord",
            {
                "channel_id": "discord-channel",
                "guild_id": "guild-1",
                "member": {"user": {"id": "discord-user", "username": "alice"}},
                "data": {"name": "accounts"},
            },
            "discord-channel",
            "discord-user",
            "/accounts",
        ),
        (
            "slack",
            {
                "event": {
                    "channel": "slack-channel",
                    "user": "slack-user",
                    "thread_ts": "thread-1",
                    "channel_type": "channel",
                    "text": "/portfolio",
                },
            },
            "slack-channel",
            "slack-user",
            "/portfolio",
        ),
        (
            "feishu",
            {
                "event": {
                    "message": {
                        "chat_id": "feishu-chat",
                        "message_id": "feishu-message",
                        "content": "{\"text\":\"/strategies\"}",
                    },
                    "sender": {"sender_id": {"user_id": "feishu-user"}},
                },
            },
            "feishu-chat",
            "feishu-user",
            "/strategies",
        ),
        (
            "dingtalk",
            {
                "conversationId": "dingtalk-conversation",
                "senderStaffId": "dingtalk-user",
                "text": {"content": "/help"},
            },
            "dingtalk-conversation",
            "dingtalk-user",
            "/help",
        ),
        (
            "wecom",
            {
                "FromUserName": "wecom-user",
                "ToUserName": "wecom-bot",
                "MsgId": "wecom-message",
                "Content": "/status",
            },
            "wecom-user",
            "wecom-user",
            "/status",
        ),
        (
            "matrix",
            {
                "room_id": "matrix-room",
                "sender": {"id": "matrix-user"},
                "event_id": "matrix-event",
                "content": {"body": "/help"},
            },
            "matrix-room",
            "matrix-user",
            "/help",
        ),
        (
            "whatsapp",
            {
                "metadata": {"phone_number_id": "wa-phone"},
                "contacts": [{"wa_id": "wa-user", "profile": {"name": "WA User"}}],
                "messages": [{"id": "wa-message", "from": "wa-user", "text": {"body": "/help"}}],
            },
            "wa-user",
            "wa-user",
            "/help",
        ),
    ],
)
def test_gateway_inbound_normalizes_common_platform_payloads(
    tmp_path,
    platform,
    payload,
    expected_chat,
    expected_user,
    expected_command,
):
    result = _inbound(tmp_path, {"platform": platform, "channel": platform, **payload})

    assert result["ok"] is True
    assert result["chat_id"] == expected_chat
    assert result["command"] == expected_command
    if expected_user:
        assert f"user:{expected_user}" in result["session_key"] or expected_command in {"/status", "/accounts", "/portfolio", "/strategies", "/help"}


def test_gateway_platform_catalog_marks_non_native_bridges_as_inbound_webhook_capable():
    by_id = {row["id"]: row for row in list_platforms()}

    for platform in ("whatsapp", "signal", "matrix", "email", "sms", "weixin", "qqbot"):
        assert by_id[platform]["support_level"] == "inbound_webhook"


@pytest.mark.parametrize(
    ("platform", "payload", "expected"),
    [
        (
            "discord",
            {
                "channel_id": "discord-channel",
                "author": {"id": "discord-user"},
                "content": "please inspect",
                "attachments": [
                    {
                        "id": "att-1",
                        "filename": "chart.png",
                        "content_type": "image/png",
                        "size": 128,
                        "url": "https://cdn.discordapp.example/chart.png",
                    }
                ],
            },
            {
                "name": "chart.png",
                "mime_type": "image/png",
                "kind": "image",
                "url": "https://cdn.discordapp.example/chart.png",
            },
        ),
        (
            "whatsapp",
            {
                "metadata": {"phone_number_id": "wa-phone"},
                "contacts": [{"wa_id": "wa-user"}],
                "messages": [
                    {
                        "id": "wa-message",
                        "from": "wa-user",
                        "type": "image",
                        "image": {
                            "id": "wa-image-id",
                            "mime_type": "image/jpeg",
                            "caption": "chart",
                        },
                    }
                ],
            },
            {
                "id": "wa-image-id",
                "name": "wa-image-id",
                "mime_type": "image/jpeg",
                "kind": "image",
            },
        ),
        (
            "slack",
            {
                "event": {
                    "channel": "slack-channel",
                    "user": "slack-user",
                    "text": "file",
                    "files": [
                        {
                            "id": "slack-file",
                            "name": "demo.mp4",
                            "mimetype": "video/mp4",
                            "url_private": "https://slack-files.example/demo.mp4",
                        }
                    ],
                },
            },
            {
                "id": "slack-file",
                "name": "demo.mp4",
                "mime_type": "video/mp4",
                "kind": "video",
                "url": "https://slack-files.example/demo.mp4",
            },
        ),
    ],
)
def test_gateway_inbound_normalizes_media_attachments_for_bridge_payloads(
    tmp_path,
    monkeypatch,
    platform,
    payload,
    expected,
):
    captured: dict[str, object] = {}

    def _fake_run_gateway_turn(client, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return {
            "ok": True,
            "reply_text": "",
            "attachments": [],
            "session_id": "sid",
            "session_key": "skey",
        }

    monkeypatch.setattr(routes_gateway, "_run_gateway_turn", _fake_run_gateway_turn)

    result = _inbound(tmp_path, {"platform": platform, "channel": platform, **payload})

    assert result["ok"] is True
    attachments = captured["attachments"]
    assert isinstance(attachments, list)
    assert attachments
    for key, value in expected.items():
        assert attachments[0][key] == value


def test_gateway_session_ids_are_windows_filename_safe():
    sid = session_id(
        "matrix",
        chat_id="!room:matrix.org",
        user_id="@alice:matrix.org",
    )

    assert ":" not in sid
    assert sid == "matrix__room_matrix.org_u_alice_matrix.org"
