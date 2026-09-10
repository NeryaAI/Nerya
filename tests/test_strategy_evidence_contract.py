"""A review must use observations, not fabricate causes or missing measurements."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.strategy_history import store
from nerya.strategy_history.attribution import (
    attribute_session, execution_quality, indicator_sensitivity, paper_vs_live_divergence,
)
from nerya.strategy_history.review import StrategyReviewer

pytestmark = pytest.mark.smoke


def test_attribution_preserves_risk_evidence_without_inventing_a_cause(tmp_path):
    paths = WorkspacePaths(tmp_path)
    store.record_trigger(paths, strategy_id="alpha", session_id="s1", event={"name": "rebalance"})
    store.record_intent(paths, strategy_id="alpha", session_id="s1", intent={"intent_id": "i1"})
    store.record_risk(paths, strategy_id="alpha", session_id="s1",
                      decision={"decision": "reject", "reason": "configured exposure limit"})
    bundle = attribute_session(paths, "alpha", "s1").as_dict()
    assert "root_causes" not in bundle
    assert "proposal_seeds" not in bundle
    assert bundle["counts"]["fills"] == 0
    assert bundle["pnl_usd"] is None
    assert bundle["evidence"]["risk"][0]["risk_decision"]["reason"] == "configured exposure limit"
    assert bundle["observations"]
    assert "risk_limit_suggestion" not in json.dumps(bundle)
    assert bundle["evidence_refs"]


def test_missing_execution_metrics_are_not_perfect_zero_cost_execution(tmp_path):
    paths = WorkspacePaths(tmp_path)
    store.record_fill(paths, strategy_id="alpha", session_id="s1", fill={"market": "TEST"})
    measured = execution_quality(paths, "alpha", "s1")
    assert measured["slippage_bps"]["p50"] is None
    assert measured["latency_ms"]["p50"] is None
    assert measured["slippage_bps"]["samples"] == 0
    assert "avg_quality" not in measured
    assert "score" not in measured["per_fill"][0]


def test_unknown_mode_is_not_silently_paper(tmp_path):
    paths = WorkspacePaths(tmp_path)
    store.record_pnl(paths, strategy_id="alpha", session_id="unclassified", pnl={"realized_usd": 100})
    measured = paper_vs_live_divergence(paths, "alpha")
    assert measured["paper_sessions"] == measured["live_sessions"] == 0
    assert measured["unclassified_sessions"] == 1
    assert measured["paper_mean_pnl_usd"] is None
    assert measured["divergence_usd"] is None
    assert paper_vs_live_divergence(paths, "alpha", window_sessions=0)["unclassified_sessions"] == 0


def test_partial_pnl_events_are_aggregated_before_indicator_association(tmp_path):
    paths = WorkspacePaths(tmp_path)
    store.record_decision(paths, strategy_id="alpha", session_id="s1",
                          decision={"order_id": "o1", "features": {"volatility": 3.0}})
    for pnl in (10, -2):
        store.record_pnl(paths, strategy_id="alpha", session_id="s1", pnl={"order_id": "o1", "realized_usd": pnl})
    result = indicator_sensitivity(paths, "alpha", "s1")["indicators"][0]
    assert result["win_samples"] == 1
    assert result["loss_samples"] == 0
    assert result["delta"] is None


def test_trade_review_receives_evidence_before_call_without_forced_tier(tmp_path):
    paths = WorkspacePaths(tmp_path)
    store.record_risk(paths, strategy_id="alpha", session_id="s1",
                      decision={"decision": "reject", "reason": "unique-exposure-evidence"})
    store.record_pnl(paths, strategy_id="alpha", session_id="s1", pnl={"realized_usd": -1234})
    calls = []
    def call(**kwargs):
        calls.append(kwargs)
        assert "unique-exposure-evidence" in kwargs["prompt"]
        assert "-1234" in kwargs["prompt"]
        assert kwargs["tier"] == "light"
        return SimpleNamespace(tier="light", task=kwargs["task"], raw="", parsed={"summary": "inspect the recorded risk decision"})
    result = StrategyReviewer(Config(paths=paths), SimpleNamespace(call=call)).review_trade(
        "alpha", "s1", stage="close", tier="light",
    )
    assert result["llm_tier"] == "light"
    assert result["attribution"]["evidence_refs"]
    assert len(calls) == 1


def test_empty_reflection_does_not_create_permanent_learning(tmp_path):
    from nerya.evolution.reflection_engine import run_reflection
    from nerya.memory.runtime import MemoryRuntime
    paths = WorkspacePaths(tmp_path)
    report = run_reflection(paths)
    assert report["ok"]
    assert MemoryRuntime(Config(paths=paths)).store.projection_records(actor_id="default") == []


def test_scenario_loss_cap_does_not_fabricate_counterfactual_profit(tmp_path):
    from nerya.strategy_history.scenario_replay import scenario_replay
    paths = WorkspacePaths(tmp_path)
    store.record_pnl(paths, strategy_id="alpha", session_id="s1", pnl={"realized_usd": -100})
    report = scenario_replay(paths, "alpha", "s1", overrides={"daily_loss_cap_usd": 5}).asdict()
    assert report["baseline"]["pnl_usd"] == -100
    assert report["projection"]["pnl_usd"] is None
    assert report["deltas"]["pnl_usd"] is None
    assert "not_simulated" in report["projection"]["status"]


def test_scenario_unknown_execution_is_not_treated_as_zero_latency(tmp_path):
    from nerya.strategy_history.scenario_replay import scenario_replay
    paths = WorkspacePaths(tmp_path)
    store.record_fill(paths, strategy_id="alpha", session_id="s1", fill={"order_id": "o1"})
    report = scenario_replay(paths, "alpha", "s1", overrides={"latency_ms_cap": 1}).asdict()
    assert report["projection"]["fills"] == 0
    assert report["dropped"][0]["reason"] == "missing_latency_ms"
    assert report["dropped"][0]["observed"] is None


def test_removed_scenario_score_is_not_silently_accepted(tmp_path):
    from nerya.strategy_history.scenario_replay import scenario_replay
    with pytest.raises(TypeError):
        scenario_replay(WorkspacePaths(tmp_path), "alpha", "s1", overrides={"min_fill_score": 0.8})
