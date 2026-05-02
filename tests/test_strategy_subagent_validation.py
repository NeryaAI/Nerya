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
