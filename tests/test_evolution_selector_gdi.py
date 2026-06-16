from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from nerya.api import routes_evolution
from nerya.core import jsonl
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.assets import create_candidate, promote_candidate, record_capsule_from_proposal
from nerya.evolution.patch_proposal import create_proposal
from nerya.evolution.selector import select_assets_for_signals

pytestmark = pytest.mark.smoke


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_selector_adds_explainable_gdi_and_keeps_negative_capsules(tmp_path):
    paths = WorkspacePaths(tmp_path)
    jsonl.append(
        paths.evolution_capsules,
        {
            "id": "cap_old_positive",
            "gene_id": "gene_nerya_strategy_drawdown_review",
            "summary": "Old positive drawdown fix.",
            "evidence_refs": ["proposal:prp_old"],
            "validation_results": [{"status": "passed"}],
            "outcome_score": 0.9,
            "promotion_ref": "proposal:prp_old",
            "strategy_id": "alpha",
            "ts": _iso_days_ago(120),
        },
        stamp=False,
    )
    jsonl.append(
        paths.evolution_capsules,
        {
            "id": "cap_recent_negative",
            "gene_id": "gene_nerya_strategy_drawdown_review",
            "summary": "Recent rejected widening after post-apply regression.",
            "evidence_refs": ["proposal:prp_bad"],
            "validation_results": [{"status": "failed"}],
            "outcome_score": -0.7,
            "promotion_ref": "proposal:prp_bad",
            "strategy_id": "alpha",
            "ts": _iso_days_ago(1),
        },
        stamp=False,
    )
    jsonl.append(
        paths.journal("evolution"),
        {
            "kind": "proposal.post_apply_observation",
            "proposal_id": "prp_bad",
            "status": "regressed",
            "summary": "post-apply drawdown worsened",
        },
    )
    jsonl.append(
        paths.evolution_events,
        {
            "id": "evt_use_gene",
            "genes_used": ["gene_nerya_strategy_drawdown_review"],
            "metadata": {"capsules_used": ["cap_old_positive"]},
        },
        stamp=False,
    )

    selected = select_assets_for_signals(
        paths,
        [{"kind": "strategy_drawdown", "strategy_id": "alpha"}],
        strategy_id="alpha",
    )

    assert selected["gdi"]["version"] == "gdi_v1"
    gene = selected["genes"][0]
    assert gene["id"] == "gene_nerya_strategy_drawdown_review"
    assert gene["gdi"]["score"] > 0
    assert gene["gdi"]["components"]["intrinsic"] >= gene["confidence"]
    capsules = {row["id"]: row for row in selected["capsules"]}
    assert capsules["cap_recent_negative"]["gdi"]["polarity"] == "negative"
    assert capsules["cap_recent_negative"]["gdi"]["post_apply_status"] == "regressed"
    assert capsules["cap_recent_negative"]["gdi"]["score"] >= 0.55
    assert capsules["cap_old_positive"]["gdi"]["usage_count"] == 1


def test_selector_ignores_asset_candidates_until_promotion(tmp_path):
    paths = WorkspacePaths(tmp_path)
    candidate = create_candidate(
        paths,
        kind="capsule",
        summary="Pending preview lesson",
        payload={
            "id": "cap_pending_preview",
            "gene_id": "gene_nerya_strategy_drawdown_review",
            "summary": "Pending preview lesson",
            "validation_results": [{"status": "passed"}],
            "outcome_score": 1.0,
            "evidence_refs": ["strategy_tuning:tune_pending"],
        },
        evidence_refs=["strategy_tuning:tune_pending"],
        strategy_id="alpha",
    )

    selected = select_assets_for_signals(
        paths,
        [{"kind": "strategy_drawdown", "strategy_id": "alpha"}],
        strategy_id="alpha",
    )

    assert candidate["promotion_gates"]["selector_eligible"] is False
    assert all(row["id"] != "cap_pending_preview" for row in selected["capsules"])

    promoted = promote_candidate(paths, candidate["id"], operator="test")
    assert promoted["ok"] is True
    selected_after = select_assets_for_signals(
        paths,
        [{"kind": "strategy_drawdown", "strategy_id": "alpha"}],
        strategy_id="alpha",
    )

    assert any(row["id"] == "cap_pending_preview" for row in selected_after["capsules"])


def test_selector_gdi_uses_weighted_post_apply_observation_summary(tmp_path):
    paths = WorkspacePaths(tmp_path)
    jsonl.append(
        paths.evolution_capsules,
        {
            "id": "cap_obs",
            "gene_id": "gene_nerya_strategy_drawdown_review",
            "summary": "Drawdown tuning learned from noisy post-apply runs.",
            "evidence_refs": ["proposal:prp_obs"],
            "validation_results": [{"status": "passed"}],
            "outcome_score": 0.0,
            "promotion_ref": "proposal:prp_obs",
            "strategy_id": "alpha",
            "ts": _iso_days_ago(1),
        },
        stamp=False,
    )
    anchor = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    for minutes in range(1, 9):
        jsonl.append(
            paths.journal("evolution"),
            {
                "kind": "proposal.post_apply_observation",
                "proposal_id": "prp_obs",
                "status": "regressed",
                "source": "strategy_run",
                "observed_at": (anchor - timedelta(minutes=minutes)).isoformat(),
                "summary": "same-source runtime regression",
            },
            stamp=False,
        )
    jsonl.append(
        paths.journal("evolution"),
        {
            "kind": "proposal.post_apply_observation",
            "proposal_id": "prp_obs",
            "status": "observing",
            "source": "strategy_run",
            "observed_at": anchor.isoformat(),
            "summary": "latest tick still observing",
        },
        stamp=False,
    )

    selected = select_assets_for_signals(
        paths,
        [{"kind": "strategy_drawdown", "strategy_id": "alpha"}],
        strategy_id="alpha",
    )

    gdi = next(row["gdi"] for row in selected["capsules"] if row["id"] == "cap_obs")
    weighted = gdi["post_apply_weighted"]
    assert gdi["post_apply_status"] == "observing"
    assert gdi["polarity"] == "negative"
    assert gdi["components"]["human"] == 0.85
    assert weighted["count"] == 9
    assert weighted["by_status"] == {"observing": 1, "regressed": 8}
    assert weighted["by_source"] == {"strategy_run": 9}
    assert weighted["weighted_by_source"]["strategy_run"] == 3.0
    assert 2.0 < weighted["weighted_negative_count"] < 3.0
    assert weighted["weighted_observing_count"] < 0.5
    assert weighted["decay"]["source_weight_cap"] == 3.0


def test_selector_prefers_trigger_relevant_capsules(tmp_path):
    paths = WorkspacePaths(tmp_path)
    jsonl.append(
        paths.evolution_capsules,
        {
            "id": "cap_high_vol_match",
            "gene_id": "gene_nerya_market_regime_tuning_review",
            "summary": "High-volatility breakout tuning added confirmation.",
            "evidence_refs": ["proposal:prp_high_vol"],
            "validation_results": [{"status": "passed"}],
            "outcome_score": 0.2,
            "promotion_ref": "proposal:prp_high_vol",
            "strategy_id": "alpha",
            "ts": _iso_days_ago(1),
            "metadata": {
                "trigger_signal_kinds": ["market_regime_high_volatility"],
                "trigger_market_regimes": ["high_volatility"],
                "trigger_markets": ["mock:BTC/USDT"],
                "trigger_timeframes": ["1h"],
            },
        },
        stamp=False,
    )
    jsonl.append(
        paths.evolution_capsules,
        {
            "id": "cap_rangebound_mismatch",
            "gene_id": "gene_nerya_market_regime_tuning_review",
            "summary": "Range-bound tuning narrowed entries.",
            "evidence_refs": ["proposal:prp_range"],
            "validation_results": [{"status": "passed"}],
            "outcome_score": 1.0,
            "promotion_ref": "proposal:prp_range",
            "strategy_id": "alpha",
            "ts": _iso_days_ago(1),
            "metadata": {
                "trigger_signal_kinds": ["market_regime_rangebound"],
                "trigger_market_regimes": ["rangebound"],
                "trigger_markets": ["mock:BTC/USDT"],
                "trigger_timeframes": ["1h"],
            },
        },
        stamp=False,
    )

    selected = select_assets_for_signals(
        paths,
        [
            {
                "kind": "market_regime_high_volatility",
                "strategy_id": "alpha",
                "metadata": {"markets": ["mock:BTC/USDT"], "timeframe": "1h"},
            }
        ],
        strategy_id="alpha",
    )

    capsule_ids = [row["id"] for row in selected["capsules"]]
    assert capsule_ids.index("cap_high_vol_match") < capsule_ids.index("cap_rangebound_mismatch")
    matched = next(row for row in selected["capsules"] if row["id"] == "cap_high_vol_match")
    mismatched = next(row for row in selected["capsules"] if row["id"] == "cap_rangebound_mismatch")
    assert matched["gdi"]["matched_signals"] == ["market_regime_high_volatility"]
    assert matched["gdi"]["components"]["relevance"] > mismatched["gdi"]["components"]["relevance"]
    assert matched["gdi"]["relevance"]["matched_context"]["market_regimes"] == ["high_volatility"]


def test_capsule_from_proposal_preserves_trigger_context(tmp_path):
    paths = WorkspacePaths(tmp_path)
    proposal = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="High volatility tuning",
        metadata={
            "strategy_id": "alpha",
            "package_hash": "pkg_hash",
            "evolution_trigger_context": {
                "signal_kinds": ["strategy_tuning_run", "market_regime_high_volatility"],
                "market_regimes": ["high_volatility"],
                "markets": ["mock:BTC/USDT"],
                "timeframes": ["1h"],
                "data_quality": ["degraded"],
                "selected_gene_ids": ["gene_nerya_market_regime_tuning_review"],
                "selected_capsule_ids": ["cap_prior"],
                "evidence_refs": ["strategy_tuning:tune_1"],
            },
        },
        evidence_refs=["strategy_tuning:tune_1"],
    )

    capsule = record_capsule_from_proposal(paths, proposal.id, outcome_score=1.0)

    assert capsule is not None
    assert capsule["strategy_id"] == "alpha"
    assert capsule["gene_id"] == "gene_nerya_market_regime_tuning_review"
    metadata = capsule["metadata"]
    assert metadata["trigger_signal_kinds"] == [
        "strategy_tuning_run",
        "market_regime_high_volatility",
    ]
    assert metadata["trigger_market_regimes"] == ["high_volatility"]
    assert metadata["trigger_markets"] == ["mock:BTC/USDT"]
    assert metadata["trigger_timeframes"] == ["1h"]
    assert metadata["trigger_data_quality"] == ["degraded"]
    assert metadata["selected_capsule_ids"] == ["cap_prior"]
    assert metadata["package_hash"] == "pkg_hash"


def test_assets_route_returns_gdi_breakdown(tmp_path):
    paths = WorkspacePaths(tmp_path)
    client = SimpleNamespace(config=Config(paths=paths, data={}))
    assets_route = next(
        handler
        for method, path, handler in routes_evolution.routes()
        if method == "POST" and path == "/evolution/assets"
    )

    out = assets_route(client, {"kind": "gene", "limit": 5})

    assert out["assets"]
    assert all("gdi" in row for row in out["assets"])
    assert {"intrinsic", "usage", "human", "freshness"} <= set(
        out["assets"][0]["gdi"]["components"]
    )
