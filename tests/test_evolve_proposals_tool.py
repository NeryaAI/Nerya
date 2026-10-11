from __future__ import annotations

from copy import deepcopy
import pytest

from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import create_proposal
from nerya.tools.native.evolve import (
    evolve_core_config_patch_handler,
    evolve_proposals_handler,
    evolve_reflect_handler,
)
from nerya.tools.types import ToolCall, ToolErrorKind


pytestmark = pytest.mark.smoke


def test_evolve_reflect_uses_canonical_runner_payload(tmp_path, monkeypatch) -> None:
    paths = WorkspacePaths(root=tmp_path)

    def fake_evolve(config: Config, *, strategy_id=None, isolated=False) -> dict:
        assert isolated is True
        assert config.paths is paths
        return {
            "proposal": {
                "id": "prp_reflect_1",
                "kind": "learning_update",
                "validation_plan_id": "vpl_reflect_1",
            },
            "ranked": [{"id": "seed_1"}, {"id": "seed_2"}],
            "signals": [{"id": "sig_1", "evidence_refs": ["journal:agent:0"]}],
            "selected_assets": {
                "genes": [{"id": "gene_1"}],
                "capsules": [{"id": "cap_1"}],
            },
            "event": {"id": "evt_1"},
        }

    monkeypatch.setattr("nerya.evolution.runner.evolve", fake_evolve)

    result = evolve_reflect_handler(
        ToolCall(name="evolve_reflect", arguments={}),
        config=Config(paths=paths),
    )

    assert not result.is_error
    data = result.content[0].data
    assert data["proposal"]["id"] == "prp_reflect_1"
    assert data["validation_plan_id"] == "vpl_reflect_1"
    assert data["ranked_seeds"] == [{"id": "seed_1"}, {"id": "seed_2"}]
    assert data["seed_count"] == 2
    assert data["signals"][0]["id"] == "sig_1"
    assert data["signal_count"] == 1
    assert data["selected_assets"]["genes"][0]["id"] == "gene_1"
    assert data["event"]["id"] == "evt_1"


def test_evolve_proposals_supports_exact_lookup_beyond_limit(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    first = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="older target proposal",
    )
    create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="newer unrelated proposal",
    )

    result = evolve_proposals_handler(
        ToolCall(
            name="evolve_proposals",
            arguments={"proposal_id": first.id, "limit": 1},
        ),
        config=Config(paths=paths),
    )

    data = result.content[0].data
    assert data["found"] is True
    assert data["proposal_id"] == first.id
    assert data["count"] == 1
    assert data["proposal"]["id"] == first.id
    assert data["proposals"][0]["id"] == first.id
    assert data["proposal"]["path"].endswith(first.id)


def test_evolve_proposals_exact_lookup_reports_not_found(tmp_path) -> None:
    result = evolve_proposals_handler(
        ToolCall(
            name="evolve_proposals",
            arguments={"proposal_id": "prp_missing"},
        ),
        config=Config(paths=WorkspacePaths(root=tmp_path)),
    )

    data = result.content[0].data
    assert data["found"] is False
    assert data["proposal_id"] == "prp_missing"
    assert data["count"] == 0
    assert data["proposal"] is None


def test_evolve_core_config_patch_creates_proposal_only_patch(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    result = evolve_core_config_patch_handler(
        ToolCall(
            name="evolve_core_config_patch",
            arguments={
                "target": "agents.yml",
                "summary": "Tune default agent parallelism",
                "config_after": {
                    "llm": {"reasoning_effort": "medium"},
                    "agent": {"max_parallel": 8},
                },
            },
        ),
        config=Config(paths=paths),
    )

    assert not result.is_error
    proposal = result.content[0].data["proposal"]
    assert proposal["kind"] == "core_config_patch"
    assert proposal["target"] == "agents.yml"
    assert (paths.evolution / "proposals" / proposal["id"] / "after" / "agents.yml").exists()
    assert not (tmp_path / "agents.yml").exists()


def test_evolve_core_config_patch_allows_news_feed_config_patch(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    result = evolve_core_config_patch_handler(
        ToolCall(
            name="evolve_core_config_patch",
            arguments={
                "target": "news_feeds.yml",
                "summary": "Add custom RSS feed",
                "config_after": {
                    "feeds": [
                        {
                            "id": "example",
                            "url": "https://example.com/feed.xml",
                            "type": "rss",
                            "enabled": True,
                        }
                    ]
                },
            },
        ),
        config=Config(paths=paths),
    )

    assert not result.is_error
    proposal = result.content[0].data["proposal"]
    assert proposal["kind"] == "core_config_patch"
    assert proposal["target"] == "news_feeds.yml"
    assert (paths.evolution / "proposals" / proposal["id"] / "after" / "news_feeds.yml").exists()
    assert not (tmp_path / "news_feeds.yml").exists()


def test_evolve_core_config_patch_allows_message_channel_config_patch(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    result = evolve_core_config_patch_handler(
        ToolCall(
            name="evolve_core_config_patch",
            arguments={
                "target": "messages/channels.yml",
                "summary": "Add Discord critical-risk webhook channel",
                "config_after": {
                    "channels": {
                        "discord_critical": {
                            "kind": "discord",
                            "webhook_url_ref": "vault://discord_critical_webhook",
                            "topics": ["risk.critical", "trade.execution"],
                            "trade_notifications": True,
                        }
                    }
                },
            },
        ),
        config=Config(paths=paths),
    )

    assert not result.is_error
    proposal = result.content[0].data["proposal"]
    assert proposal["kind"] == "core_config_patch"
    assert proposal["target"] == "messages/channels.yml"
    after_path = paths.evolution / "proposals" / proposal["id"] / "after" / "messages" / "channels.yml"
    assert after_path.exists()
    after_text = after_path.read_text(encoding="utf-8")
    assert "kind: discord" in after_text
    assert "webhook_url_ref: vault://discord_critical_webhook" in after_text
    assert "topics:" in after_text
    assert "events:" not in after_text
    assert "webhook_url:" not in after_text
    assert not (tmp_path / "messages" / "channels.yml").exists()


def test_evolve_core_config_patch_normalizes_message_channel_aliases(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    result = evolve_core_config_patch_handler(
        ToolCall(
            name="evolve_core_config_patch",
            arguments={
                "target": "messages/channels.yaml",
                "summary": "Add Discord webhook channel",
                "config_after": {
                    "channels": {
                        "discord_ops": {
                            "type": "discord",
                            "url": "https://discord.com/api/webhooks/REPLACE_ME/REPLACE_ME",
                            "events": {"item": ["risk_critical", "trade.execution"]},
                            "enabled": "true",
                        }
                    }
                },
            },
        ),
        config=Config(paths=paths),
    )

    assert not result.is_error
    proposal = result.content[0].data["proposal"]
    after_path = paths.evolution / "proposals" / proposal["id"] / "after" / "messages" / "channels.yaml"
    after_text = after_path.read_text(encoding="utf-8")
    assert "kind: discord" in after_text
    assert "webhook_url_ref: vault://gateway_discord_ops_webhook_url" in after_text
    assert "https://discord.com/api/webhooks" not in after_text
    assert "topics:" in after_text
    assert "- risk_critical" in after_text
    assert "events:" not in after_text


def test_evolve_core_config_patch_normalizes_message_channel_model_draft(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    result = evolve_core_config_patch_handler(
        ToolCall(
            name="evolve_core_config_patch",
            arguments={
                "target": "messages/channels.yaml",
                "summary": "Add critical-risk Discord routing",
                "config_after": {
                    "version": "2",
                    "channels": {
                        "discord_critical_risk": {
                            "channel_type": "discord_webhook",
                            "description": (
                                "Discord webhook for critical risk events. "
                                "Webhook URL is resolved from env var DISCORD_WEBHOOK_URL."
                            ),
                            "enabled": "true",
                            "webhook_url_env": "DISCORD_WEBHOOK_URL_CRITICAL_RISK",
                            "webhook_url_default": "null",
                            "mention_role_id_env": "DISCORD_CRITICAL_MENTION_ROLE_ID",
                            "message_format": {
                                "title": "Critical risk",
                                "username": "Nerya Risk Sentinel",
                            },
                            "events": {"item": ["risk.critical"]},
                            "min_severity": "critical",
                            "rate_limit_per_minute": "30",
                            "delivery_targets": {
                                "target_id": "${DISCORD_WEBHOOK_URL_CRITICAL_RISK}",
                                "enabled": "true",
                            },
                        }
                    },
                    "readiness": {
                        "status": "blocked_pending_credentials",
                        "required_env_vars": {"item": "DISCORD_WEBHOOK_URL_CRITICAL_RISK"},
                    },
                    "routes": {
                        "critical_risk": {
                            "channels": {"item": ["discord_critical_risk", "console"]},
                            "min_severity": "critical",
                        }
                    },
                    "global_policy": {"critical_always_notify": "true"},
                },
            },
        ),
        config=Config(paths=paths),
    )

    assert not result.is_error
    proposal = result.content[0].data["proposal"]
    after_path = paths.evolution / "proposals" / proposal["id"] / "after" / "messages" / "channels.yaml"
    after = yaml_io.load(after_path, default={})
    assert after == {
        "version": "2",
        "channels": {
            "discord_critical_risk": {
                "kind": "discord",
                "enabled": True,
                "webhook_url_ref": "vault://gateway_discord_critical_risk_webhook_url",
                "topics": ["risk.critical"],
                "username": "Nerya Risk Sentinel",
                "label": "Critical risk",
            }
        },
        "severity_routes": {
            "critical": ["discord_critical_risk", "console"],
        },
    }
    after_text = after_path.read_text(encoding="utf-8")
    assert "DISCORD_WEBHOOK_URL_CRITICAL_RISK" not in after_text
    assert "DISCORD_WEBHOOK_URL" not in after_text
    assert "channel_type" not in after_text
    assert "delivery_targets" not in after_text
    assert "\nroutes:" not in after_text
    assert "global_policy" not in after_text


def test_evolve_core_config_patch_infers_webhook_kind_from_url_ref(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    result = evolve_core_config_patch_handler(
        ToolCall(
            name="evolve_core_config_patch",
            arguments={
                "target": "messages/channels.yml",
                "summary": "Add generic webhook channel",
                "config_after": {
                    "channels": {
                        "webhook_site_strategy_fills": {
                            "url_ref": "vault://gateway_webhook_site_strategy_fills_url",
                        }
                    }
                },
            },
        ),
        config=Config(paths=paths),
    )

    assert not result.is_error
    proposal = result.content[0].data["proposal"]
    after_path = paths.evolution / "proposals" / proposal["id"] / "after" / "messages" / "channels.yml"
    after = yaml_io.load(after_path, default={})
    assert after["channels"]["webhook_site_strategy_fills"] == {
        "kind": "webhook",
        "url_ref": "vault://gateway_webhook_site_strategy_fills_url",
    }


def test_evolve_core_config_patch_preserves_telegram_chat_id_vault_ref(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    result = evolve_core_config_patch_handler(
        ToolCall(
            name="evolve_core_config_patch",
            arguments={
                "target": "messages/channels.yml",
                "summary": "Add Telegram gateway channel",
                "config_after": {
                    "channels": {
                        "telegram": {
                            "kind": "telegram",
                            "bot_token_ref": "vault://TG_BOT_TOKEN",
                            "chat_id": "vault://TG_CHAT_ID",
                            "enabled": "true",
                            "mode": "polling",
                        }
                    }
                },
            },
        ),
        config=Config(paths=paths),
    )

    assert not result.is_error
    proposal = result.content[0].data["proposal"]
    after_path = paths.evolution / "proposals" / proposal["id"] / "after" / "messages" / "channels.yml"
    after = yaml_io.load(after_path, default={})
    assert after["channels"]["telegram"] == {
        "kind": "telegram",
        "enabled": True,
        "mode": "polling",
        "bot_token_ref": "vault://TG_BOT_TOKEN",
        "chat_id_ref": "vault://TG_CHAT_ID",
    }


def test_evolve_core_config_patch_wraps_top_level_message_channel_map(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    result = evolve_core_config_patch_handler(
        ToolCall(
            name="evolve_core_config_patch",
            arguments={
                "target": "messages/channels.yml",
                "summary": "Add Telegram gateway channel",
                "config_after": {
                    "telegram": {
                        "bot_token_ref": "vault://TG_BOT_TOKEN",
                        "chat_id_ref": "vault://TG_CHAT_ID",
                    }
                },
            },
        ),
        config=Config(paths=paths),
    )

    assert not result.is_error
    proposal = result.content[0].data["proposal"]
    after_path = paths.evolution / "proposals" / proposal["id"] / "after" / "messages" / "channels.yml"
    after = yaml_io.load(after_path, default={})
    assert after == {
        "channels": {
            "telegram": {
                "kind": "telegram",
                "bot_token_ref": "vault://TG_BOT_TOKEN",
                "chat_id_ref": "vault://TG_CHAT_ID",
            }
        }
    }


def test_evolve_core_config_patch_normalizes_severity_routing_without_channels(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    result = evolve_core_config_patch_handler(
        ToolCall(
            name="evolve_core_config_patch",
            arguments={
                "target": "messages/channels.yml",
                "summary": "Add severity routing",
                "config_after": {
                    "severity_routing": {
                        "rules": {
                            "info": {"channels": {"item": "telegram"}},
                            "critical": {
                                "channels": {"item": ["telegram", "discord"]},
                            },
                            "silent": {"channels": {"item": "none"}},
                        },
                        "default_channels": {"item": "telegram"},
                        "silent_suppresses_push": "true",
                    }
                },
            },
        ),
        config=Config(paths=paths),
    )

    assert not result.is_error
    proposal = result.content[0].data["proposal"]
    after_path = paths.evolution / "proposals" / proposal["id"] / "after" / "messages" / "channels.yml"
    after = yaml_io.load(after_path, default={})
    assert after == {
        "severity_routes": {
            "info": ["telegram"],
            "critical": ["telegram", "discord"],
            "silent": [],
            "*": ["telegram"],
        }
    }
    after_text = after_path.read_text(encoding="utf-8")
    assert "severity_routing" not in after_text
    assert "silent_suppresses_push" not in after_text


def test_evolve_core_config_patch_normalizes_direct_severity_routing_map(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    result = evolve_core_config_patch_handler(
        ToolCall(
            name="evolve_core_config_patch",
            arguments={
                "target": "messages/channels.yaml",
                "summary": "Add direct severity routing map",
                "config_after": {
                    "severity_routing": {
                        "info": {"channels": {"item": "telegram"}},
                        "critical": {
                            "channels": {"item": ["telegram", "discord"]},
                        },
                        "silent": {"channels": ""},
                    }
                },
            },
        ),
        config=Config(paths=paths),
    )

    assert not result.is_error
    proposal = result.content[0].data["proposal"]
    after_path = paths.evolution / "proposals" / proposal["id"] / "after" / "messages" / "channels.yaml"
    after = yaml_io.load(after_path, default={})
    assert after == {
        "severity_routes": {
            "info": ["telegram"],
            "critical": ["telegram", "discord"],
            "silent": [],
        }
    }


def test_evolve_core_config_patch_normalizes_routing_by_severity_map(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    result = evolve_core_config_patch_handler(
        ToolCall(
            name="evolve_core_config_patch",
            arguments={
                "target": "messages/channels.yaml",
                "summary": "Add severity routing",
                "config_after": {
                    "routing": {
                        "by_severity": {
                            "info": {"channels": {"item": "telegram"}},
                            "critical": {
                                "channels": {"item": ["telegram", "discord"]},
                            },
                            "silent": {"channels": {"item": "none"}, "push": "false"},
                        },
                        "default": {"channels": {"item": "telegram"}},
                    },
                    "channels": {
                        "telegram": {"enabled": "true"},
                        "discord": {"enabled": "true"},
                    },
                },
            },
        ),
        config=Config(paths=paths),
    )

    assert not result.is_error
    proposal = result.content[0].data["proposal"]
    after_path = paths.evolution / "proposals" / proposal["id"] / "after" / "messages" / "channels.yaml"
    after = yaml_io.load(after_path, default={})
    assert after == {
        "channels": {
            "telegram": {
                "kind": "telegram",
                "enabled": True,
            },
            "discord": {
                "kind": "discord",
                "enabled": True,
            },
        },
        "severity_routes": {
            "info": ["telegram"],
            "critical": ["telegram", "discord"],
            "silent": [],
        },
    }
    after_text = after_path.read_text(encoding="utf-8")
    assert "\nrouting:" not in after_text


def test_evolve_core_config_patch_rejects_protected_trading_scope(tmp_path) -> None:
    result = evolve_core_config_patch_handler(
        ToolCall(
            name="evolve_core_config_patch",
            arguments={
                "target": "nerya.yml",
                "summary": "Raise global exposure",
                "config_after": {
                    "trading": {
                        "max_global_exposure_pct": 200,
                    },
                },
            },
        ),
        config=Config(paths=WorkspacePaths(root=tmp_path)),
    )

    assert result.is_error
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION_DENIED
    assert "advisory reject" in result.error.message
    assert result.error.detail["reason"] == "protected_scope"
    assert "operator_review" not in result.error.recovery_hint
    assert not (tmp_path / "nerya.yml").exists()


def _protected_mcp_call(config_after: dict) -> ToolCall:
    return ToolCall(name="evolve_core_config_patch", arguments={
        "target": "nerya.yml", "summary": "Review MCP exposure",
        "config_after": config_after,
    })


def test_protected_mcp_config_reject_is_not_mcp_connection_authorization(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    config = Config(paths=paths, data=deepcopy(DEFAULT_CONFIG))
    after = {"mcp": {"enabled": not DEFAULT_CONFIG["mcp"]["enabled"]}}
    result = evolve_core_config_patch_handler(_protected_mcp_call(after), config=config)

    assert result.is_error and result.error.kind == ToolErrorKind.PERMISSION_DENIED
    # A domain safety refusal must NEVER turn into a chat-originated MCP
    # OAuth, connector, or administrator settings authorization request.
    assert result.error.detail["reason"] == "protected_scope"
    assert result.error.recovery_hint["reason"] == "protected_scope"
    assert "operator_review" not in result.error.recovery_hint
    assert not paths.config.exists()
    assert not any(paths.proposals.glob("*"))

    from nerya.agent.loop import (
        _build_protected_scope_rejection_final_text,
        _protected_scope_rejection_data,
    )
    from nerya.agent.tool_projection import project_tool_results

    summary = _protected_scope_rejection_data(result)
    assert summary is not None and "operator_review" not in summary
    assert "不是 MCP 连接或 OAuth 授权请求" in _build_protected_scope_rejection_final_text([summary])
    projection = project_tool_results([result],
        render_tool_result=lambda _: {"type": "tool_result", "content": "permission denied"},
        rendered_tool_result_text=lambda _: None)
    assert "operator_review" not in projection.event_blocks[0]["recovery"]
    assert not projection.approval_blocks


@pytest.mark.parametrize("extra", [
    {"mcp": {"auth_mode": "none"}},
    {"mcp": {"oauth_epoch": "reset-auth"}},
    {"mcp": {"openai_tunnel": {"api_key_ref": "vault://other_key"}}},
    {"trading": {"max_global_exposure_pct": 200}},
    {"llm": {"unsupported_change": True}},
])
def test_protected_mcp_config_does_not_authorize_unsupported_or_mixed_changes(tmp_path, extra) -> None:
    config = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    after = deepcopy(DEFAULT_CONFIG)
    after["mcp"]["enabled"] = not after["mcp"]["enabled"]
    for key, val in extra.items():
        if key == "mcp":
            for subkey, entry in val.items():
                if isinstance(entry, dict):
                    after["mcp"].setdefault(subkey, {}).update(entry)
                else:
                    after["mcp"][subkey] = entry
        else:
            after.setdefault(key, {}).update(val)
    result = evolve_core_config_patch_handler(_protected_mcp_call(after), config=config)
    assert result.is_error and result.error.kind == ToolErrorKind.PERMISSION_DENIED
    assert "operator_review" not in result.error.recovery_hint
    assert "oauth" not in result.error.recovery_hint
    assert not config.paths.config.exists()


def test_rejected_mcp_config_never_exposes_secret_or_authorization_request(tmp_path) -> None:
    config = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    after = deepcopy(DEFAULT_CONFIG)
    after["mcp"]["enabled"] = not after["mcp"]["enabled"]
    after["mcp"]["openai_tunnel"]["api_key_ref"] = "vault://not-for-chat"
    result = evolve_core_config_patch_handler(_protected_mcp_call(after), config=config)
    assert result.is_error and result.error.kind == ToolErrorKind.PERMISSION_DENIED
    assert "not-for-chat" not in str(result.error.recovery_hint)
    assert "operator_review" not in result.error.recovery_hint


def test_partial_provider_patch_preserves_existing_protected_mcp_without_false_reject(tmp_path) -> None:
    """Regression from a real provider-only Agent tool call rejected as MCP."""
    from nerya.core.config import load_config

    paths = WorkspacePaths(root=tmp_path)
    before = {
        "mcp": {"enabled": True, "allow_tools": ["nerya_info"], "auth_mode": "oauth2"},
        "providers": {"existing": {"enabled": True}},
        "runtime": {"kill_switch": False},
    }
    yaml_io.dump(paths.config, before)
    config = load_config(tmp_path)
    result = evolve_core_config_patch_handler(
        _protected_mcp_call({"providers": {"solana": {"enabled": True}}}),
        config=config,
    )

    assert not result.is_error, result.error
    pid = result.content[0].data["proposal"]["id"]
    after = yaml_io.load(paths.proposals / pid / "after/nerya.yml", default={})
    assert after["mcp"] == before["mcp"]
    assert after["runtime"] == before["runtime"]
    assert after["providers"] == {"existing": {"enabled": True}, "solana": {"enabled": True}}
    assert yaml_io.load(paths.config, default={}) == before  # proposal-only


def test_evolve_config_yaml_mapping_string_is_repaired_before_tool_schema(tmp_path) -> None:
    from nerya.core.config import load_config
    from nerya.tools.executor import _repair_arguments_before_validation
    from nerya.tools.native.evolve import EVOLVE_CORE_CONFIG_PATCH_SCHEMA

    paths = WorkspacePaths(root=tmp_path)
    yaml_io.dump(paths.config, {"mcp": {"enabled": True, "allow_tools": ["nerya_info"]}})
    call = _protected_mcp_call("providers:\n  solana:\n    enabled: true\n")
    _repair_arguments_before_validation(call, EVOLVE_CORE_CONFIG_PATCH_SCHEMA)
    assert call.arguments["config_after"] == {"providers": {"solana": {"enabled": True}}}
    result = evolve_core_config_patch_handler(call, config=load_config(tmp_path))
    assert not result.is_error
    assert yaml_io.load(paths.config, default={})["mcp"]["allow_tools"] == ["nerya_info"]


@pytest.mark.parametrize("untrusted", [
    '{"mcp": {"enabled": true,',
    "providers:\n  solana: [unterminated",
    "!!python/object/apply:os.system ['echo should-not-run']",
    "null",
])
def test_evolve_config_invalid_string_never_guesses_missing_yaml(tmp_path, untrusted) -> None:
    from nerya.tools.executor import _repair_arguments_before_validation
    from nerya.tools.native.evolve import EVOLVE_CORE_CONFIG_PATCH_SCHEMA

    call = _protected_mcp_call(untrusted)
    _repair_arguments_before_validation(call, EVOLVE_CORE_CONFIG_PATCH_SCHEMA)
    assert call.arguments["config_after"] == untrusted


def test_explicit_partial_mcp_document_still_cannot_remove_protections(tmp_path) -> None:
    from nerya.core.config import load_config

    paths = WorkspacePaths(root=tmp_path)
    yaml_io.dump(paths.config, {"mcp": {"enabled": True, "allow_tools": ["nerya_info"]}})
    result = evolve_core_config_patch_handler(
        _protected_mcp_call({"mcp": {"enabled": True}}),
        config=load_config(tmp_path),
    )
    assert result.is_error and result.error.kind == ToolErrorKind.PERMISSION_DENIED
    assert "operator_review" not in result.error.recovery_hint
    assert not any(paths.proposals.glob("*"))


def test_evolve_core_config_patch_allows_unchanged_protected_defaults(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    result = evolve_core_config_patch_handler(
        ToolCall(
            name="evolve_core_config_patch",
            arguments={
                "target": "nerya.yml",
                "summary": "Tune safe agent defaults",
                "config_after": {
                    "runtime": {
                        "live_trading_enabled": False,
                        "kill_switch": False,
                    },
                    "llm": {"reasoning_effort": "medium"},
                    "agent": {"max_parallel": 8},
                },
            },
        ),
        config=Config(
            paths=paths,
            data={
                "runtime": {
                    "live_trading_enabled": False,
                    "kill_switch": False,
                },
            },
        ),
    )

    assert not result.is_error
    proposal = result.content[0].data["proposal"]
    assert proposal["kind"] == "core_config_patch"
    assert proposal["target"] == "nerya.yml"
    assert (paths.evolution / "proposals" / proposal["id"] / "after" / "nerya.yml").exists()
