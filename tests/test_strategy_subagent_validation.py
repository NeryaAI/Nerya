from __future__ import annotations

import pytest

from nerya.strategies.validator import validate_proposal_files


pytestmark = pytest.mark.smoke


def _files(*, mode: str = "paper", require_subagent: bool = False) -> dict[str, str]:
    require_text = "true" if require_subagent else "false"
    return {
        "strategy.yml": f"""
version: 1
strategy_id: no_subagent_required
title: No Subagent Required
mode: {mode}
entrypoint: main.py:run
markets: ["paper:BTCUSDT"]
accounts: [paper_main]
schedule:
  type: cron
  cron: "*/5 * * * *"
  enabled: true
policy:
  allow_direct_order: true
  require_subagent_before_order: {require_text}
  max_single_order_usd: 100
  min_confidence: 0.5
llm_policy:
  default_tier: light
  allowed_tiers: [light]
  max_calls_per_run: 1
subagents: []
""",
        "main.py": "def run(ctx):\n    return {'decision': 'HOLD'}\n",
    }


def _files_with_main(main_py: str) -> dict[str, str]:
    files = _files()
    files["main.py"] = main_py
    return files


def test_missing_subagent_is_not_a_validation_blocker() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files(require_subagent=True),
    )

    assert result.ok
    codes = {issue.code for issue in result.issues}
    assert "missing_required_subagent" not in codes


def test_live_strategy_without_subagent_is_not_warned() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files(mode="live"),
    )

    assert result.ok
    codes = {issue.code for issue in result.issues}
    assert "live_without_subagent" not in codes


def test_backtest_incompatible_strategy_context_surfaces_are_blocked() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files_with_main(
            "\n".join(
                [
                    "def run(context):",
                    "    candles = context.market_data.get_candles(market='paper:BTCUSDT', interval='1h', count=50)",
                    "    account_id = context.account_id",
                    "    account = context.portfolio.get_account(account_id)",
                    "    positions = context.portfolio.get_positions('paper:BTCUSDT')",
                    "    return {'decision': 'HOLD', 'rows': len(candles), 'positions': len(positions), 'account': account_id}",
                ]
            )
        ),
    )

    assert not result.ok
    blockers = [issue for issue in result.blockers if issue.code == "unsupported_strategy_context_surface"]
    assert len(blockers) == 4
    message = "\n".join(issue.message for issue in blockers)
    assert "ctx.market.candles" in message
    assert "ctx.portfolio.positions" in message
    assert "ctx.config.accounts[0]" in message
