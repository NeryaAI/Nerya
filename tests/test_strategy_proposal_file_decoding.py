from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import Proposal, create_proposal
from nerya.sdk.strategy_api import StrategyAPI
from nerya.tools.native.strategy_runtime import strategy_validate_handler
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def _proposal_with_bad_utf8_file(cfg: Config) -> Proposal:
    strategy_id = "decode_safe"
    proposal = create_proposal(
        cfg.paths,
        kind="strategy_package_proposal",
        summary="Decode-safe strategy proposal",
        extra_files={
            f"after/strategies/{strategy_id}/strategy.yml": f"""
version: 1
strategy_id: {strategy_id}
title: Decode Safe
mode: paper
entrypoint: main.py:run
markets: ["paper:BTCUSDT"]
accounts: [paper_main]
schedule:
  type: cron
  cron: "*/5 * * * *"
  enabled: true
policy:
  allow_direct_order: true
  require_subagent_before_order: false
  max_single_order_usd: 100
  min_confidence: 0.5
llm_policy:
  default_tier: light
  allowed_tiers: [light]
  max_calls_per_run: 1
subagents: []
""",
            f"after/strategies/{strategy_id}/main.py": (
                "def run(ctx):\n    return {'decision': 'HOLD'}\n"
            ),
            f"after/strategies/{strategy_id}/notes.md": "placeholder\n",
        },
        initial_state="pending_review",
    )
    notes = proposal.path / "after" / "strategies" / strategy_id / "notes.md"
    notes.write_bytes(b"\xf3bad utf8 auxiliary note")
    return proposal


def test_strategy_sdk_validate_tolerates_non_utf8_proposal_files(tmp_path) -> None:
    cfg = _config(tmp_path)
    proposal = _proposal_with_bad_utf8_file(cfg)

    result = StrategyAPI(config=cfg, skills=None).validate(proposal_id=proposal.id)  # type: ignore[arg-type]

    assert result["ok"] is True


def test_native_strategy_validate_tolerates_non_utf8_proposal_files(tmp_path) -> None:
    cfg = _config(tmp_path)
    proposal = _proposal_with_bad_utf8_file(cfg)

    result = strategy_validate_handler(
        ToolCall(
            name="strategy.validate",
            arguments={"proposal_id": proposal.id},
        ),
        config=cfg,
    )

    assert result.is_error is False
    assert result.content[0].data["ok"] is True
