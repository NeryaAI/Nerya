from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from nerya.api import routes_evolution
from nerya.core import jsonl
from nerya.core.time import reset_clock, set_clock
from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution import assets as evolution_assets
from nerya.evolution.patch_proposal import create_proposal
from nerya.evolution.post_apply_observation import record_post_apply_observation
from nerya.evolution.promotion import apply_proposal
from nerya.evolution.promotion import proposal_action_gates
from nerya.evolution.timeline import proposal_why_reused
from nerya.evolution.validation_plan import (
    build_validation_plan,
    load_validation_plan,
    run_validation_plan,
    write_validation_plan,
)
from nerya.llm.gateway import LLMCall
from nerya.strategies.package import load_package
from nerya.strategies.evolution import (
    StrategyEvolutionRunner,
    _filter_changes,
    _materialize_strategy_tuning_after_files,
    _score_optimizer_outcome_feedback,
    _tuning_asset_selection_signals,
    _validation_plan_input,
)
from nerya.subagents.runtime import SubAgentRuntime
from nerya.skills.builtin.backtest.scripts.data_cache import NoHistoricalDataError
import pytest

pytestmark = pytest.mark.smoke


class _Guardrails:
    max_patch_files = 2


class _Cfg:
    forbidden_targets = ["limits.yml", "accounts/*"]
    allowed_targets = ["main.py", "config.yml"]
    guardrails = _Guardrails()


def _seed_strategy(paths: WorkspacePaths, strategy_id: str = "alpha"):
    root = paths.strategy(strategy_id)
    yaml_io.dump(
        root / "strategy.yml",
        {
            "version": 1,
            "strategy_id": strategy_id,
            "title": "Alpha strategy",
            "mode": "paper",
            "entrypoint": "main.py:run",
            "markets": ["mock:BTC/USDT"],
            "accounts": ["paper_main"],
            "schedule": {"type": "cron", "cron": "*/5 * * * *"},
            "subagents": ["strategy_tuner"],
            "tuning": {
                "enabled": True,
                "schedule": {"type": "cron", "cron": "0 */6 * * *"},
                "objectives": ["return"],
                "proposal_policy": {"allowed_targets": ["main.py", "strategy.yml"]},
                "guardrails": {"require_backtest": False},
            },
        },
    )
    (root / "main.py").write_text(
        "def run(ctx):\n    return {'ok': True}\n",
        encoding="utf-8",
    )
    return load_package(paths, strategy_id)


def test_strategy_tuning_requires_validation_plan_when_changes_exist():
    output = {"proposed_changes": [{"file": "main.py", "kind": "code_patch"}]}
    accepted, _dropped, _warnings = _filter_changes(output, _Cfg())
    plan = build_validation_plan(output.get("validation_plan"), source="test", require=bool(accepted))

    assert accepted
    assert plan.status == "blocked"
    assert "validation_plan_required" in plan.blocked_reasons


def test_strategy_tuning_drops_forbidden_targets():
    output = {"proposed_changes": [{"file": "limits.yml", "kind": "config"}]}
    accepted, dropped, _warnings = _filter_changes(output, _Cfg())

    assert accepted == []
    assert dropped[0]["reason"] == "forbidden_target"


def test_strategy_tuning_accepts_legacy_proposed_patches_alias():
    output = {"proposed_patches": [{"file": "main.py", "kind": "code_patch"}]}
    accepted, dropped, _warnings = _filter_changes(output, _Cfg())

    assert accepted == [{"file": "main.py", "kind": "code_patch"}]
    assert dropped == []


def test_strategy_tuning_derives_validation_plan_from_required_flags():
    raw = _validation_plan_input(
        {"backtest_required": True, "shadow_run_required": True}
    )
    plan = build_validation_plan(raw, source="test", require=True)

    assert [step.type for step in plan.steps] == [
        "unit_test",
        "backtest",
        "shadow_run",
    ]
    assert plan.blocked_reasons == []


def test_validation_plan_run_executes_unit_test_and_writes_artifact(tmp_path):
    paths = WorkspacePaths(tmp_path)
    sample = tmp_path / "test_validation_sample.py"
    sample.write_text("def test_sample():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    plan = build_validation_plan(
        [{"type": "unit_test", "command": f"python -m pytest {sample} -q"}],
        source="test",
        strategy_id="alpha",
    )
    plan_id = write_validation_plan(paths, plan)

    result = run_validation_plan(paths, plan_id=plan_id, dry_run=False)

    assert result["ok"] is True
    assert result["dry_run"] is False
    assert result["status"] == "passed"
    run_id = result["validation_run_id"]
    run_path = paths.evolution / "validation_runs" / f"{run_id}.json"
    assert run_path.exists()
    assert result["run"]["steps"][0]["status"] == "passed"
    assert result["run"]["steps"][0]["evidence_ref"] == f"validation:{run_id}:step:0"
    updated = load_validation_plan(paths, plan_id)
    assert updated["status"] == "passed"
    assert updated["steps"][0]["status"] == "passed"
    assert updated["steps"][0]["evidence_ref"] == f"validation:{run_id}:step:0"


def test_validation_plan_run_executes_backtest_step_with_proposal_id(tmp_path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    metrics = paths.strategy("alpha") / "backtests" / "bt1" / "metrics.json"
    report = metrics.with_name("report.md")
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_text('{"verdict": "PASS"}', encoding="utf-8")
    report.write_text("# Backtest\n", encoding="utf-8")

    calls: list[dict[str, object]] = []

    def fake_backtest(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        return {
            "ok": True,
            "strategy_id": "alpha",
            "proposal_id": kwargs.get("proposal_id"),
            "backtest_ts": "bt1",
            "verdict": "PASS",
            "coverage_ok": True,
            "total_return_pct": 3.2,
            "max_drawdown_pct": 1.1,
            "metrics_path": str(metrics),
            "report_path": str(report.relative_to(paths.root)),
            "out_dir": str(metrics.parent),
        }

    monkeypatch.setattr(
        "nerya.evolution.validation_plan.run_strategy_backtest",
        fake_backtest,
    )
    plan = build_validation_plan(
        [{"type": "backtest", "required": True, "preset": "default", "allow_mock": True}],
        source="test",
        proposal_id="prp_alpha",
        strategy_id="alpha",
    )
    plan_id = write_validation_plan(paths, plan)

    result = run_validation_plan(paths, plan_id=plan_id, dry_run=False)

    assert result["ok"] is True
    assert result["status"] == "passed"
    assert calls == [
        {
            "proposal_id": "prp_alpha",
            "strategy_id": None,
            "preset": "default",
            "config_path": None,
            "workspace": paths.root,
            "allow_mock": False,
        }
    ]
    step = result["run"]["steps"][0]
    assert step["type"] == "backtest"
    assert step["status"] == "passed"
    assert step["allow_mock"] is False
    assert step["requested_allow_mock"] is True
    assert step["backtest_result"]["verdict"] == "PASS"
    assert any(artifact["kind"] == "backtest_metrics" for artifact in step["artifacts"])
    assert "post_apply_observation" not in step
    updated = load_validation_plan(paths, plan_id)
    assert updated["status"] == "passed"
    assert updated["steps"][0]["status"] == "passed"


def test_validation_backtest_records_post_apply_observation_for_applied_proposal(tmp_path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="applied learning",
        initial_state="applied",
        evidence_refs=["turn:t_apply"],
        metadata={"strategy_id": "alpha"},
    )
    metrics = paths.strategy("alpha") / "backtests" / "bt_post" / "metrics.json"
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_text('{"verdict": "PASS"}', encoding="utf-8")

    def fake_backtest(**kwargs):  # noqa: ANN003, ANN202
        return {
            "ok": True,
            "strategy_id": "alpha",
            "proposal_id": kwargs.get("proposal_id"),
            "backtest_ts": "bt_post",
            "verdict": "PASS",
            "coverage_ok": True,
            "total_return_pct": 2.4,
            "max_drawdown_pct": 0.8,
            "metrics_path": str(metrics),
            "out_dir": str(metrics.parent),
        }

    monkeypatch.setattr(
        "nerya.evolution.validation_plan.run_strategy_backtest",
        fake_backtest,
    )
    plan = build_validation_plan(
        [{"type": "backtest", "required": True}],
        source="test",
        proposal_id=proposal.id,
        strategy_id="alpha",
    )
    plan_id = write_validation_plan(paths, plan)

    result = run_validation_plan(paths, plan_id=plan_id, dry_run=False)

    assert result["ok"] is True
    step = result["run"]["steps"][0]
    observation = step["post_apply_observation"]
    assert observation["status"] == "healthy"
    assert observation["journal_ref"].startswith("journal:evolution:")
    assert step["evidence_ref"] in observation["evidence_refs"]
    assert any(ref.startswith("file:") for ref in observation["evidence_refs"])
    rows = [
        row for row in jsonl.read_all(paths.journal("evolution"))
        if row.get("kind") == "proposal.post_apply_observation"
    ]
    assert rows[-1]["proposal_id"] == proposal.id
    assert rows[-1]["source"] == "validation_backtest"
    assert rows[-1]["run_id"] == result["validation_run_id"]
    assert rows[-1]["metadata"]["validation_step_status"] == "passed"


def test_validation_plan_backtest_missing_history_is_failed(tmp_path, monkeypatch):
    paths = WorkspacePaths(tmp_path)

    def fake_backtest(**_kwargs):  # noqa: ANN003, ANN202
        raise NoHistoricalDataError("no historical candles for ALPHA")

    monkeypatch.setattr(
        "nerya.evolution.validation_plan.run_strategy_backtest",
        fake_backtest,
    )
    plan = build_validation_plan(
        [{"type": "backtest", "required": True}],
        source="test",
        strategy_id="alpha",
    )
    plan_id = write_validation_plan(paths, plan)

    result = run_validation_plan(paths, plan_id=plan_id, dry_run=False)

    assert result["ok"] is False
    assert result["status"] == "failed"
    step = result["run"]["steps"][0]
    assert step["type"] == "backtest"
    assert step["status"] == "failed"
    assert step["reason"] == "no_historical_data"
    assert step["backtest_result"]["coverage_ok"] is False


def test_strategy_tuning_materializes_after_content(tmp_path):
    paths = WorkspacePaths(tmp_path)
    pkg = _seed_strategy(paths)

    files, materialized, unmaterialized = _materialize_strategy_tuning_after_files(
        pkg,
        [
            {
                "file": "main.py",
                "kind": "full_file",
                "after_content": "def run(ctx):\n    return {'ok': 'tuned'}\n",
            }
        ],
    )

    assert files == {
        "after/strategies/alpha/main.py": "def run(ctx):\n    return {'ok': 'tuned'}\n"
    }
    assert materialized == ["strategies/alpha/main.py"]
    assert unmaterialized == []


def test_strategy_tuning_code_patch_without_content_stays_advisory(tmp_path):
    paths = WorkspacePaths(tmp_path)
    pkg = _seed_strategy(paths)

    files, materialized, unmaterialized = _materialize_strategy_tuning_after_files(
        pkg,
        [{"file": "main.py", "kind": "code_patch", "rationale": "tighten filter"}],
    )

    assert files == {}
    assert materialized == []
    assert unmaterialized[0]["reason"] == "missing_after_content"


def test_strategy_tuning_proposal_writes_after_files(tmp_path):
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={"runtime": {"mock_mode": True}})
    pkg = _seed_strategy(paths)

    class Snapshot:
        runs_considered = 5
        run_metrics = {"ok_rate": 1.0, "error_rate": 0.0}
        trade_metrics = {"pnl_total_usd": 12.0, "max_drawdown_usd": -2.0}

        def asdict(self):  # noqa: ANN201
            return {
                "runs_considered": self.runs_considered,
                "run_metrics": self.run_metrics,
                "trade_metrics": self.trade_metrics,
            }

    selected_assets = {
        "selection_signals": [
            {
                "id": "sig_tune",
                "kind": "market_regime_high_volatility",
                "severity": "warn",
                "summary": "High volatility regime matched prior tuning lessons.",
                "confidence": 0.9,
                "evidence_refs": ["strategy_tuning:tune_test"],
                "metadata": {"markets": ["mock:BTC/USDT"], "timeframe": "1h"},
            }
        ],
        "genes": [
            {
                "id": "gene_volatility_tuning",
                "summary": "Tune entry filters when volatility expands.",
                "signals_match": ["market_regime_high_volatility"],
                "evidence_refs": ["proposal:gene_source"],
                "gdi": {
                    "version": "gdi_v1",
                    "score": 0.82,
                    "polarity": "positive",
                    "matched_signals": ["market_regime_high_volatility"],
                    "relevance": {
                        "version": "trigger_relevance_v1",
                        "score": 0.9,
                        "source": "gene_signals",
                        "matched_signals": ["market_regime_high_volatility"],
                    },
                },
            }
        ],
        "capsules": [
            {
                "id": "cap_prior_regression",
                "kind": "capsule",
                "summary": "Prior volatility patch overtraded and regressed.",
                "outcome_score": -0.4,
                "evidence_refs": ["proposal:bad_prior"],
                "gdi": {
                    "version": "gdi_v1",
                    "score": 0.71,
                    "polarity": "negative",
                    "matched_signals": ["market_regime_high_volatility"],
                    "relevance": {
                        "version": "trigger_relevance_v1",
                        "score": 0.86,
                        "source": "trigger_metadata",
                        "matched_signals": ["market_regime_high_volatility"],
                        "matched_context": {"market_regimes": ["high_volatility"]},
                    },
                },
            }
        ],
    }

    proposal = StrategyEvolutionRunner(
        config=config,
        skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
    )._create_tuning_proposal(
        pkg=pkg,
        run_id="tune_test",
        snapshot=Snapshot(),
        output={"summary": "tighten filter"},
        accepted=[
            {
                "file": "main.py",
                "kind": "full_file",
                "after_content": "def run(ctx):\n    return {'ok': 'tuned'}\n",
            }
        ],
        review_text="# Review\n",
        audit_text=json.dumps({"selected_assets": selected_assets}),
        source_event_id="evt_test",
        validation_plan_id="vpl_test",
        selected_assets=selected_assets,
    )

    assert proposal is not None
    after_file = proposal.path / "after" / "strategies" / "alpha" / "main.py"
    assert after_file.read_text(encoding="utf-8") == (
        "def run(ctx):\n    return {'ok': 'tuned'}\n"
    )
    materialization = proposal.path / "materialization.json"
    assert '"materialized": true' in materialization.read_text(encoding="utf-8")
    assert proposal.metadata["materialized"] is True
    assert proposal.metadata["advisory_only"] is False
    why = proposal_why_reused(paths, proposal.asdict())
    assert why is not None
    assert why["counts"]["genes"] == 1
    assert why["counts"]["negative_capsules"] == 1
    assert why["trigger_context"]["signal_kinds"] == ["market_regime_high_volatility"]
    assert why["proposal_diff"]["change_count"] == 1
    assert why["negative_capsules"][0]["id"] == "cap_prior_regression"
    assert why["negative_capsules"][0]["relevance_score"] == 0.86

    route_map = {(method, path): handler for method, path, handler in routes_evolution.routes()}
    detail = route_map[("POST", "/evolution/proposals/{proposal_id}")](
        SimpleNamespace(config=config),
        {"proposal_id": proposal.id},
    )
    assert detail["why_reused"]["proposal_diff"]["paths"] == ["strategies/alpha/main.py"]


def test_strategy_tuner_payload_includes_selected_genes_and_capsules(tmp_path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={"runtime": {"mock_mode": True}})
    pkg = _seed_strategy(paths)
    jsonl.append(
        paths.evolution_capsules,
        {
            "id": "cap_positive",
            "gene_id": "gene_nerya_strategy_drawdown_review",
            "summary": "Prior drawdown tuning reduced false entries.",
            "evidence_refs": ["proposal:prp_positive"],
            "validation_results": [{"status": "passed"}],
            "outcome_score": 0.8,
            "strategy_id": "alpha",
        },
        stamp=False,
    )
    jsonl.append(
        paths.evolution_capsules,
        {
            "id": "cap_negative",
            "gene_id": "gene_nerya_strategy_drawdown_review",
            "summary": "Rejected widening position filter after slippage worsened.",
            "evidence_refs": ["proposal:prp_negative"],
            "validation_results": [{"status": "failed"}],
            "outcome_score": -0.6,
            "strategy_id": "alpha",
        },
        stamp=False,
    )
    jsonl.append(
        paths.evolution_capsules,
        {
            "id": "cap_other_strategy",
            "summary": "Should not leak across strategy-local tuning.",
            "evidence_refs": [],
            "validation_results": [],
            "outcome_score": 1.0,
            "strategy_id": "beta",
        },
        stamp=False,
    )

    class Snapshot:
        def asdict(self):  # noqa: ANN201
            return {
                "strategy_id": "alpha",
                "package_hash": pkg.content_hash,
                "runs_considered": 20,
                "trade_metrics": {
                    "max_drawdown_usd": -42.0,
                    "avg_slippage": 9.5,
                    "slippage_samples": 4,
                },
            }

    captured: dict[str, object] = {}

    def fake_dispatch(self, name, *, payload, **kwargs):  # noqa: ANN001, ANN202
        captured["name"] = name
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return {"ok": True, "output": {"proposed_changes": []}}

    monkeypatch.setattr(
        "nerya.subagents.dispatcher.SubAgentDispatcher.dispatch",
        fake_dispatch,
    )

    envelope = StrategyEvolutionRunner(
        config=config,
        skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
    )._dispatch_tuner(
        pkg=pkg,
        snapshot=Snapshot(),
        trigger_event_id=None,
        run_id="tune_assets",
    )

    assert envelope["selected_assets"]["genes"][0]["id"] == "gene_nerya_strategy_drawdown_review"
    payload = captured["payload"]
    assert "Strategy tuning materialization contract" in payload["__team_instructions"]
    assert payload["materializable_output_contract"]["version"] == (
        "strategy_tuning_materializable_output_v1"
    )
    assert payload["selected_genes"][0]["id"] == "gene_nerya_strategy_drawdown_review"
    assert payload["similar_capsules"][0]["id"] == "cap_positive"
    assert payload["negative_capsules"][0]["id"] == "cap_negative"
    assert all(c["id"] != "cap_other_strategy" for c in payload["selected_assets"]["capsules"])
    signal_kinds = {s["kind"] for s in payload["selected_assets"]["selection_signals"]}
    assert {"strategy_tuning_run", "strategy_drawdown", "high_slippage"} <= signal_kinds


def test_strategy_tuner_payload_uses_market_regime_selection_signals(tmp_path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={"runtime": {"mock_mode": True}})
    pkg = _seed_strategy(paths)
    paths.evolution_genes.parent.mkdir(parents=True, exist_ok=True)
    paths.evolution_genes.write_text(
        json.dumps(
            [
                {
                    "id": "gene_market_regime_breakout",
                    "category": "strategy",
                    "signals_match": [
                        "market_regime_trending",
                        "market_regime_high_volatility",
                        "market_news_context",
                    ],
                    "preconditions": ["performance_snapshot_has_market_context"],
                    "strategy": ["reuse breakout-regime tuning lessons"],
                    "validation": ["backtest"],
                    "confidence": 0.98,
                    "summary": "Reuse prior lessons for high-volatility breakout regimes.",
                }
            ]
        ),
        encoding="utf-8",
    )
    jsonl.append(
        paths.evolution_capsules,
        {
            "id": "cap_breakout_prior",
            "gene_id": "gene_market_regime_breakout",
            "summary": "Prior breakout tuning needed wider confirmation windows.",
            "evidence_refs": ["proposal:prp_breakout"],
            "validation_results": [{"status": "passed"}],
            "outcome_score": 0.9,
            "strategy_id": "alpha",
        },
        stamp=False,
    )

    class Snapshot:
        def asdict(self):  # noqa: ANN201
            return {
                "strategy_id": "alpha",
                "package_hash": pkg.content_hash,
                "runs_considered": 12,
                "trade_metrics": {},
                "market_context": {
                    "timeframe": "1h",
                    "markets": ["mock:BTC/USDT"],
                    "items": [
                        {
                            "market": "mock:BTC/USDT",
                            "timeframe": "1h",
                            "candles_count": 96,
                            "features": {
                                "close": 100.0,
                                "sma_20": 93.0,
                                "ema_20": 95.0,
                                "ret_1": 0.041,
                                "atr_14": 4.2,
                                "adx_14": 31.0,
                                "rsi_14": 68.0,
                                "breakout": {"breakout": True, "strength": 0.045},
                            },
                            "_envelope": {"mode": "live", "source": "mock_exchange"},
                        }
                    ],
                },
                "news_context": {
                    "count": 1,
                    "symbols": ["BTC"],
                    "items": [
                        {
                            "source": "newswire",
                            "title": "BTC volatility expands after ETF flows",
                            "published_at": "2026-06-17T00:00:00+00:00",
                            "tickers": ["BTC"],
                            "matched_tickers": ["BTC"],
                        }
                    ],
                },
            }

    captured: dict[str, object] = {}

    def fake_dispatch(self, name, *, payload, **kwargs):  # noqa: ANN001, ANN202
        captured["payload"] = payload
        return {"ok": True, "output": {"proposed_changes": []}}

    monkeypatch.setattr(
        "nerya.subagents.dispatcher.SubAgentDispatcher.dispatch",
        fake_dispatch,
    )

    envelope = StrategyEvolutionRunner(
        config=config,
        skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
    )._dispatch_tuner(
        pkg=pkg,
        snapshot=Snapshot(),
        trigger_event_id=None,
        run_id="tune_regime",
    )

    payload = captured["payload"]
    signal_kinds = {
        signal["kind"]
        for signal in payload["selected_assets"]["selection_signals"]
    }
    assert {
        "market_regime_trending",
        "market_regime_high_volatility",
        "market_news_context",
    } <= signal_kinds
    assert payload["selected_genes"][0]["id"] == "gene_market_regime_breakout"
    assert payload["similar_capsules"][0]["id"] == "cap_breakout_prior"
    regime_signal = next(
        signal for signal in envelope["selected_assets"]["selection_signals"]
        if signal["kind"] == "market_regime_high_volatility"
    )
    assert regime_signal["metadata"]["markets"] == ["mock:BTC/USDT"]
    assert regime_signal["metadata"]["feature_evidence"][0]["atr_pct"] == 0.042


def test_market_regime_selection_signals_include_data_quality_warnings(tmp_path):
    paths = WorkspacePaths(tmp_path)
    pkg = _seed_strategy(paths)

    class Snapshot:
        def asdict(self):  # noqa: ANN201
            return {
                "strategy_id": "alpha",
                "package_hash": pkg.content_hash,
                "trade_metrics": {},
                "market_context": {
                    "timeframe": "1h",
                    "markets": ["mock:BTC/USDT"],
                    "items": [
                        {
                            "market": "mock:BTC/USDT",
                            "timeframe": "1h",
                            "candles_count": 0,
                            "features": {},
                            "_envelope": {
                                "mode": "unavailable",
                                "source": "candles",
                                "degraded": True,
                                "error": "no_rows",
                            },
                        }
                    ],
                },
                "news_context": {"count": 0, "error": "HTTPError: timeout"},
            }

    signals = _tuning_asset_selection_signals(pkg, Snapshot(), "tune_degraded_market")
    degraded = next(signal for signal in signals if signal["kind"] == "market_data_degraded")
    assert degraded["severity"] == "warn"
    reasons = {issue["reason"] for issue in degraded["metadata"]["issues"]}
    assert {"no_recent_candles", "degraded_market_data", "news_context_error"} <= reasons


def test_apply_blocks_strategy_tuning_without_after_files(tmp_path):
    paths = WorkspacePaths(tmp_path)
    proposal = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="Advisory tuning note",
        target="strategies/alpha",
        initial_state="approved",
        metadata={"strategy_id": "alpha", "advisory_only": True},
    )

    result = apply_proposal(paths, proposal.id)

    assert result["ok"] is False
    assert result["reason"] == "advisory_only"


def test_apply_blocks_materialized_mutation_without_validation_evidence(tmp_path):
    paths = WorkspacePaths(tmp_path)
    proposal = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="Tune alpha without validation",
        target="strategies/alpha",
        initial_state="approved",
        evidence_refs=["turn:t_alpha"],
        metadata={"strategy_id": "alpha", "materialized": True},
        extra_files={
            "after/strategies/alpha/main.py": "def run(ctx):\n    return {'ok': True}\n",
        },
    )

    result = apply_proposal(paths, proposal.id)

    assert result["ok"] is False
    assert result["reason"] == "missing_validation_evidence"
    assert "missing_validation_evidence" in result["action_gates"]["blockers"]
    assert not (tmp_path / "strategies" / "alpha" / "main.py").exists()


def test_apply_requires_passed_validation_plan_for_materialized_mutation(tmp_path):
    paths = WorkspacePaths(tmp_path)
    plan = build_validation_plan(
        [{"type": "manual_review", "required": True}],
        source="test",
        strategy_id="alpha",
    )
    plan_id = write_validation_plan(paths, plan)
    plan_path = paths.evolution_validation_plans / f"{plan_id}.json"
    plan_record = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_record["status"] = "passed"
    plan_record["steps"][0]["status"] = "passed"
    plan_record["steps"][0]["evidence_ref"] = "validation:vrn_alpha:step:0"
    plan_path.write_text(json.dumps(plan_record), encoding="utf-8")
    proposal = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="Validated alpha tuning",
        target="strategies/alpha",
        initial_state="approved",
        evidence_refs=["turn:t_alpha"],
        validation_plan_id=plan_id,
        metadata={"strategy_id": "alpha", "materialized": True},
        extra_files={
            "after/strategies/alpha/main.py": "def run(ctx):\n    return {'ok': True}\n",
        },
    )

    gates = proposal_action_gates(paths, proposal.id)
    result = apply_proposal(paths, proposal.id)

    assert gates["can_apply"] is True
    assert result["ok"] is True
    assert result["action_gates"]["can_apply"] is True
    assert (tmp_path / "strategies" / "alpha" / "main.py").exists()


def test_strategy_tuning_selects_best_multi_candidate_and_persists_optimizer_report(
    tmp_path,
    monkeypatch,
):
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={"runtime": {"mock_mode": True}})
    _seed_strategy(paths)

    def fake_dispatch(self, name, *, payload, **kwargs):  # noqa: ANN001, ANN202
        return {
            "ok": True,
            "output": {
                "summary": "choose the safest materialized tuning",
                "evidence": [{"source": "recent_runs", "finding": "false entries increased"}],
                "candidates": [
                    {
                        "id": "blocked_no_validation",
                        "summary": "materialized but lacks validation",
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": "def run(ctx):\n    return {'ok': 'blocked'}\n",
                            }
                        ],
                    },
                    {
                        "id": "advisory_patch",
                        "summary": "advisory patch only",
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "code_patch",
                                "rationale": "tighten entry condition",
                            }
                        ],
                        "validation_plan": ["manual_review"],
                    },
                    {
                        "id": "safe_backtest",
                        "summary": "materialized change with stronger validation",
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": (
                                    "def run(ctx):\n"
                                    "    return {'ok': True, 'tuned': 'safe_backtest'}\n"
                                ),
                            }
                        ],
                        "expected_effect": {"drawdown": "lower", "false_entries": "lower"},
                        "validation_plan": ["unit", "backtest"],
                        "risk_flags": [],
                    },
                    {
                        "id": "unsafe_limit",
                        "summary": "forbidden account-side change",
                        "proposed_changes": [
                            {
                                "file": "limits.yml",
                                "kind": "strategy_yml",
                                "config_after": {"live_trading_enabled": True},
                            }
                        ],
                        "validation_plan": ["manual_review"],
                    },
                ],
            },
        }

    monkeypatch.setattr(
        "nerya.subagents.dispatcher.SubAgentDispatcher.dispatch",
        fake_dispatch,
    )

    def fake_backtest(**kwargs):  # noqa: ANN003, ANN202
        package_dir = Path(str(kwargs.get("package_dir") or ""))
        out_dir = package_dir / "backtests" / "preview_ok"
        out_dir.mkdir(parents=True, exist_ok=True)
        metrics = out_dir / "metrics.json"
        report = out_dir / "report.md"
        trades = out_dir / "trades.csv"
        config_path = out_dir / "config.yml"
        metrics.write_text("{}", encoding="utf-8")
        report.write_text("# preview ok\n", encoding="utf-8")
        trades.write_text("id\n", encoding="utf-8")
        config_path.write_text("preset: default\n", encoding="utf-8")
        return {
            "ok": True,
            "strategy_id": "alpha",
            "package_dir": str(package_dir),
            "backtest_ts": "preview_ok",
            "verdict": "PASS",
            "coverage_ok": True,
            "total_return_pct": 1.2,
            "max_drawdown_pct": -0.3,
            "sharpe_ratio": 0.8,
            "total_trades": 2,
            "metrics_path": str(metrics),
            "report_path": str(report),
            "trades_path": str(trades),
            "config_path": str(config_path),
        }

    monkeypatch.setattr("nerya.strategies.evolution.run_strategy_backtest", fake_backtest)

    result = StrategyEvolutionRunner(
        config=config,
        skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
    ).run_once("alpha", operator="test", dry_run=False)

    assert result.status == "ok"
    assert result.proposal_id
    assert result.subagent_output["candidate_id"] == "safe_backtest"
    assert result.optimizer_report["selected_candidate_id"] == "safe_backtest"
    candidate_status = {
        row["candidate_id"]: row["status"]
        for row in result.optimizer_report["candidates"]
    }
    assert candidate_status["blocked_no_validation"] == "blocked"
    assert candidate_status["advisory_patch"] == "advisory"
    assert candidate_status["safe_backtest"] == "materialized"
    assert candidate_status["unsafe_limit"] == "empty"

    proposal_dir = paths.proposals / result.proposal_id
    after_file = proposal_dir / "after" / "strategies" / "alpha" / "main.py"
    assert "safe_backtest" in after_file.read_text(encoding="utf-8")
    proposal_meta = yaml_io.load(proposal_dir / "proposal.yml", default={}) or {}
    assert proposal_meta["metadata"]["optimizer"]["selected_candidate_id"] == "safe_backtest"
    tuning_run = json.loads((proposal_dir / "tuning_run.json").read_text(encoding="utf-8"))
    assert tuning_run["optimizer_report"]["candidate_count"] == 4
    assert tuning_run["optimizer_report"]["selected_candidate_id"] == "safe_backtest"

    audit = json.loads(
        (paths.strategy("alpha") / "reviews" / f"tuning_{result.run_id}_audit.json")
        .read_text(encoding="utf-8")
    )
    assert audit["raw_subagent_output"]["candidates"][2]["id"] == "safe_backtest"
    assert audit["subagent_output"]["candidate_id"] == "safe_backtest"
    assert audit["optimizer_report"]["selected_candidate_id"] == "safe_backtest"

    route_map = {(method, path): handler for method, path, handler in routes_evolution.routes()}
    detail = route_map[("POST", "/evolution/proposals/{proposal_id}")](
        SimpleNamespace(config=config),
        {"proposal_id": result.proposal_id},
    )
    assert detail["optimizer_report"]["selected_candidate_id"] == "safe_backtest"
    assert detail["optimizer_report"]["candidates"][0]["candidate_id"] == "blocked_no_validation"
    out = route_map[("POST", "/evolution/timeline")](
        SimpleNamespace(config=config),
        {"strategy_id": "alpha", "limit": 50},
    )
    linked = [
        item for item in out["timeline"]
        if f"strategy_tuning:{result.run_id}" in item.get("evidence_refs", [])
    ]
    assert linked
    assert linked[0]["optimizer_report"]["selected_candidate_id"] == "safe_backtest"
    optimizer = next(
        artifact
        for section in linked[0]["process"]["sections"]
        for artifact in section["artifacts"]
        if artifact["title"] == "Candidate optimizer"
    )
    optimizer_payload = json.loads(optimizer["preview"])
    assert optimizer_payload["selected_candidate_id"] == "safe_backtest"
    assert optimizer_payload["candidates"][2]["status"] == "materialized"
    assert optimizer_payload["validation_preview"]["previewed_count"] == 1
    assert optimizer_payload["candidates"][2]["validation_preview"]["status"] == "passed"
    assert optimizer_payload["backtest_preview"]["previewed_count"] == 1
    assert optimizer_payload["candidates"][2]["backtest_preview"]["status"] == "passed"
    assert optimizer_payload["candidates"][2]["asset_candidate"]["id"]

    asset_candidates = evolution_assets.list_candidates(paths)
    assert len(asset_candidates) == 1
    preview_candidate = asset_candidates[0]
    assert preview_candidate["kind"] == "capsule"
    assert preview_candidate["safe_to_promote"] is True
    assert preview_candidate["promotion_gates"]["can_promote"] is True
    assert preview_candidate["promotion_gates"]["selector_eligible"] is False
    assert preview_candidate["promotion_gates"]["review_only_until_promoted"] is True
    assert "Backtest preview passed" in preview_candidate["summary"]
    assert f"strategy_tuning:{result.run_id}" in preview_candidate["evidence_refs"]
    payload = preview_candidate["payload"]
    assert payload["outcome_score"] > 0
    assert payload["metadata"]["origin"] == "strategy_optimizer_preview"
    assert payload["metadata"]["preview_type"] == "backtest"
    assert payload["metadata"]["preview_status"] == "passed"
    assert payload["metadata"]["selected_by_optimizer"] is True
    assert payload["metadata"]["origin"] == "strategy_optimizer_preview"


def test_strategy_tuning_candidate_validation_preview_penalizes_static_failures(
    tmp_path,
    monkeypatch,
):
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={"runtime": {"mock_mode": True}})
    _seed_strategy(paths)

    def fake_dispatch(self, name, *, payload, **kwargs):  # noqa: ANN001, ANN202
        return {
            "ok": True,
            "output": {
                "summary": "choose candidate after bounded validation preview",
                "candidates": [
                    {
                        "id": "bad_static",
                        "summary": "High scoring candidate with forbidden import.",
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": (
                                    "import requests\n\n"
                                    "def run(ctx):\n"
                                    "    return {'ok': True, 'bad': requests.__name__}\n"
                                ),
                            }
                        ],
                        "expected_effect": {"return": "higher"},
                        "validation_plan": ["unit", "backtest"],
                    },
                    {
                        "id": "safe_static",
                        "summary": "Lower scoring candidate that passes static preview.",
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": (
                                    "def run(ctx):\n"
                                    "    return {'ok': True, 'safe': True}\n"
                                ),
                            }
                        ],
                        "validation_plan": ["manual_review"],
                    },
                ],
            },
        }

    monkeypatch.setattr(
        "nerya.subagents.dispatcher.SubAgentDispatcher.dispatch",
        fake_dispatch,
    )

    result = StrategyEvolutionRunner(
        config=config,
        skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
    ).run_once("alpha", operator="test", dry_run=False)

    assert result.status == "ok"
    assert result.subagent_output["candidate_id"] == "safe_static"
    report = result.optimizer_report
    assert report["selected_candidate_id"] == "safe_static"
    assert report["validation_preview"]["previewed_count"] == 2
    assert report["validation_preview"]["failed_count"] == 1
    candidates = {
        row["candidate_id"]: row
        for row in report["candidates"]
    }
    assert candidates["bad_static"]["status"] == "failed_preview"
    assert candidates["bad_static"]["validation_preview"]["status"] == "failed"
    assert "candidate_validation_preview_failed" in candidates["bad_static"]["reasons"]
    assert any(
        "forbidden_import" in str(reason)
        for reason in candidates["bad_static"]["validation_preview"]["blocked_reasons"]
    )
    assert candidates["safe_static"]["validation_preview"]["status"] == "passed"
    refs = candidates["safe_static"]["validation_preview"]["evidence_refs"]
    assert refs and refs[0].startswith("file:evolution/optimizer_runs/")
    assert (paths.root / refs[0].split("file:", 1)[1]).exists()


def test_strategy_tuning_candidate_backtest_preview_penalizes_failed_replay(
    tmp_path,
    monkeypatch,
):
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={"runtime": {"mock_mode": True}})
    _seed_strategy(paths)

    def fake_dispatch(self, name, *, payload, **kwargs):  # noqa: ANN001, ANN202
        return {
            "ok": True,
            "output": {
                "summary": "choose candidate after bounded backtest preview",
                "candidates": [
                    {
                        "id": "bad_backtest",
                        "summary": "Looks strong but fails historical replay.",
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": (
                                    "def run(ctx):\n"
                                    "    return {'ok': True, 'bad_backtest': True}\n"
                                ),
                            }
                        ],
                        "expected_effect": {"return": "higher"},
                        "validation_plan": ["unit", "backtest"],
                    },
                    {
                        "id": "safe_manual",
                        "summary": "Lower nominal score but no failed backtest preview.",
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": (
                                    "def run(ctx):\n"
                                    "    return {'ok': True, 'safe_manual': True}\n"
                                ),
                            }
                        ],
                        "validation_plan": ["manual_review"],
                    },
                ],
            },
        }

    def fake_backtest(**kwargs):  # noqa: ANN003, ANN202
        package_dir = Path(str(kwargs.get("package_dir") or ""))
        out_dir = package_dir / "backtests" / "preview_fail"
        out_dir.mkdir(parents=True, exist_ok=True)
        metrics = out_dir / "metrics.json"
        report = out_dir / "report.md"
        trades = out_dir / "trades.csv"
        config_path = out_dir / "config.yml"
        metrics.write_text("{}", encoding="utf-8")
        report.write_text("# preview fail\n", encoding="utf-8")
        trades.write_text("id\n", encoding="utf-8")
        config_path.write_text("preset: default\n", encoding="utf-8")
        return {
            "ok": True,
            "strategy_id": "alpha",
            "package_dir": str(package_dir),
            "backtest_ts": "preview_fail",
            "verdict": "FAIL",
            "coverage_ok": True,
            "total_return_pct": -4.2,
            "max_drawdown_pct": -8.5,
            "sharpe_ratio": -0.4,
            "total_trades": 4,
            "metrics_path": str(metrics),
            "report_path": str(report),
            "trades_path": str(trades),
            "config_path": str(config_path),
        }

    monkeypatch.setattr(
        "nerya.subagents.dispatcher.SubAgentDispatcher.dispatch",
        fake_dispatch,
    )
    monkeypatch.setattr("nerya.strategies.evolution.run_strategy_backtest", fake_backtest)

    result = StrategyEvolutionRunner(
        config=config,
        skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
    ).run_once("alpha", operator="test", dry_run=False)

    assert result.status == "ok"
    assert result.subagent_output["candidate_id"] == "safe_manual"
    report = result.optimizer_report
    assert report["selected_candidate_id"] == "safe_manual"
    assert report["backtest_preview"]["previewed_count"] == 1
    assert report["backtest_preview"]["failed_count"] == 1
    candidates = {
        row["candidate_id"]: row
        for row in report["candidates"]
    }
    assert candidates["bad_backtest"]["status"] == "failed_backtest_preview"
    assert candidates["bad_backtest"]["backtest_preview"]["status"] == "failed"
    assert candidates["bad_backtest"]["backtest_preview"]["backtest_result"]["verdict"] == "FAIL"
    assert "candidate_backtest_preview_failed" in candidates["bad_backtest"]["reasons"]
    refs = candidates["bad_backtest"]["backtest_preview"]["evidence_refs"]
    assert any(ref.endswith("backtest_preview.json") for ref in refs)
    assert any(ref.endswith("metrics.json") for ref in refs)
    assert candidates["bad_backtest"]["asset_candidate"]["id"]

    asset_candidates = evolution_assets.list_candidates(paths)
    failed_preview_candidates = [
        row for row in asset_candidates
        if row["payload"]["metadata"]["optimizer_candidate_id"] == "bad_backtest"
    ]
    assert len(failed_preview_candidates) == 1
    payload = failed_preview_candidates[0]["payload"]
    assert failed_preview_candidates[0]["promotion_gates"]["selector_eligible"] is False
    assert "promotes_as_negative_cautionary_capsule" in failed_preview_candidates[0]["promotion_gates"]["warnings"]
    assert payload["outcome_score"] < 0
    assert payload["metadata"]["preview_type"] == "backtest"
    assert payload["metadata"]["preview_status"] == "failed"
    assert payload["metadata"]["selected_by_optimizer"] is False
    assert payload["validation_results"][0]["backtest_result"]["verdict"] == "FAIL"


def test_strategy_tuning_candidate_backtest_preview_compares_workspace_baseline(
    tmp_path,
    monkeypatch,
):
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={"runtime": {"mock_mode": True}})
    _seed_strategy(paths)
    baseline_dir = paths.strategy("alpha") / "backtests" / "baseline_good"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    (baseline_dir / "metrics.json").write_text(
        json.dumps(
            {
                "verdict": "PASS",
                "total_return_pct": 6.0,
                "max_drawdown_pct": 1.0,
                "sharpe_ratio": 1.2,
                "profit_factor": 1.5,
                "win_rate_pct": 62.0,
                "total_trades": 10,
            }
        ),
        encoding="utf-8",
    )

    def fake_dispatch(self, name, *, payload, **kwargs):  # noqa: ANN001, ANN202
        return {
            "ok": True,
            "output": {
                "summary": "compare candidate preview with baseline",
                "candidates": [
                    {
                        "id": "worse_than_baseline",
                        "summary": "Passes standalone replay but regresses baseline.",
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": (
                                    "def run(ctx):\n"
                                    "    return {'ok': True, 'worse': True}\n"
                                ),
                            }
                        ],
                        "validation_plan": ["unit", "backtest"],
                    },
                    {
                        "id": "safe_manual",
                        "summary": "Keeps manual path while candidate baseline evidence is weak.",
                        "expected_effect": {"risk": "unchanged"},
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": (
                                    "def run(ctx):\n"
                                    "    return {'ok': True, 'safe_manual': True}\n"
                                ),
                            }
                        ],
                        "validation_plan": ["manual_review"],
                    },
                ],
            },
        }

    def fake_backtest(**kwargs):  # noqa: ANN003, ANN202
        package_dir = Path(str(kwargs.get("package_dir") or ""))
        out_dir = package_dir / "backtests" / "preview_pass_regressed"
        out_dir.mkdir(parents=True, exist_ok=True)
        metrics = out_dir / "metrics.json"
        report = out_dir / "report.md"
        metrics.write_text("{}", encoding="utf-8")
        report.write_text("# preview pass but worse\n", encoding="utf-8")
        return {
            "ok": True,
            "strategy_id": "alpha",
            "package_dir": str(package_dir),
            "backtest_ts": "preview_pass_regressed",
            "verdict": "PASS",
            "coverage_ok": True,
            "total_return_pct": 1.0,
            "max_drawdown_pct": 6.0,
            "sharpe_ratio": 0.2,
            "profit_factor": 0.8,
            "win_rate_pct": 42.0,
            "total_trades": 4,
            "metrics_path": str(metrics),
            "report_path": str(report),
        }

    monkeypatch.setattr(
        "nerya.subagents.dispatcher.SubAgentDispatcher.dispatch",
        fake_dispatch,
    )
    monkeypatch.setattr("nerya.strategies.evolution.run_strategy_backtest", fake_backtest)

    result = StrategyEvolutionRunner(
        config=config,
        skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
    ).run_once("alpha", operator="test", dry_run=False)

    assert result.status == "ok"
    assert result.subagent_output["candidate_id"] == "safe_manual"
    report = result.optimizer_report
    candidates = {row["candidate_id"]: row for row in report["candidates"]}
    regressed = candidates["worse_than_baseline"]
    preview = regressed["backtest_preview"]
    assert preview["status"] == "passed"
    assert preview["baseline_comparison"]["status"] == "complete"
    assert preview["baseline_comparison"]["overall_direction"] == "regressed"
    assert preview["baseline_comparison"]["score_delta"] < 0
    assert preview["score_delta"] < 0
    assert "candidate_backtest_baseline_regressed" in regressed["reasons"]
    assert any(
        row["key"] == "total_return_pct" and row["direction"] == "regressed"
        for row in preview["baseline_comparison"]["metrics_delta"]
    )
    assert any(
        ref.endswith("strategies/alpha/backtests/baseline_good/metrics.json")
        for ref in preview["baseline_comparison"]["evidence_refs"]
    )
    assert any(
        ref.endswith("strategies/alpha/backtests/baseline_good/metrics.json")
        for ref in preview["evidence_refs"]
    )

    asset_candidates = evolution_assets.list_candidates(paths)
    preview_candidates = [
        row for row in asset_candidates
        if row["payload"]["metadata"]["optimizer_candidate_id"] == "worse_than_baseline"
    ]
    assert len(preview_candidates) == 1
    payload = preview_candidates[0]["payload"]
    assert payload["metadata"]["baseline_comparison"]["overall_direction"] == "regressed"
    assert payload["validation_results"][0]["baseline_comparison"]["score_delta"] < 0


def test_strategy_tuning_candidate_scoring_uses_historical_outcome_feedback(
    tmp_path,
    monkeypatch,
):
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={"runtime": {"mock_mode": True}})
    _seed_strategy(paths)
    prior_report = {
        "version": "strategy_tuning_optimizer_v1",
        "candidate_count": 1,
        "evaluated_count": 1,
        "selected_candidate_id": "feedback_winner",
        "selected_index": 0,
        "selected_score": 100,
        "candidates": [
            {
                "candidate_id": "feedback_winner",
                "index": 0,
                "score": 100,
                "status": "materialized",
                "summary": "Previously healthy materialized filter change.",
                "accepted_count": 1,
                "materialized_count": 1,
                "materialized_files": ["strategies/alpha/main.py"],
                "accepted_targets": ["main.py"],
                "validation_status": "not_run",
                "validation_types": ["manual_review"],
                "blocked_reasons": [],
                "risk_flags": [],
                "reasons": ["materialized_files:1", "validation_step:manual_review"],
            }
        ],
    }
    prior = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="Prior healthy optimizer result",
        target="strategies/alpha",
        initial_state="applied",
        evidence_refs=["strategy_tuning:tune_prior"],
        metadata={"strategy_id": "alpha", "materialized": True},
        extra_files={
            "tuning_run.json": json.dumps({"optimizer_report": prior_report}),
            "after/strategies/alpha/main.py": "def run(ctx):\n    return {'ok': 'prior'}\n",
        },
    )
    jsonl.append(
        paths.journal("strategy_evolution"),
        {
            "kind": "strategy.tuning",
            "run_id": "tune_prior",
            "strategy_id": "alpha",
            "proposal_id": prior.id,
            "status": "ok",
            "ts": "2026-06-17T00:00:00+00:00",
        },
        stamp=False,
    )
    recorded = record_post_apply_observation(
        paths,
        proposal_id=prior.id,
        status="healthy",
        source="validation_backtest",
        observed_at="2026-06-17T01:00:00+00:00",
        summary="Prior selected candidate stayed healthy.",
        evidence_refs=["validation:vrn_prior:step:0"],
    )
    assert recorded["ok"] is True

    def fake_dispatch(self, name, *, payload, **kwargs):  # noqa: ANN001, ANN202
        return {
            "ok": True,
            "output": {
                "summary": "choose using outcome feedback",
                "candidates": [
                    {
                        "id": "fresh_alternative",
                        "summary": "Same shape but no prior successful outcome.",
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": "def run(ctx):\n    return {'ok': 'fresh'}\n",
                            }
                        ],
                        "validation_plan": ["manual_review"],
                    },
                    {
                        "id": "feedback_winner",
                        "summary": "Same shape with prior healthy outcome feedback.",
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": "def run(ctx):\n    return {'ok': 'feedback'}\n",
                            }
                        ],
                        "validation_plan": ["manual_review"],
                    },
                ],
            },
        }

    monkeypatch.setattr(
        "nerya.subagents.dispatcher.SubAgentDispatcher.dispatch",
        fake_dispatch,
    )

    result = StrategyEvolutionRunner(
        config=config,
        skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
    ).run_once("alpha", operator="test", dry_run=False)

    assert result.status == "ok"
    assert result.subagent_output["candidate_id"] == "feedback_winner"
    report = result.optimizer_report
    assert report["outcome_feedback"]["sample_count"] == 1
    assert report["outcome_feedback"]["positive_samples"] == 1
    candidates = {
        row["candidate_id"]: row
        for row in report["candidates"]
    }
    assert candidates["feedback_winner"]["score"] > candidates["fresh_alternative"]["score"]
    assert candidates["feedback_winner"]["outcome_feedback"]["score_delta"] > (
        candidates["fresh_alternative"]["outcome_feedback"]["score_delta"]
    )
    assert "historical_outcome_feedback_positive" in candidates["feedback_winner"]["reasons"]


def _optimizer_preview_candidate_payload(
    *,
    strategy_id: str,
    run_id: str,
    optimizer_candidate_id: str,
    preview_status: str,
    outcome_score: float,
    risk_flags: list[str] | None = None,
) -> dict[str, object]:
    return {
        "gene_id": "gene_nerya_strategy_drawdown_review",
        "summary": f"{optimizer_candidate_id} {preview_status} preview",
        "evidence_refs": [
            f"strategy_tuning:{run_id}",
            f"file:evolution/optimizer_runs/{run_id}/candidates/{optimizer_candidate_id}/backtest_preview.json",
        ],
        "validation_results": [
            {
                "type": "candidate_backtest_preview",
                "status": preview_status,
                "candidate_id": optimizer_candidate_id,
                "evidence_refs": [
                    f"file:evolution/optimizer_runs/{run_id}/candidates/{optimizer_candidate_id}/backtest_preview.json"
                ],
            }
        ],
        "outcome_score": outcome_score,
        "promotion_ref": f"strategy_tuning:{run_id}:candidate:{optimizer_candidate_id}:backtest",
        "strategy_id": strategy_id,
        "metadata": {
            "origin": "strategy_optimizer_preview",
            "optimizer_run_id": run_id,
            "optimizer_candidate_id": optimizer_candidate_id,
            "preview_type": "backtest",
            "preview_status": preview_status,
            "validation_types": ["manual_review"],
            "materialized_files": [f"strategies/{strategy_id}/main.py"],
            "accepted_targets": ["main.py"],
            "risk_flags": risk_flags or [],
            "reasons": ["materialized_files:1", "validation_step:manual_review"],
        },
    }


def test_strategy_tuning_candidate_scoring_uses_operator_candidate_decisions(
    tmp_path,
    monkeypatch,
):
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={"runtime": {"mock_mode": True}})
    _seed_strategy(paths)
    promoted = evolution_assets.create_candidate(
        paths,
        kind="capsule",
        summary="Promoted positive optimizer preview",
        payload=_optimizer_preview_candidate_payload(
            strategy_id="alpha",
            run_id="tune_promoted_keep",
            optimizer_candidate_id="operator_keep",
            preview_status="passed",
            outcome_score=0.7,
        ),
        evidence_refs=["strategy_tuning:tune_promoted_keep"],
        strategy_id="alpha",
    )
    rejected = evolution_assets.create_candidate(
        paths,
        kind="capsule",
        summary="Rejected positive optimizer preview",
        payload=_optimizer_preview_candidate_payload(
            strategy_id="alpha",
            run_id="tune_rejected_drop",
            optimizer_candidate_id="operator_drop",
            preview_status="passed",
            outcome_score=0.7,
        ),
        evidence_refs=["strategy_tuning:tune_rejected_drop"],
        strategy_id="alpha",
    )
    assert evolution_assets.promote_candidate(paths, promoted["id"], operator="test")["ok"] is True
    assert evolution_assets.reject_candidate(
        paths,
        rejected["id"],
        reason="operator prefers alternative",
        operator="test",
    )["ok"] is True

    def fake_dispatch(self, name, *, payload, **kwargs):  # noqa: ANN001, ANN202
        return {
            "ok": True,
            "output": {
                "summary": "choose using candidate decisions",
                "candidates": [
                    {
                        "id": "neutral_choice",
                        "summary": "No operator decision history.",
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": "def run(ctx):\n    return {'ok': 'neutral'}\n",
                            }
                        ],
                        "validation_plan": ["manual_review"],
                    },
                    {
                        "id": "operator_keep",
                        "summary": "Matches the promoted positive preview.",
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": "def run(ctx):\n    return {'ok': 'keep'}\n",
                            }
                        ],
                        "validation_plan": ["manual_review"],
                    },
                    {
                        "id": "operator_drop",
                        "summary": "Matches the rejected positive preview.",
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": "def run(ctx):\n    return {'ok': 'drop'}\n",
                            }
                        ],
                        "validation_plan": ["manual_review"],
                    },
                ],
            },
        }

    monkeypatch.setattr(
        "nerya.subagents.dispatcher.SubAgentDispatcher.dispatch",
        fake_dispatch,
    )

    result = StrategyEvolutionRunner(
        config=config,
        skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
    ).run_once("alpha", operator="test", dry_run=False)

    assert result.status == "ok"
    assert result.subagent_output["candidate_id"] == "operator_keep"
    report = result.optimizer_report
    feedback = report["outcome_feedback"]
    assert feedback["sample_count"] == 2
    assert feedback["candidate_decision_samples"] == 2
    assert feedback["candidate_decision_positive_samples"] == 1
    assert feedback["candidate_decision_negative_samples"] == 1
    assert any(
        example.get("source") == "asset_candidate_decision"
        for example in feedback["examples"]
    )
    candidates = {row["candidate_id"]: row for row in report["candidates"]}
    assert candidates["operator_keep"]["score"] > candidates["neutral_choice"]["score"]
    assert candidates["operator_drop"]["score"] < candidates["neutral_choice"]["score"]
    assert candidates["operator_keep"]["outcome_feedback"]["score_delta"] > 0
    assert candidates["operator_drop"]["outcome_feedback"]["score_delta"] < (
        candidates["neutral_choice"]["outcome_feedback"]["score_delta"]
    )
    keep_match = candidates["operator_keep"]["outcome_feedback"]["matched_features"][0]
    assert keep_match["sources"]["asset_candidate_decision"] == 1
    assert keep_match["examples"][0]["source"] == "asset_candidate_decision"
    assert keep_match["examples"][0]["asset_candidate_id"] == promoted["id"]
    assert keep_match["examples"][0]["feedback_policy"] == "promoted_positive_preview_reward"
    assert 0 < keep_match["examples"][0]["feedback_weighting"]["decay_weight"] <= 1
    assert "strategy_tuning:tune_promoted_keep" in keep_match["examples"][0]["evidence_refs"]
    assert "historical_outcome_feedback_positive" in candidates["operator_keep"]["reasons"]
    assert "historical_outcome_feedback_negative" in candidates["operator_drop"]["reasons"]


def test_promoted_negative_candidate_decision_penalizes_matching_risk_feature(
    tmp_path,
    monkeypatch,
):
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={"runtime": {"mock_mode": True}})
    _seed_strategy(paths)
    cautionary = evolution_assets.create_candidate(
        paths,
        kind="capsule",
        summary="Promoted cautionary optimizer preview",
        payload=_optimizer_preview_candidate_payload(
            strategy_id="alpha",
            run_id="tune_promoted_caution",
            optimizer_candidate_id="failed_pattern",
            preview_status="failed",
            outcome_score=-0.7,
            risk_flags=["overfit_entry"],
        ),
        evidence_refs=["strategy_tuning:tune_promoted_caution"],
        strategy_id="alpha",
    )
    assert evolution_assets.promote_candidate(paths, cautionary["id"], operator="test")["ok"] is True

    def fake_dispatch(self, name, *, payload, **kwargs):  # noqa: ANN001, ANN202
        return {
            "ok": True,
            "output": {
                "summary": "avoid known cautionary risk",
                "candidates": [
                    {
                        "id": "safe_shape",
                        "summary": "Same file without known risk.",
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": "def run(ctx):\n    return {'ok': 'safe'}\n",
                            }
                        ],
                        "validation_plan": ["manual_review"],
                    },
                    {
                        "id": "risky_retry",
                        "summary": "Repeats a promoted cautionary risk.",
                        "risk_flags": ["overfit_entry"],
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": "def run(ctx):\n    return {'ok': 'risky'}\n",
                            }
                        ],
                        "validation_plan": ["manual_review"],
                    },
                ],
            },
        }

    monkeypatch.setattr(
        "nerya.subagents.dispatcher.SubAgentDispatcher.dispatch",
        fake_dispatch,
    )

    result = StrategyEvolutionRunner(
        config=config,
        skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
    ).run_once("alpha", operator="test", dry_run=False)

    assert result.status == "ok"
    candidates = {row["candidate_id"]: row for row in result.optimizer_report["candidates"]}
    assert candidates["risky_retry"]["outcome_feedback"]["score_delta"] < 0
    assert "historical_outcome_feedback_negative" in candidates["risky_retry"]["reasons"]
    assert any(
        feature["feature"] == "risk:overfit_entry"
        for feature in candidates["risky_retry"]["outcome_feedback"]["matched_features"]
    )
    risk_match = [
        feature for feature in candidates["risky_retry"]["outcome_feedback"]["matched_features"]
        if feature["feature"] == "risk:overfit_entry"
    ][0]
    assert risk_match["sources"]["asset_candidate_decision"] == 1
    assert risk_match["examples"][0]["feedback_policy"] == "promoted_negative_preview_caution_penalty"


def test_operator_candidate_decision_feedback_decays_and_caps_feature_weight(
    tmp_path,
    monkeypatch,
):
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={"runtime": {"mock_mode": True}})
    _seed_strategy(paths)
    fixed_now = datetime(2026, 6, 17, tzinfo=timezone.utc)

    def promote_decision(run_id: str, clock: datetime) -> None:
        set_clock(lambda: clock)
        candidate = evolution_assets.create_candidate(
            paths,
            kind="capsule",
            summary=f"Promoted optimizer preview {run_id}",
            payload=_optimizer_preview_candidate_payload(
                strategy_id="alpha",
                run_id=run_id,
                optimizer_candidate_id="operator_keep",
                preview_status="passed",
                outcome_score=0.7,
            ),
            evidence_refs=[f"strategy_tuning:{run_id}"],
            strategy_id="alpha",
        )
        assert evolution_assets.promote_candidate(paths, candidate["id"], operator="test")["ok"] is True

    try:
        promote_decision("tune_old_keep", datetime(2025, 12, 20, tzinfo=timezone.utc))
        for index in range(3):
            promote_decision(f"tune_fresh_keep_{index}", fixed_now)
        set_clock(lambda: fixed_now)

        def fake_dispatch(self, name, *, payload, **kwargs):  # noqa: ANN001, ANN202
            return {
                "ok": True,
                "output": {
                    "summary": "choose using capped decision feedback",
                    "candidates": [
                        {
                            "id": "neutral_choice",
                            "summary": "Same target without candidate-id decision history.",
                            "proposed_changes": [
                                {
                                    "file": "main.py",
                                    "kind": "full_file",
                                    "after_content": "def run(ctx):\n    return {'ok': 'neutral'}\n",
                                }
                            ],
                            "validation_plan": ["manual_review"],
                        },
                        {
                            "id": "operator_keep",
                            "summary": "Matches repeated promoted operator decisions.",
                            "proposed_changes": [
                                {
                                    "file": "main.py",
                                    "kind": "full_file",
                                    "after_content": "def run(ctx):\n    return {'ok': 'keep'}\n",
                                }
                            ],
                            "validation_plan": ["manual_review"],
                        },
                    ],
                },
            }

        monkeypatch.setattr(
            "nerya.subagents.dispatcher.SubAgentDispatcher.dispatch",
            fake_dispatch,
        )

        result = StrategyEvolutionRunner(
            config=config,
            skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
        ).run_once("alpha", operator="test", dry_run=False)
    finally:
        reset_clock()

    assert result.status == "ok"
    report = result.optimizer_report
    feedback = report["outcome_feedback"]
    assert feedback["candidate_decision_samples"] == 4
    assert feedback["decision_feedback_policy"]["half_life_days"] == 45.0
    assert feedback["decision_feedback_policy"]["feature_source_cap"] == 1.2
    old_example = [
        example for example in feedback["examples"]
        if example.get("run_id") == "tune_old_keep"
    ][0]
    fresh_example = [
        example for example in feedback["examples"]
        if example.get("run_id") == "tune_fresh_keep_2"
    ][0]
    assert old_example["feedback_weighting"]["decay_weight"] < 0.1
    assert old_example["feedback_score"] < 0.05
    assert fresh_example["feedback_weighting"]["decay_weight"] == pytest.approx(1.0)

    candidates = {row["candidate_id"]: row for row in report["candidates"]}
    keep_match = [
        feature for feature in candidates["operator_keep"]["outcome_feedback"]["matched_features"]
        if feature["feature"] == "candidate_id:operator_keep"
    ][0]
    assert keep_match["positive_by_source"]["asset_candidate_decision"] > 1.2
    assert keep_match["positive"] == pytest.approx(1.2)
    assert keep_match["source_caps"]["asset_candidate_decision"] == 1.2
    assert candidates["operator_keep"]["score"] > candidates["neutral_choice"]["score"]


def test_optimizer_feedback_calibration_downweights_low_confidence_matches():
    matched_feature = {
        "candidate_id:operator_keep": {
            "feature": "candidate_id:operator_keep",
            "positive": 1.0,
            "negative": 0.0,
            "net": 1.0,
            "samples": 1,
            "sources": {"proposal_outcome": 1},
        }
    }
    candidate = {
        "candidate_id": "operator_keep",
        "proposed_changes": [
            {
                "file": "main.py",
                "kind": "full_file",
                "after_content": "def run(ctx):\n    return {'ok': 'keep'}\n",
            }
        ],
    }
    low_confidence = {
        "version": "optimizer_outcome_feedback_v1",
        "run_count": 1,
        "sample_count": 1,
        "positive_samples": 1,
        "negative_samples": 0,
        "neutral_samples": 0,
        "proposal_samples": 1,
        "candidate_decision_samples": 0,
        "features": matched_feature,
    }
    high_confidence = {
        "version": "optimizer_outcome_feedback_v1",
        "run_count": 4,
        "sample_count": 6,
        "positive_samples": 3,
        "negative_samples": 2,
        "neutral_samples": 1,
        "proposal_samples": 4,
        "candidate_decision_samples": 2,
        "features": matched_feature,
    }

    low_delta, low_match = _score_optimizer_outcome_feedback(
        output=candidate,
        accepted=[],
        materialized=[],
        validation_types=[],
        risk_flags=[],
        outcome_feedback=low_confidence,
    )
    high_delta, high_match = _score_optimizer_outcome_feedback(
        output=candidate,
        accepted=[],
        materialized=[],
        validation_types=[],
        risk_flags=[],
        outcome_feedback=high_confidence,
    )

    assert high_match["calibration_status"] == "calibrated"
    assert high_match["calibration_confidence"] == "high"
    assert high_match["calibration_scale"] == pytest.approx(1.0)
    assert high_match["raw_score_delta"] == pytest.approx(3.0)
    assert high_delta == pytest.approx(3.0)

    assert low_match["calibration_status"] == "needs_more_evidence"
    assert low_match["calibration_confidence"] == "low"
    assert "low_sample_count" in low_match["calibration_warnings"]
    assert "single_run_feedback" in low_match["calibration_warnings"]
    assert 0 < low_match["calibration_scale"] < 0.25
    assert low_match["raw_score_delta"] == pytest.approx(high_match["raw_score_delta"])
    assert 0 < low_delta < high_delta


def test_strategy_tuning_persists_prompt_audit_and_timeline(tmp_path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={"runtime": {"mock_mode": True}})
    root = paths.strategy("alpha")
    yaml_io.dump(
        root / "strategy.yml",
        {
            "version": 1,
            "strategy_id": "alpha",
            "title": "Alpha strategy",
            "mode": "paper",
            "entrypoint": "main.py:run",
            "markets": ["mock:BTC/USDT"],
            "accounts": ["paper_main"],
            "schedule": {"type": "cron", "cron": "*/5 * * * *"},
            "subagents": ["strategy_tuner"],
            "tuning": {
                "enabled": True,
                "schedule": {"type": "cron", "cron": "0 */6 * * *"},
                "objectives": ["return"],
                "tuning_prompt": "prefer fewer false positives",
                "proposal_policy": {"allowed_targets": ["main.py"]},
                "guardrails": {"require_backtest": False},
            },
        },
    )
    (root / "main.py").write_text("def run(ctx):\n    return {'ok': True}\n", encoding="utf-8")
    tuner_prompt = root / "subagents" / "strategy_tuner.agent.md"
    tuner_prompt.parent.mkdir(parents=True, exist_ok=True)
    tuner_prompt.write_text("Tune alpha with small patches only.", encoding="utf-8")
    paths.evolution_genes.parent.mkdir(parents=True, exist_ok=True)
    paths.evolution_genes.write_text(
        json.dumps(
            [
                {
                    "id": "gene_tuning_context",
                    "category": "strategy",
                    "signals_match": ["strategy_tuning_run"],
                    "preconditions": ["strategy_id_is_known"],
                    "strategy": ["reuse the latest strategy tuning lessons"],
                    "validation": ["manual_review"],
                    "confidence": 0.77,
                    "summary": "Reuse prior tuning context for strategy adjustments.",
                }
            ]
        ),
        encoding="utf-8",
    )
    jsonl.append(
        paths.evolution_capsules,
        {
            "id": "cap_prior_regression",
            "gene_id": "gene_tuning_context",
            "summary": "Prior widening change regressed after application.",
            "evidence_refs": ["proposal:prp_regressed"],
            "validation_results": [{"status": "failed"}],
            "outcome_score": -0.7,
            "strategy_id": "alpha",
        },
        stamp=False,
    )
    observation_proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="alpha runtime observation",
        initial_state="applied",
        metadata={"strategy_id": "alpha"},
    )
    recorded = record_post_apply_observation(
        paths,
        proposal_id=observation_proposal.id,
        status="regressed",
        source="strategy_run_paper",
        observed_at="2026-06-17T00:00:00+00:00",
        summary="paper run widened drawdown after apply",
        metrics={"mode": "paper", "run_status": "error"},
        evidence_refs=["file:strategies/alpha/runs/run_regressed.json"],
        run_id="run_regressed",
    )
    assert recorded["ok"] is True

    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class FakeLLM:
        def call(self, **kwargs):  # noqa: ANN201
            assert "Tune alpha with small patches only." in kwargs["prompt"]
            assert "prefer fewer false positives" in kwargs["prompt"]
            assert "Strategy tuning materialization contract" in kwargs["prompt"]
            assert "after_content" in kwargs["prompt"]
            assert "config_after" in kwargs["prompt"]
            assert "materializable_output_contract" in kwargs["prompt"]
            return LLMCall(
                tier="high",
                task=kwargs["task"],
                caller=kwargs["caller"],
                tokens=17,
                usd=0.001,
                raw=(
                    '{"summary":"tighten signal filter",'
                    '"proposed_changes":[{"file":"main.py","kind":"code_patch","rationale":"reduce noise"}],'
                    '"validation_plan":["manual_review"],'
                    '"done":true}'
                ),
                parsed={
                    "summary": "tighten signal filter",
                    "proposed_changes": [
                        {"file": "main.py", "kind": "code_patch", "rationale": "reduce noise"}
                    ],
                    "validation_plan": ["manual_review"],
                    "done": True,
                },
                provider="fake",
                model="fake-model",
            )

    original_runtime_run = SubAgentRuntime.run

    def fake_runtime(self, spec, **kwargs):  # noqa: ANN001, ANN202
        runtime = SubAgentRuntime(
            config=self.config,
            skills=self.skills,
            llm=FakeLLM(),
            tool_registry=self.tool_registry,
        )
        return original_runtime_run(runtime, spec, **kwargs)

    monkeypatch.setattr("nerya.subagents.dispatcher.SubAgentRuntime.run", fake_runtime)

    result = StrategyEvolutionRunner(config=config, skills=FakeSkills()).run_once(
        "alpha",
        operator="test",
        note="audit test",
        dry_run=True,
    )

    assert result.status == "ok"
    assert result.optimizer_report == {}
    assert result.audit_path
    audit_path = root / "reviews" / f"tuning_{result.run_id}_audit.json"
    assert audit_path.exists()
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "Tune alpha with small patches only." in audit_text
    assert "prefer fewer false positives" in audit_text
    assert "tighten signal filter" in audit_text
    audit = json.loads(audit_text)
    assert audit["provider"] == "fake"
    assert audit["model"] == "fake-model"
    assert audit["model_calls"][0]["model"] == "fake-model"
    contract = audit["payload"]["materializable_output_contract"]
    assert contract["version"] == "strategy_tuning_materializable_output_v1"
    assert contract["required_for_applyable_changes"] is True
    assert any("after_content" in row for row in contract["accepted_change_shapes"])
    subagent_runs = [
        row
        for row in jsonl.read_all(paths.journal("agent"))
        if row.get("kind") == "subagent.run"
        and row.get("name") == "strategy_tuner"
        and row.get("session_id") == result.run_id
    ]
    assert subagent_runs
    assert subagent_runs[-1]["provider"] == "fake"
    assert subagent_runs[-1]["model"] == "fake-model"
    assert subagent_runs[-1]["model_calls"][0]["model"] == "fake-model"

    route_map = {(method, path): handler for method, path, handler in routes_evolution.routes()}
    monkeypatch.setattr(
        "nerya.evolution.timeline._strategy_tuning_llm_calls",
        lambda _paths: pytest.fail("direct model metadata should not scan legacy llm journal"),
    )
    out = route_map[("POST", "/evolution/timeline")](
        SimpleNamespace(config=config),
        {"strategy_id": "alpha", "limit": 50},
    )
    linked = [
        item for item in out["timeline"]
        if f"strategy_tuning:{result.run_id}" in item.get("evidence_refs", [])
    ]
    assert linked
    process = linked[0]["process"]
    assert process["has_prompt"] is True
    assert process["has_inputs"] is True
    assert process["has_outputs"] is True
    assert process["run"]["subagent"] == "strategy_tuner"
    assert process["run"]["provider"] == "fake"
    assert process["run"]["model"] == "fake-model"
    assert process["run"]["model_metadata_source"] is None
    assert process["run"]["tier"] == "medium"
    titles = [
        artifact["title"]
        for section in process["sections"]
        for artifact in section["artifacts"]
    ]
    assert "Role prompt" in titles
    assert "Subagent payload" in titles
    assert "Market & risk context" in titles
    assert "Runtime feedback" in titles
    assert "Reused evolution assets" in titles
    assert "Subagent output" in titles
    decision_context = next(
        artifact
        for section in process["sections"]
        for artifact in section["artifacts"]
        if artifact["title"] == "Market & risk context"
    )
    context_payload = json.loads(decision_context["preview"])
    assert context_payload["strategy_id"] == "alpha"
    assert context_payload["market_context"]["markets"] == ["mock:BTC/USDT"]
    assert context_payload["market_context"]["items"][0]["market"] == "mock:BTC/USDT"
    assert context_payload["trade_metrics"]["pnl_total_usd"] == 0.0
    assert context_payload["risk_metrics"]["risk_rejects"] == 0
    feedback = next(
        artifact
        for section in process["sections"]
        for artifact in section["artifacts"]
        if artifact["title"] == "Runtime feedback"
    )
    feedback_payload = json.loads(feedback["preview"])
    assert feedback_payload["negative_count"] == 1
    assert feedback_payload["weighted_negative_count"] == 1.0
    assert feedback_payload["recent_observations"][0]["run_id"] == "run_regressed"
    reused = next(
        artifact
        for section in process["sections"]
        for artifact in section["artifacts"]
        if artifact["title"] == "Reused evolution assets"
    )
    assert reused["kind"] == "asset"
    reused_payload = json.loads(reused["preview"])
    assert reused_payload["counts"]["genes"] >= 1
    assert reused_payload["counts"]["negative_capsules"] == 1
    reused_gene_ids = {row["id"] for row in reused_payload["genes"]}
    assert "gene_tuning_context" in reused_gene_ids
    assert all(row["gdi"]["version"] == "gdi_v1" for row in reused_payload["genes"])
    assert reused_payload["negative_capsules"][0]["id"] == "cap_prior_regression"
