from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.api import routes_gateway
from nerya.api.gateway_commands import CommandContext, DEFAULT_REGISTRY, menu_commands
from nerya.api.route_scopes import required_scope
from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths


pytestmark = pytest.mark.smoke


def _client(tmp_path):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})
    return SimpleNamespace(config=cfg)


def _ctx(tmp_path, raw_text: str) -> CommandContext:
    return CommandContext(
        client=_client(tmp_path),
        platform="telegram",
        chat_id="chat-1",
        session_id="session-1",
        raw_text=raw_text,
    )


def _write_strategy(tmp_path, strategy_id: str = "alpha") -> None:
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})
    root = cfg.paths.strategy(strategy_id)
    yaml_io.dump(
        root / "strategy.yml",
        {
            "id": strategy_id,
            "title": "Alpha rotation",
            "status": "paper",
            "mode": "paper",
            "account_id": "paper_main",
            "markets": ["mock:BTC/USDT", "mock:ETH/USDT"],
            "trigger_kinds": ["schedule"],
            "subagents": ["risk_reviewer"],
            "paper_trading_enabled": True,
            "live_trading_enabled": False,
        },
    )
    yaml_io.dump(
        root / "limits.yml",
        {
            "min_confidence": 0.7,
            "max_single_order_usd": 250,
            "approval_threshold_usd": 1000,
        },
    )


def _write_account(tmp_path) -> None:
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "paper_main",
                    "exchange": "mock",
                    "venue": "mock",
                    "mode": "paper",
                    "status": "active",
                    "initial_balance_usd": 12345,
                }
            ]
        },
    )


def test_gateway_menu_includes_strategy_and_portfolio_commands():
    commands = {row["command"] for row in menu_commands(platform="telegram")}

    assert {"strategies", "accounts", "portfolio", "workflows"}.issubset(commands)


def test_gateway_workflows_command_describes_schedule_surface(tmp_path):
    outcome = DEFAULT_REGISTRY.handle("/workflows", _ctx(tmp_path, "/workflows"))

    assert outcome.handled is True
    assert outcome.command == "/workflows"
    assert "Workflows" in outcome.reply_text
    assert "schedule" in outcome.reply_text
    assert "调度" in outcome.reply_text


def test_gateway_strategies_lists_and_describes_workspace_strategies(tmp_path):
    _write_strategy(tmp_path)

    list_out = DEFAULT_REGISTRY.handle("/strategies", _ctx(tmp_path, "/strategies"))
    detail_out = DEFAULT_REGISTRY.handle("/strategies alpha", _ctx(tmp_path, "/strategies alpha"))

    assert list_out.handled is True
    assert "`alpha`" in list_out.reply_text
    assert "mock:BTC/USDT" in list_out.reply_text
    assert detail_out.handled is True
    assert "Strategy `alpha`" in detail_out.reply_text
    assert "min_confidence=0.7" in detail_out.reply_text
    assert "risk_reviewer" in detail_out.reply_text


def test_gateway_portfolio_summary_command_reads_accounts(tmp_path):
    _write_account(tmp_path)

    outcome = DEFAULT_REGISTRY.handle("/portfolio", _ctx(tmp_path, "/portfolio"))

    assert outcome.handled is True
    assert "Portfolio summary" in outcome.reply_text
    assert "paper_main" in outcome.reply_text
    assert "12,345.00" in outcome.reply_text


def test_gateway_commands_route_exports_shared_registry(tmp_path):
    client = _client(tmp_path)
    route_map = {(method, path): handler for method, path, handler in routes_gateway.routes()}

    result = route_map[("GET", "/gateway/commands")](client, {"platform": "discord"})

    assert result["ok"] is True
    commands = {row["command"] for row in result["commands"]}
    assert {"strategies", "accounts", "portfolio"}.issubset(commands)
    assert required_scope("GET", "/gateway/commands") == "read:runtime"
