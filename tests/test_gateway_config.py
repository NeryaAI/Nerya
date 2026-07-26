from __future__ import annotations

from types import SimpleNamespace

from nerya.api import routes_gateway
from nerya.api.route_scopes import required_scope
from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.security.secrets import SecretVault


def _client(tmp_path):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})
    return SimpleNamespace(config=cfg, skills=SimpleNamespace())


def _route_map():
    return {(method, path): handler for method, path, handler in routes_gateway.routes()}


def test_gateway_config_upsert_vaults_plaintext_and_redacts_response(tmp_path, monkeypatch):
    client = _client(tmp_path)
    routes = _route_map()
    monkeypatch.setattr(
        routes_gateway,
        "launch_configured_gateways_on_start",
        lambda _client: {"scheduled": True, "telegram_pollers": []},
    )
    secret_url = "https://discord.example/webhooks/super-secret-token"

    result = routes[("POST", "/gateway/config/upsert")](
        client,
        {
            "channel": "discord_ops",
            "kind": "discord",
            "webhook_url": secret_url,
            "trade_notifications": True,
            "topics": ["trades", "approvals"],
            "username": "Nerya",
        },
    )

    assert result["ok"] is True
    assert secret_url not in str(result)
    doc = yaml_io.load(client.config.paths.messages_channels, default={})
    channel_cfg = doc["channels"]["discord_ops"]
    assert channel_cfg["kind"] == "discord"
    assert channel_cfg["webhook_url_ref"] == "vault://gateway_discord_ops_webhook_url"
    assert "webhook_url" not in channel_cfg
    assert secret_url not in client.config.paths.messages_channels.read_text(encoding="utf-8")
    vault = SecretVault.open(client.config.paths.vault_enc)
    assert vault.resolve("gateway_discord_ops_webhook_url", required_scope="messaging") == secret_url

    public_channel = result["channel"]
    assert public_channel["secret_refs"]["webhook_url_ref"]["ref"] == "vault://gateway_discord_ops_webhook_url"
    assert public_channel["config"]["trade_notifications"] is True
    assert public_channel["config"]["topics"] == ["trades", "approvals"]
    assert required_scope("GET", "/gateway/config") == "read:runtime"
    assert required_scope("POST", "/gateway/config/upsert") == "write:config"


def test_gateway_config_delete_removes_channel(tmp_path, monkeypatch):
    client = _client(tmp_path)
    routes = _route_map()
    monkeypatch.setattr(
        routes_gateway,
        "launch_configured_gateways_on_start",
        lambda _client: {"scheduled": True, "telegram_pollers": []},
    )
    routes[("POST", "/gateway/config/upsert")](
        client,
        {
            "channel": "telegram_ops",
            "kind": "telegram",
            "bot_token": "123456:telegram-secret",
            "chat_id": "123456789",
        },
    )

    deleted = routes[("POST", "/gateway/config/delete")](client, {"channel": "telegram_ops"})

    assert deleted["ok"] is True
    assert deleted["deleted"] is True
    doc = yaml_io.load(client.config.paths.messages_channels, default={})
    assert "telegram_ops" not in (doc.get("channels") or {})
    assert required_scope("POST", "/gateway/config/delete") == "write:config"


def test_gateway_config_test_uses_message_pipeline_without_exposing_secrets(tmp_path, monkeypatch):
    client = _client(tmp_path)
    routes = _route_map()
    yaml_io.dump(
        client.config.paths.messages_channels,
        {
            "channels": {
                "discord_ops": {
                    "kind": "discord",
                    "webhook_url_ref": "vault://discord_webhook_url",
                }
            }
        },
    )

    class FakePipeline:
        def __init__(self, *, config):
            self.config = config

        def send(
            self, *, channel, text, strategy_id=None, template=None,
            context=None, attachments=None,
        ):
            return {
                "message_id": "msg-test",
                "channel": channel,
                "kind": "discord",
                "text": text,
                "delivered": True,
                "webhook_url": "https://should-not-leak",
            }

    monkeypatch.setattr(routes_gateway, "MessagePipeline", FakePipeline)

    result = routes[("POST", "/gateway/config/test")](
        client,
        {"channel": "discord_ops", "text": "gateway smoke", "mode": "send_only"},
    )

    assert result["ok"] is True
    assert result["delivery"]["channel"] == "discord_ops"
    assert "https://should-not-leak" not in str(result)
    assert required_scope("POST", "/gateway/config/test") == "gateway:send"


def test_gateway_config_test_runs_agent_turn_and_replies(tmp_path, monkeypatch):
    client = _client(tmp_path)
    routes = _route_map()
    yaml_io.dump(
        client.config.paths.messages_channels,
        {
            "channels": {
                "discord_ops": {
                    "kind": "discord",
                    "webhook_url_ref": "vault://discord_webhook_url",
                    "allowed_user_ids": ["operator-1"],
                }
            }
        },
    )
    calls: list[dict] = []
    sent: list[dict] = []

    class FakeHooks:
        def register(self, _phase, _handler) -> None:
            return None

    class FakeKernel:
        def __init__(self, *, config, skills):
            self.config = config
            self.skills = skills
            self.hooks = FakeHooks()

        def run_turn(self, *, trigger, session_id):
            calls.append({"trigger": trigger, "session_id": session_id})
            return SimpleNamespace(
                turn_id="turn-agent-test",
                final_text="real agent reply",
                decision={
                    "action": "send_message",
                    "payload": {"text": "real agent reply"},
                },
                tool_trace=[],
                actions=[],
                blocks=[],
            )

    class FakePipeline:
        def __init__(self, *, config):
            self.config = config

        def send(
            self, *, channel, text, strategy_id=None, template=None,
            context=None, attachments=None,
        ):
            sent.append({"channel": channel, "text": text, "context": context})
            return {
                "message_id": "msg-agent-test",
                "channel": channel,
                "kind": "discord",
                "text": text,
                "delivered": True,
            }

    monkeypatch.setattr(routes_gateway, "AgentKernel", FakeKernel)
    monkeypatch.setattr(routes_gateway, "MessagePipeline", FakePipeline)

    result = routes[("POST", "/gateway/config/test")](
        client,
        {"channel": "discord_ops", "text": "prove agent wiring"},
    )

    assert result["ok"] is True
    assert result["mode"] == "agent"
    assert result["agent"]["turn_id"] == "turn-agent-test"
    assert result["reply_text"] == "real agent reply"
    assert calls[0]["trigger"]["source"] == "discord"
    assert calls[0]["trigger"]["kind"] == "user.chat"
    assert calls[0]["trigger"]["payload"]["text"] == "prove agent wiring"
    assert calls[0]["trigger"]["payload"]["user_id"] == "operator-1"
    assert sent[0]["channel"] == "discord_ops"
    assert sent[0]["text"] == "real agent reply"
    assert sent[0]["context"]["kind"] == "gateway_agent_test"
