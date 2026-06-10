from __future__ import annotations

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import create_proposal
from nerya.tools.native.evolve import (
    evolve_core_config_patch_handler,
    evolve_proposals_handler,
)
from nerya.tools.types import ToolCall, ToolErrorKind


pytestmark = pytest.mark.smoke


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
    assert not (tmp_path / "nerya.yml").exists()


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
