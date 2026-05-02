import importlib.util
from pathlib import Path


def _load_demo():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "run_hackathon_demo_validation.py"
    spec = importlib.util.spec_from_file_location("run_hackathon_demo_validation", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_payloads_cover_short_and_long_agent_strategies():
    demo = _load_demo()

    payloads = {p["strategy_id"]: p for p in demo._strategy_payloads()}

    short = payloads[demo.SHORT_ID]
    assert short["status"] == "paper"
    assert "candle.5m.close" in short["trigger_kinds"]
    assert short["config"]["demo_lane"] == "short_cycle"
    assert short["config"]["timeframe"] == "5m"
    assert "risk_critic" in short["subagents"]

    long = payloads[demo.LONG_ID]
    assert long["status"] == "draft"
    assert "daily.research" in long["trigger_kinds"]
    assert "agent.analysis.ready" in long["trigger_kinds"]
    assert long["config"]["requires_agent_team_memo"] is True
    assert {"technical_analyst", "risk_critic", "portfolio_manager"}.issubset(
        set(long["subagents"])
    )


def test_demo_summary_checks_require_frontend_team_and_evolution_evidence():
    demo = _load_demo()

    dashboard = {
        "api": {
            "list": {"body": {"strategies": [{"id": demo.SHORT_ID}, {"id": demo.LONG_ID}]}},
            "short": {"body": {
                "strategy": {
                    "status": "paper",
                    "trigger_kinds": ["candle.5m.close", "risk.guard"],
                    "subagents": ["risk_critic"],
                },
                "config": {"demo_lane": "short_cycle", "timeframe": "5m", "execution_policy": "paper_only"},
                "prompts": {"main": "短周期 5m paper"},
            }},
            "long": {"body": {
                "strategy": {
                    "status": "draft",
                    "trigger_kinds": ["daily.research", "agent.analysis.ready"],
                    "subagents": ["technical_analyst", "risk_critic", "portfolio_manager"],
                },
                "config": {"requires_agent_team_memo": True},
                "prompts": {"main": "Agent Team，没有 team memo 不允许下单，审批"},
            }},
        },
        "pages": {
            "/dashboard": {"status": 200, "len": 2000},
            "/strategies": {"status": 200, "len": 2000},
            f"/strategies/{demo.SHORT_ID}": {"status": 200, "len": 2000},
            f"/strategies/{demo.LONG_ID}": {"status": 200, "len": 2000},
            "/self-evolution": {"status": 200, "len": 2000},
        },
        "proxy": {
            "strategies": {"body": {"strategies": [{"id": demo.SHORT_ID}, {"id": demo.LONG_ID}]}},
        },
        "browser": {"ok": True},
    }
    team = {
        "run": {"body": {"ok": True, "run": {"id": "team-1"}}},
        "detail": {"body": {
            "run": {"status": "completed", "template_id": "market_analysis_team"},
            "tasks": [
                {"owner": "technical-analyst", "status": "completed"},
                {"owner": "sentiment-analyst", "status": "completed"},
                {"owner": "risk-critic", "status": "completed"},
                {"owner": "market-lead", "status": "completed"},
            ],
            "events": [{"kind": "phase.enter"}, {"kind": "gates.evaluated"}, {"kind": "run.completed"}],
            "blackboard": [{"kind": "signal"}, {"kind": "evidence"}, {"kind": "risk"}],
            "artifacts": [{}, {}, {}, {}],
            "final_report": "BTC consensus tasks gates evidence",
        }},
    }
    evolution = {
        "signals": {"body": {"signals": [{"id": "s1"}]}},
        "reflect": {"body": {"proposal": {"id": "p1"}}},
        "proposals": {"body": {"proposals": [{
            "kind": "learning_update",
            "validation_plan_id": "v1",
            "evidence_refs": [demo.SHORT_ID, demo.LONG_ID],
        }]}},
        "timeline": {"body": {"timeline": [
            {"stage": "signal", "strategy_id": demo.SHORT_ID},
            {"stage": "proposal", "strategy_id": demo.LONG_ID},
            {"stage": "validation", "summary": demo.SHORT_ID},
        ]}},
    }

    summary = demo._summarize(
        strategies=[],
        team=team,
        evolution=evolution,
        dashboard=dashboard,
    )

    assert summary["ok"] is True
    assert summary["checks"]["short_config_declares_short_cycle"] is True
    assert summary["checks"]["long_requires_team_memo"] is True
    assert summary["checks"]["team_blackboard_has_signal_evidence_risk"] is True
    assert summary["checks"]["evolution_is_proposal_first"] is True
