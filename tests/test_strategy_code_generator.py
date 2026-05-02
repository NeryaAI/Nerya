from __future__ import annotations

from pathlib import Path

import pytest

from nerya.core import yaml_io
from nerya.core.paths import WorkspacePaths
from nerya.evolution.strategy_code_generator import (
    StrategyCodeGenerator,
    StrategyGenerationRequest,
)

pytestmark = pytest.mark.smoke


def test_strategy_generator_normalizes_natural_language_tuning_objectives(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="amzn_daily_team_long",
        title="AMZN daily team long",
        strategy_class="trend",
        markets=("yahoo:AMZN",),
        accounts=("paper_main",),
        tuning_objectives=(
            "Increase risk-adjusted returns without raising drawdown above 20%",
        ),
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    manifest = yaml_io.loads(out.files["strategy.yml"])
    assert out.validation is not None
    assert out.validation.ok
    assert manifest["tuning"]["objectives"] == [
        "risk_adjusted_return",
        "drawdown",
        "return",
    ]


def test_strategy_generate_tool_and_handler_share_tuning_default():
    """Guard against schema/default drift without importing native bootstrap."""

    root = Path(__file__).resolve().parents[1]
    source = (root / "nerya" / "tools" / "native" / "strategy_runtime.py").read_text(
        encoding="utf-8",
    )

    assert '"default": True' in source
    assert 'create_tuning=bool(args.get("create_tuning", True))' in source
