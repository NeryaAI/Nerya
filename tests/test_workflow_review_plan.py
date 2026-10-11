import pytest

from nerya.evolution.strategy_code_generator import StrategyCodeGenerator, StrategyGenerationRequest
from nerya.core.paths import WorkspacePaths
from nerya.core import yaml_io
from nerya.strategies.package import StrategyTuningConfig
from nerya.strategies.workflow_graph import build_workflows
from nerya.core.errors import TradingError

pytestmark = pytest.mark.smoke


def test_review_plan_round_trips_without_enabling_a_schedule():
    raw = {"enabled": False, "review_plan": {"focus": "Observed spread", "next_review": "After sufficient new samples"}}
    result = StrategyTuningConfig.from_dict(raw, where="fixture")
    assert not result.enabled and result.schedule is None
    assert result.asdict()["review_plan"] == raw["review_plan"]
    assert StrategyTuningConfig.from_dict(result.asdict(), where="roundtrip").review_plan == result.review_plan


@pytest.mark.parametrize("plan", [[], {"focus": 42}, {"focus": "a" * 12001}, {str(i): "x" for i in range(21)}])
def test_invalid_plan_is_rejected(plan):
    # An empty list is not a text mapping either.
    with pytest.raises(TradingError):
        StrategyTuningConfig.from_dict({"review_plan": plan}, where="fixture")


@pytest.mark.parametrize("create_tuning", [False, True])
def test_generated_strategy_includes_an_independent_paused_plan(tmp_path, create_tuning):
    request = StrategyGenerationRequest(strategy_id="review_fixture", markets=("BINANCE:BTCUSDT",), accounts=("paper_fixture",),
        execution_mode="script", strategy_class="trend", create_tuning=create_tuning)
    result = StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(request, validate=False, create_proposal_record=False)
    manifest = yaml_io.loads(result.files["strategy.yml"])
    # Creating the review capability is not the same as enabling its schedule.
    assert manifest["tuning"]["enabled"] is create_tuning
    assert "review_fixture" in manifest["tuning"]["review_plan"]["scope"]
    assert not manifest["tuning"].get("schedule", {}).get("enabled")
    graph = build_workflows(result.files)
    plan_node = next(node for node in graph["evolution"]["nodes"] if node["id"] == "proposal:tuning")
    assert plan_node["config"]["review_plan"] == manifest["tuning"]["review_plan"]
    if not create_tuning:
        assert "subagents/strategy_tuner.agent.md" not in result.files
