from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from nerya.api import routes_approvals, routes_gateway
from nerya.core import jsonl, yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.db.repositories import AgentSessionRepository
from nerya.db.sqlite import connect


pytestmark = pytest.mark.smoke


def _client(tmp_path):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})
    return SimpleNamespace(config=cfg, skills=SimpleNamespace())


def _route_map():
    return {(method, path): handler for method, path, handler in routes_gateway.routes()}


def _write_channel(client, *, extra: dict | None = None) -> None:
    cfg = {
        "kind": "discord",
        "webhook_url_ref": "vault://discord_webhook_url",
    }
    if extra:
        cfg.update(extra)
    yaml_io.dump(
        client.config.paths.messages_channels,
        {"channels": {"discord_ops": cfg}},
    )


class _Hooks:
    def register(self, _phase, _handler) -> None:
        return None


class _FakeKernel:
    calls: list[dict] = []

    def __init__(self, *, config, skills):
        self.config = config
        self.skills = skills
        self.hooks = _Hooks()

    def run_turn(self, *, trigger, session_id):
        self.calls.append({"trigger": trigger, "session_id": session_id})
        return SimpleNamespace(
            turn_id=f"turn-{len(self.calls)}",
            final_text=f"reply for {session_id}",
            decision={},
            tool_trace=[],
            actions=[],
            blocks=[],
        )


def test_gateway_inbound_isolates_group_sessions_by_user(tmp_path, monkeypatch):
    client = _client(tmp_path)
    _write_channel(client, extra={"auto_reply": False})
    _FakeKernel.calls = []
    monkeypatch.setattr(routes_gateway, "AgentKernel", _FakeKernel)

    routes = _route_map()
    first = routes[("POST", "/gateway/inbound")](
        client,
        {
            "platform": "discord",
            "channel": "discord_ops",
            "chat_id": "group-1",
            "user_id": "u1",
            "text": "hello from u1",
        },
    )
    second = routes[("POST", "/gateway/inbound")](
        client,
        {
            "platform": "discord",
            "channel": "discord_ops",
            "chat_id": "group-1",
            "user_id": "u2",
            "text": "hello from u2",
        },
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["session_id"] != second["session_id"]
    assert first["session_key"] == "discord:group-1:user:u1"
    assert second["session_key"] == "discord:group-1:user:u2"
    assert _FakeKernel.calls[0]["trigger"]["payload"]["user_id"] == "u1"
    assert _FakeKernel.calls[1]["trigger"]["payload"]["user_id"] == "u2"


def test_gateway_inbound_can_share_group_session_when_configured(tmp_path, monkeypatch):
    client = _client(tmp_path)
    _write_channel(
        client,
        extra={"auto_reply": False, "group_sessions_per_user": False},
    )
    _FakeKernel.calls = []
    monkeypatch.setattr(routes_gateway, "AgentKernel", _FakeKernel)

    routes = _route_map()
    first = routes[("POST", "/gateway/inbound")](
        client,
        {
            "platform": "discord",
            "channel": "discord_ops",
            "chat_id": "group-1",
            "user_id": "u1",
            "text": "hello from u1",
        },
    )
    second = routes[("POST", "/gateway/inbound")](
        client,
        {
            "platform": "discord",
            "channel": "discord_ops",
            "chat_id": "group-1",
            "user_id": "u2",
            "text": "hello from u2",
        },
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["session_id"] == second["session_id"]
    assert first["session_key"] == "discord:group-1"
    assert second["session_key"] == "discord:group-1"


def test_gateway_inbound_allowlist_blocks_unknown_user_before_agent(tmp_path, monkeypatch):
    client = _client(tmp_path)
    _write_channel(
        client,
        extra={"auto_reply": False, "allowed_user_ids": ["u1"]},
    )

    class NoKernel:
        def __init__(self, *args, **kwargs):  # pragma: no cover - failure path
            raise AssertionError("AgentKernel should not be constructed")

    monkeypatch.setattr(routes_gateway, "AgentKernel", NoKernel)
    routes = _route_map()

    result = routes[("POST", "/gateway/inbound")](
        client,
        {
            "platform": "discord",
            "channel": "discord_ops",
            "chat_id": "group-1",
            "user_id": "u2",
            "text": "blocked",
        },
    )

    assert result["ok"] is False
    assert result["error"] == "unauthorized"
    assert result["reason"] == "user_not_allowed"


def test_gateway_inbound_routes_approval_callback_without_agent(tmp_path, monkeypatch):
    client = _client(tmp_path)
    _write_channel(client, extra={"auto_reply": False})
    client.config.paths.approvals_pending.parent.mkdir(parents=True, exist_ok=True)
    client.config.paths.approvals_pending.write_text(
        json.dumps(
            {
                "approval_id": "approval-1",
                "approval_actor_id": "u1",
                "state": "pending",
                "kind": "tool_permission",
                "expires_at": time.time() + 60,
                "payload": {"tool": {"name": "run_shell"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class NoKernel:
        def __init__(self, *args, **kwargs):  # pragma: no cover - failure path
            raise AssertionError("AgentKernel should not be constructed")

    monkeypatch.setattr(routes_gateway, "AgentKernel", NoKernel)
    routes = _route_map()

    result = routes[("POST", "/gateway/inbound")](
        client,
        {
            "platform": "discord",
            "channel": "discord_ops",
            "chat_id": "group-1",
            "user_id": "u1",
            "callback_data": "approve:approval-1",
        },
    )

    assert result["ok"] is True
    assert result["kind"] == "approval_callback"
    assert result["state"] == "approved"
    approved = jsonl.read_all(client.config.paths.approvals_approved)
    assert approved[0]["approval_id"] == "approval-1"
    assert approved[0]["state"] == "approved"


def test_gateway_inbound_uses_nested_telegram_callback_identity(tmp_path, monkeypatch):
    client = _client(tmp_path)
    yaml_io.dump(
        client.config.paths.messages_channels,
        {
            "channels": {
                "telegram": {
                    "kind": "telegram",
                    "bot_token_ref": "vault://telegram_bot_token",
                    "chat_id": "chat-1",
                    "allowed_user_ids": ["u1"],
                }
            }
        },
    )
    client.config.paths.approvals_pending.parent.mkdir(parents=True, exist_ok=True)
    client.config.paths.approvals_pending.write_text(
        json.dumps(
            {
                "approval_id": "approval-telegram",
                "actor_id": "u1",
                "state": "pending",
                "kind": "tool_permission",
                "expires_at": time.time() + 60,
                "payload": {"tool": {"name": "run_shell"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    cleared: list[dict] = []
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(routes_gateway.telegram, "answer_callback_query", lambda **_kwargs: None)
    monkeypatch.setattr(
        routes_gateway.telegram,
        "clear_reply_markup",
        lambda **kwargs: cleared.append(kwargs),
    )
    monkeypatch.setattr(
        routes_approvals,
        "_publish_approval_resolution",
        lambda approval_id, **kwargs: published.append((approval_id, kwargs)),
    )
    routes = _route_map()

    result = routes[("POST", "/gateway/inbound")](
        client,
        {
            "platform": "telegram",
            "channel": "telegram",
            "callback_query": {
                "id": "cb-1",
                "data": "approve:approval-telegram",
                "from": {"id": "u1", "username": "u1"},
                "message": {
                    "message_id": 42,
                    "chat": {"id": "chat-1", "type": "group"},
                },
            },
        },
    )

    assert result["ok"] is True
    assert result["state"] == "approved"
    approved = jsonl.read_all(client.config.paths.approvals_approved)
    assert approved[0]["approval_id"] == "approval-telegram"
    assert approved[0]["resolved_by_actor_id"] == "u1"
    assert len(published) == 1
    assert published[0][0] == "approval-telegram"
    assert published[0][1]["state"] == "approved"
    assert len(cleared) == 1

    repeated = routes[("POST", "/gateway/inbound")](
        client,
        {
            "platform": "telegram",
            "channel": "telegram",
            "callback_query": {
                "id": "cb-2",
                "data": "approve:approval-telegram",
                "from": {"id": "u1", "username": "u1"},
                "message": {
                    "message_id": 42,
                    "chat": {"id": "chat-1", "type": "group"},
                },
            },
        },
    )

    assert repeated["ok"] is False
    assert repeated["state"] == "error"
    assert len(published) == 1
    assert len(cleared) == 1


def test_gateway_inbound_auto_replies_through_configured_channel(tmp_path, monkeypatch):
    client = _client(tmp_path)
    _write_channel(client)
    _FakeKernel.calls = []
    sent: list[dict] = []

    class FakePipeline:
        def __init__(self, *, config):
            self.config = config

        def send(
            self,
            *,
            channel,
            text,
            strategy_id=None,
            template=None,
            context=None,
            attachments=None,
        ):
            sent.append({
                "channel": channel,
                "text": text,
                "context": context,
                "attachments": attachments,
            })
            return {
                "message_id": "msg-auto",
                "channel": channel,
                "kind": "discord",
                "text": text,
                "delivered": True,
            }

    monkeypatch.setattr(routes_gateway, "AgentKernel", _FakeKernel)
    monkeypatch.setattr(routes_gateway, "MessagePipeline", FakePipeline)
    routes = _route_map()

    result = routes[("POST", "/gateway/inbound")](
        client,
        {
            "platform": "discord",
            "channel": "discord_ops",
            "chat_id": "group-1",
            "user_id": "u1",
            "text": "please answer",
        },
    )

    assert result["ok"] is True
    assert result["delivery"]["delivered"] is True
    assert sent == [
        {
            "channel": "discord_ops",
            "text": result["reply_text"],
            "attachments": [],
            "context": {
                "kind": "gateway_auto_reply",
                "platform": "discord",
                "chat_id": "group-1",
                "session_id": result["session_id"],
                "session_key": "discord:group-1:user:u1",
                "user_id": "u1",
                "actor_id": "u1",
                "thread_id": "",
            },
        }
    ]


def test_gateway_reply_sends_attachment_only_payload(tmp_path, monkeypatch):
    client = _client(tmp_path)
    sent: list[dict] = []
    attachment = {"name": "chart.png", "mime_type": "image/png"}

    class FakePipeline:
        def __init__(self, *, config):
            self.config = config

        def send(
            self,
            *,
            channel,
            text,
            strategy_id=None,
            template=None,
            context=None,
            attachments=None,
        ):
            sent.append({
                "channel": channel,
                "text": text,
                "attachments": attachments,
                "context": context,
            })
            return {
                "message_id": "msg-attachment",
                "channel": channel,
                "kind": "discord",
                "text": text,
                "delivered": True,
            }

    monkeypatch.setattr(routes_gateway, "MessagePipeline", FakePipeline)

    delivery = routes_gateway._reply_gateway_channel(
        client,
        channel="discord_ops",
        platform="discord",
        cfg={"kind": "discord", "webhook_url_ref": "vault://discord_webhook_url"},
        chat_id="group-1",
        text="",
        attachments=[attachment],
        context={"kind": "gateway_auto_reply"},
    )

    assert delivery["delivered"] is True
    assert sent == [
        {
            "channel": "discord_ops",
            "text": "Attachment returned: chart.png",
            "attachments": [attachment],
            "context": {"kind": "gateway_auto_reply"},
        }
    ]


def test_gateway_session_command_stores_active_session_under_identity_key(tmp_path):
    client = _client(tmp_path)
    _write_channel(client, extra={"auto_reply": False})
    con = connect(client.config.paths.db)
    repo = AgentSessionRepository(con)
    repo.upsert_session(
        session_id="shared-session",
        title="Shared Session",
        source="dashboard",
        ts=1000,
    )
    repo.record_message(
        message_id="shared-session:assistant",
        session_id="shared-session",
        turn_id="turn-1",
        role="assistant",
        content="existing assistant reply",
        ts=1001,
    )
    con.close()

    routes = _route_map()
    result = routes[("POST", "/gateway/inbound")](
        client,
        {
            "platform": "discord",
            "channel": "discord_ops",
            "chat_id": "group-1",
            "user_id": "u1",
            "text": "/session shared-session",
        },
    )

    assert result["ok"] is True
    state = yaml_io.load(routes_gateway._state_path(client), default={})
    assert state["active_sessions"] == {
        "discord:group-1:user:u1": "shared-session"
    }
