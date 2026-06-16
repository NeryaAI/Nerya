from nerya.evolution.lineage_graph import build_lineage_graph


def test_lineage_graph_connects_signals_assets_validation_and_outcome():
    graph = build_lineage_graph(
        {
            "id": "prp_alpha",
            "kind": "strategy_tuning_proposal",
            "state": "applied",
            "summary": "Tighten high volatility entries",
            "ts": "2026-06-17T00:00:00Z",
            "target": "strategies/alpha",
            "evidence_refs": ["strategy_tuning:tune_alpha"],
            "source_event_id": "evt_alpha",
            "validation_plan_id": "vpl_alpha",
            "metadata": {"strategy_id": "alpha"},
        },
        validation_plan={
            "id": "vpl_alpha",
            "status": "passed",
            "last_run_id": "vrn_alpha",
            "last_run_at": "2026-06-17T00:05:00Z",
            "steps": [
                {
                    "type": "backtest",
                    "status": "passed",
                    "required": True,
                    "evidence_ref": "validation:vrn_alpha:step:0",
                }
            ],
        },
        backtest_comparison={
            "status": "complete",
            "summary": "Return improved and drawdown fell.",
            "strategy_id": "alpha",
            "before": {"backtest_id": "before"},
            "after": {"backtest_id": "after"},
            "metrics_delta": [{"key": "total_return_pct", "delta": 2.0}],
            "evidence_refs": ["file:strategies/alpha/backtests/after/metrics.json"],
        },
        post_apply_monitor={
            "status": "healthy",
            "summary": "Paper run stayed healthy.",
            "observed_at": "2026-06-17T00:10:00Z",
            "evidence_refs": ["journal:evolution:12"],
            "observations": [
                {
                    "id": "obs_alpha",
                    "status": "healthy",
                    "source": "paper",
                    "summary": "No regression.",
                    "evidence_refs": ["journal:evolution:12"],
                }
            ],
        },
        why_reused={
            "selection_signals": [
                {
                    "id": "sig_vol",
                    "kind": "market_regime_high_volatility",
                    "severity": "warn",
                    "summary": "Volatility increased.",
                    "evidence_refs": ["journal:agent:8"],
                }
            ],
            "genes": [
                {
                    "id": "gene_regime",
                    "summary": "Use stricter filters in high volatility.",
                    "evidence_refs": ["gene:gene_regime"],
                    "gdi_score": 0.81,
                }
            ],
            "capsules": [
                {
                    "id": "cap_filter",
                    "summary": "Tightened alpha filter worked before.",
                    "evidence_refs": ["capsule:cap_filter"],
                    "relevance_score": 0.92,
                }
            ],
            "negative_capsules": [
                {
                    "id": "cap_leverage_bad",
                    "summary": "Avoid adding leverage during news spikes.",
                    "evidence_refs": ["capsule:cap_leverage_bad"],
                    "polarity": "negative",
                }
            ],
            "proposal_diff": {
                "paths": ["strategies/alpha/main.py"],
                "change_count": 1,
            },
        },
        action_gates={"can_apply": True, "blockers": [], "evidence": {"refs": ["validation:vrn_alpha:step:0"]}},
    )

    node_types = {node["type"] for node in graph["nodes"]}
    edge_types = {edge["type"] for edge in graph["edges"]}

    assert graph["version"] == "lineage_graph_v1"
    assert graph["root_id"] == "proposal:prp_alpha"
    assert {
        "signal",
        "gene",
        "capsule",
        "negative_capsule",
        "proposal",
        "file_change",
        "validation_plan",
        "validation_run",
        "validation_step",
        "backtest_comparison",
        "apply",
        "post_apply_monitor",
        "post_apply_observation",
    } <= node_types
    assert {
        "triggered",
        "matched",
        "selected",
        "cautioned",
        "proposed_change",
        "requires_validation",
        "executed_as",
        "validated_by",
        "observed_by",
    } <= edge_types
    assert "journal:agent:8" in graph["evidence_refs"]
    assert "validation:vrn_alpha:step:0" in graph["evidence_refs"]
