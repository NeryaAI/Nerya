from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from nerya.api import routes_strategies_runtime
from nerya.cli.commands import strategy as strategy_cli
from nerya.core import jsonl, yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.core.time import reset_clock, set_clock
from nerya.strategies.package import load_package
from nerya.strategies import performance as performance_module
from nerya.strategies import evolution as strategy_evolution_module
from nerya.strategies.evolution import (
    StrategyEvolutionRunner,
    _select_tuning_assets,
)
from nerya.strategies.review_context import (
    StrategyReviewPolicy,
    build_strategy_review_context,
)
from nerya.strategies.state import StrategyRunRecord, StrategyRunStore
from nerya.llm.gateway import LLMCall
from nerya.sdk.strategy_api import StrategyTuningAPI
from nerya.subagents.registry import SubAgentSpec
from nerya.subagents.runtime import SubAgentRuntime
from nerya.subagents.strategy_registry import StrategySubAgentRegistry
from nerya.tools.native.strategy_runtime import strategy_tuning_run_handler
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke
_NOW = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)


def _seed_package(paths: WorkspacePaths, strategy_id: str = "alpha"):
    root = paths.strategy(strategy_id)
    yaml_io.dump(
        root / "strategy.yml",
        {
            "version": 1,
            "strategy_id": strategy_id,
            "mode": "paper",
            "entrypoint": "main.py:run",
            "schedule": {"type": "cron", "cron": "*/5 * * * *"},
        },
    )
    (root / "main.py").write_text(
        "def run(ctx):\n    return ctx.result.hold(reason='test')\n",
        encoding="utf-8",
    )
    return load_package(paths, strategy_id)


def _seed_evidence(
    paths: WorkspacePaths,
    *,
    package_hash: str,
    run_id: str,
    session_id: str,
    age_hours: float,
    pnl_usd: float,
    mode: str = "paper",
    strategy_id: str = "alpha",
    owner: str = "alpha",
) -> None:
    at = _NOW - timedelta(hours=age_hours)
    StrategyRunStore(paths, owner).write(
        StrategyRunRecord(
            run_id=run_id,
            strategy_id=strategy_id,
            package_hash=package_hash,
            started_at=at.isoformat(),
            finished_at=at.isoformat(),
            duration_ms=10,
            status="ok",
            mode=mode,
            session_id=session_id,
        )
    )
    identity = {
        "strategy_id": strategy_id,
        "run_id": run_id,
        "session_id": session_id,
        "package_hash": package_hash,
        "mode": mode,
        "ts": at.isoformat(),
    }
    history = paths.strategy_history(owner)
    jsonl.append(
        history / "pnl.jsonl",
        {**identity, "pnl": {"realized_usd": pnl_usd}},
        stamp=False,
    )
    jsonl.append(
        history / "risk.jsonl",
        {**identity, "risk_decision": {"verdict": "reject"}},
        stamp=False,
    )
    jsonl.append(
        history / "subagents.jsonl",
        {**identity, "name": f"review_{run_id}", "output": {}},
        stamp=False,
    )
    jsonl.append(
        paths.journal("evolution"),
        {
            **identity,
            "kind": "proposal.post_apply_observation",
            "proposal_id": f"proposal_{run_id}",
            "status": "observing",
            "source": f"strategy_run_{mode}",
            "observed_at": at.isoformat(),
            "metrics": {"package_hash": package_hash, "mode": mode},
        },
        stamp=False,
    )


def test_strategy_review_uses_only_frozen_active_run_evidence(tmp_path):
    paths = WorkspacePaths(tmp_path)
    package = _seed_package(paths)
    active_hash = package.content_hash
    try:
        set_clock(lambda: _NOW)
        _seed_evidence(paths, package_hash=active_hash, run_id="run_keep_newest", session_id="ses_keep_newest", age_hours=1, pnl_usd=11)
        _seed_evidence(paths, package_hash=active_hash, run_id="run_keep_second", session_id="ses_keep_second", age_hours=2, pnl_usd=7)
        _seed_evidence(paths, package_hash=active_hash, run_id="run_over_limit", session_id="ses_over_limit", age_hours=3, pnl_usd=5)
        _seed_evidence(paths, package_hash="old-package-hash", run_id="run_old_package", session_id="ses_old_package", age_hours=0.5, pnl_usd=1_000)
        _seed_evidence(paths, package_hash=active_hash, run_id="run_shadow", session_id="ses_shadow", age_hours=0.4, pnl_usd=2_000, mode="shadow")
        _seed_evidence(paths, package_hash=active_hash, run_id="run_expired", session_id="ses_expired", age_hours=25, pnl_usd=3_000)
        _seed_evidence(paths, package_hash=active_hash, run_id="run_foreign_strategy", session_id="ses_foreign_strategy", age_hours=0.3, pnl_usd=4_000, strategy_id="beta")
        jsonl.append(
            paths.strategy_history("alpha") / "pnl.jsonl",
            {"pnl": {"realized_usd": 9_000}, "ts": _NOW.isoformat()},
            stamp=False,
        )

        snapshot = build_strategy_review_context(
            paths,
            package,
            policy=StrategyReviewPolicy(
                lookback_runs=2,
                max_age_hours=24,
                execution_mode="paper",
            ),
        )
    finally:
        reset_clock()

    assert snapshot.runs_considered == 2
    assert snapshot.run_metrics["modes"] == {"paper": 2}
    assert snapshot.trade_metrics["pnl_total_usd"] == 18
    assert snapshot.risk_metrics["risk_rows"] == 2
    assert snapshot.cost_metrics["subagent_invocations"] == 2
    assert snapshot.evolution_context["post_apply_observation_count"] == 2
    assert snapshot.evidence_scope["selected_run_ids"] == [
        "run_keep_newest",
        "run_keep_second",
    ]
    assert snapshot.evidence_scope["selected_session_ids"] == [
        "ses_keep_newest",
        "ses_keep_second",
    ]
    assert snapshot.evidence_scope["excluded_run_counts"] == {
        "execution_mode": 1,
        "lookback_limit": 1,
        "max_age": 1,
        "package_hash": 1,
        "strategy_id": 1,
    }
    assert snapshot.evidence_scope["excluded_ledger_counts"]["pnl"]["unattributed"] == 1


def test_explicit_run_and_session_ids_only_narrow_the_review_scope(tmp_path):
    paths = WorkspacePaths(tmp_path)
    package = _seed_package(paths)
    try:
        set_clock(lambda: _NOW)
        _seed_evidence(paths, package_hash=package.content_hash, run_id="run_a", session_id="ses_a", age_hours=2, pnl_usd=11)
        _seed_evidence(paths, package_hash=package.content_hash, run_id="run_b", session_id="ses_b", age_hours=1, pnl_usd=22)
        _seed_evidence(paths, package_hash="old-package-hash", run_id="run_old", session_id="ses_old", age_hours=0.5, pnl_usd=999)

        selected = build_strategy_review_context(
            paths,
            package,
            policy=StrategyReviewPolicy(execution_mode="paper", run_ids=("run_b",)),
        )
        mismatched = build_strategy_review_context(
            paths,
            package,
            policy=StrategyReviewPolicy(
                execution_mode="paper",
                run_ids=("run_b",),
                session_ids=("ses_a",),
            ),
        )
        old_package = build_strategy_review_context(
            paths,
            package,
            policy=StrategyReviewPolicy(execution_mode="paper", run_ids=("run_old",)),
        )
    finally:
        reset_clock()

    assert selected.evidence_scope["selected_run_ids"] == ["run_b"]
    assert selected.trade_metrics["pnl_total_usd"] == 22
    assert mismatched.runs_considered == 0
    assert mismatched.trade_metrics["pnl_total_usd"] == 0
    assert old_package.runs_considered == 0
    assert old_package.trade_metrics["pnl_total_usd"] == 0


def test_tuning_run_uses_the_manifest_review_window(tmp_path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    _seed_package(paths)
    manifest_path = paths.strategy("alpha") / "strategy.yml"
    manifest = yaml_io.load(manifest_path)
    manifest["tuning"] = {
        "enabled": True,
        "schedule": {"type": "cron", "cron": "0 */6 * * *"},
        "lookback": {"runs": 1, "max_age_hours": 24},
        "guardrails": {"require_backtest": False},
    }
    yaml_io.dump(manifest_path, manifest)
    package = load_package(paths, "alpha")
    try:
        set_clock(lambda: _NOW)
        _seed_evidence(
            paths,
            package_hash=package.content_hash,
            run_id="run_active",
            session_id="ses_active",
            age_hours=1,
            pnl_usd=13,
        )
        _seed_evidence(
            paths,
            package_hash=package.content_hash,
            run_id="run_active_newer",
            session_id="ses_active_newer",
            age_hours=0.75,
            pnl_usd=17,
        )
        _seed_evidence(
            paths,
            package_hash="old-package-hash",
            run_id="run_old_package_newer",
            session_id="ses_old_package_newer",
            age_hours=0.5,
            pnl_usd=999,
        )

        monkeypatch.setattr(
            StrategyEvolutionRunner,
            "_dispatch_tuner",
            lambda self, **kwargs: {
                "ok": True,
                "output": {"proposed_changes": []},
            },
        )
        result = StrategyEvolutionRunner(
            config=Config(paths=paths, data={"runtime": {"mock_mode": True}}),
            skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
        ).run_once(
            "alpha",
            dry_run=True,
            evidence_run_ids=("run_active",),
            evidence_session_ids=("ses_active",),
        )
    finally:
        reset_clock()

    assert result.snapshot is not None
    assert result.snapshot["runs_considered"] == 1
    assert result.snapshot["trade_metrics"]["pnl_total_usd"] == 13
    assert result.snapshot["evidence_scope"]["execution_mode"] == "paper"
    assert result.snapshot["evidence_scope"]["max_age_hours"] == 24
    assert result.snapshot["evidence_scope"]["selected_run_ids"] == ["run_active"]
    source = result.snapshot["package_context"]["files"]["main.py"]
    assert "return ctx.result.hold" in source["content"]
    assert len(source["sha256"]) == 64
    assert result.snapshot["package_context"]["package_hash"] == package.content_hash


def test_http_tuning_run_forwards_explicit_evidence_scope():
    captured: dict[str, object] = {}

    def run(strategy_id, **kwargs):  # noqa: ANN001, ANN202
        captured["strategy_id"] = strategy_id
        captured["kwargs"] = kwargs
        return {"status": "ok"}

    route_map = {
        (method, path): handler
        for method, path, handler in routes_strategies_runtime.routes()
    }
    response = route_map[("POST", "/strategies/runtime/tuning/run")](
        SimpleNamespace(
            strategy=SimpleNamespace(tuning=SimpleNamespace(run=run))
        ),
        {
            "strategy_id": "alpha",
            "evidence_run_ids": ["run_a", "run_b"],
            "evidence_session_ids": "session_a",
        },
    )

    assert response["ok"] is True
    assert captured["strategy_id"] == "alpha"
    assert captured["kwargs"]["evidence_run_ids"] == ("run_a", "run_b")
    assert captured["kwargs"]["evidence_session_ids"] == ("session_a",)


def test_cli_tuning_run_forwards_repeatable_evidence_filters(monkeypatch):
    parser = argparse.ArgumentParser()
    strategy_cli.register(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(
        [
            "strategy",
            "tuning",
            "run",
            "alpha",
            "--evidence-run-id",
            "run_a",
            "--evidence-run-id",
            "run_b",
            "--evidence-session-id",
            "session_a",
        ]
    )
    captured: dict[str, object] = {}

    def run(strategy_id, **kwargs):  # noqa: ANN001, ANN202
        captured["strategy_id"] = strategy_id
        captured["kwargs"] = kwargs
        return {"status": "ok"}

    monkeypatch.setattr(
        strategy_cli,
        "_client",
        lambda *_args, **_kwargs: SimpleNamespace(
            strategy=SimpleNamespace(tuning=SimpleNamespace(run=run))
        ),
    )

    assert args.func(args) == 0
    assert captured["strategy_id"] == "alpha"
    assert captured["kwargs"]["evidence_run_ids"] == ("run_a", "run_b")
    assert captured["kwargs"]["evidence_session_ids"] == ("session_a",)


def test_sdk_and_native_tuning_run_forward_explicit_evidence_scope(
    tmp_path,
    monkeypatch,
):
    calls: list[dict[str, object]] = []

    class FakeRunner:
        def __init__(self, **kwargs):  # noqa: ANN003
            pass

        def run_once(self, strategy_id, **kwargs):  # noqa: ANN001, ANN202
            calls.append({"strategy_id": strategy_id, **kwargs})
            return SimpleNamespace(asdict=lambda: {"status": "ok"})

    monkeypatch.setattr(
        "nerya.sdk.strategy_api.StrategyEvolutionRunner",
        FakeRunner,
    )
    monkeypatch.setattr(
        "nerya.tools.native.strategy_runtime.StrategyEvolutionRunner",
        FakeRunner,
    )
    config = Config(paths=WorkspacePaths(tmp_path), data={})
    sdk = StrategyTuningAPI(
        SimpleNamespace(config=config, skills=SimpleNamespace())
    )

    assert sdk.run(
        "alpha",
        evidence_run_ids=("run_a", "run_b"),
        evidence_session_ids=("session_a",),
    )["status"] == "ok"
    native_result = strategy_tuning_run_handler(
        ToolCall(
            id="tool_tuning_scope",
            name="strategy_tuning_run",
            arguments={
                "strategy_id": "alpha",
                "evidence_run_ids": ["run_c"],
                "evidence_session_ids": ["session_b", "session_c"],
            },
        ),
        config=config,
        skills=SimpleNamespace(),
    )

    assert native_result.is_error is False
    assert calls[0]["evidence_run_ids"] == ("run_a", "run_b")
    assert calls[0]["evidence_session_ids"] == ("session_a",)
    assert calls[1]["evidence_run_ids"] == ("run_c",)
    assert calls[1]["evidence_session_ids"] == ("session_b", "session_c")


def test_explicit_payload_only_context_cannot_read_session_memory(
    tmp_path,
):
    sentinel = "ordinary-session-memory-must-not-appear"

    class FakeSkillRegistry:
        def list(self):  # noqa: ANN201
            raise AssertionError(f"{sentinel}: skill registry")

    class FakeSkills:
        registry = FakeSkillRegistry()

    class FakeToolRegistry:
        def list_tools(self):  # noqa: ANN201
            raise AssertionError(f"{sentinel}: native tool registry")

    captured: dict[str, object] = {}

    class FakeLLM:
        def call(self, **kwargs):  # noqa: ANN201
            captured["prompt"] = kwargs["prompt"]
            captured["metadata"] = kwargs["metadata"]
            return LLMCall(
                tier="medium",
                task=kwargs["task"],
                caller=kwargs["caller"],
                tokens=1,
                usd=0.0,
                raw='{"analysis":"hold","proposed_changes":[],"done":true}',
                parsed={
                    "analysis": "hold",
                    "proposed_changes": [],
                    "done": True,
                },
                provider="fake",
                model="fake",
            )

    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(tmp_path), data={}),
        skills=FakeSkills(),
        llm=FakeLLM(),
        tool_registry=FakeToolRegistry(),
    )
    result = runtime.run(
        SubAgentSpec(
            name="strategy_tuner",
            prompt_path=tmp_path / "strategy_tuner.agent.md",
            prompt="Use only the frozen strategy evidence.",
            allowed_skills=["memory_search"],
        ),
        trigger_event_id=None,
        payload={"strategy_id": "alpha", "performance": {"runs_considered": 1}},
        strategy_id="alpha",
        session_id="tune_isolated",
        context_scope="explicit_payload_only",
    )

    assert result["audit"]["context_scope"] == "explicit_payload_only"
    assert result["audit"]["callable_skills"] == []
    assert result["audit"]["native_tools"] == []
    assert result["audit"]["context_chars"] == 0
    assert captured["metadata"]["context_scope"] == "explicit_payload_only"
    assert sentinel not in captured["prompt"]
    assert "memory_recall" not in captured["prompt"]


def test_tuner_dispatch_always_requests_explicit_payload_only_context(
    tmp_path,
    monkeypatch,
):
    paths = WorkspacePaths(tmp_path)
    package = _seed_package(paths)
    paths.evolution_genes.parent.mkdir(parents=True, exist_ok=True)
    paths.evolution_genes.write_text(
        json.dumps(
            [
                {
                    "id": "gene_global_memory_noise",
                    "category": "harden",
                    "signals_match": ["strategy_tuning_run"],
                    "preconditions": [],
                    "strategy": ["reuse ordinary session memory"],
                    "validation": [],
                    "confidence": 1.0,
                    "summary": "Must never enter strategy tuning.",
                },
                {
                    "id": "gene_other_strategy",
                    "category": "strategy",
                    "strategy_id": "beta",
                    "signals_match": ["strategy_tuning_run"],
                    "preconditions": [],
                    "strategy": ["reuse beta-only tuning history"],
                    "validation": [],
                    "confidence": 1.0,
                    "summary": "Must remain beta-only.",
                },
                {
                    "id": "gene_shared_strategy_method",
                    "category": "strategy",
                    "strategy_id": "alpha",
                    "metadata": {"package_hash": package.content_hash},
                    "signals_match": ["strategy_tuning_run"],
                    "preconditions": [],
                    "strategy": ["use strategy-local evidence"],
                    "validation": [],
                    "confidence": 0.95,
                    "summary": "Reusable method, with strategy-local usage scoring.",
                },
                {
                    "id": "gene_old_package_strategy",
                    "category": "strategy",
                    "strategy_id": "alpha",
                    "metadata": {"package_hash": "old-package-hash"},
                    "signals_match": ["strategy_tuning_run"],
                    "preconditions": [],
                    "strategy": ["reuse an obsolete package rule"],
                    "validation": [],
                    "confidence": 1.0,
                    "summary": "Old package gene must fail closed.",
                },
                {
                    "id": "gene_unowned_global_strategy",
                    "category": "strategy",
                    "signals_match": ["strategy_tuning_run"],
                    "preconditions": [],
                    "strategy": ["global workspace rule"],
                    "validation": [],
                    "confidence": 1.0,
                    "summary": "Custom global gene must opt into a strategy owner.",
                }
            ]
        ),
        encoding="utf-8",
    )
    jsonl.append(
        paths.evolution_events,
        {
            "id": "evt_beta_gene_usage",
            "strategy_id": "beta",
            "genes_used": ["gene_shared_strategy_method"],
            "metadata": {},
        },
        stamp=False,
    )
    jsonl.append(
        paths.evolution_events,
        {
            "id": "evt_old_package_gene_usage",
            "strategy_id": "alpha",
            "package_hash": "old-package-hash",
            "genes_used": ["gene_shared_strategy_method"],
            "metadata": {},
        },
        stamp=False,
    )
    jsonl.append(
        paths.evolution_capsules,
        {
            "id": "capsule_old_package",
            "strategy_id": "alpha",
            "gene_id": "gene_shared_strategy_method",
            "summary": "Old package evidence must not affect the active package.",
            "evidence_refs": [],
            "validation_results": [{"status": "passed"}],
            "outcome_score": 1.0,
            "metadata": {"package_hash": "old-package-hash"},
        },
        stamp=False,
    )
    jsonl.append(
        paths.evolution_capsules,
        {
            "id": "capsule_without_package_owner",
            "strategy_id": "alpha",
            "gene_id": "gene_shared_strategy_method",
            "summary": "Legacy evidence without a package owner must fail closed.",
            "evidence_refs": [],
            "validation_results": [{"status": "passed"}],
            "outcome_score": 1.0,
            "metadata": {},
        },
        stamp=False,
    )
    snapshot = build_strategy_review_context(
        paths,
        package,
        policy=StrategyReviewPolicy(execution_mode="paper"),
    )
    selected_assets = _select_tuning_assets(
        paths,
        package,
        snapshot,
        "tune_isolated",
    )
    paths.evolution_genes.write_text("[]", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_dispatch(self, target, *, payload, **kwargs):  # noqa: ANN001, ANN202
        captured["target"] = target
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return {"ok": True, "output": {"proposed_changes": []}}

    monkeypatch.setattr(
        "nerya.subagents.dispatcher.SubAgentDispatcher.dispatch",
        fake_dispatch,
    )
    StrategyEvolutionRunner(
        config=Config(paths=paths, data={}),
        skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
    )._dispatch_tuner(
        pkg=package,
        snapshot=snapshot,
        selected_assets=selected_assets,
        trigger_event_id=None,
        run_id="tune_isolated",
    )

    assert captured["target"] == "subagent:strategy_tuner"
    assert captured["kwargs"]["context_scope"] == "explicit_payload_only"
    assert captured["kwargs"]["session_id"] == "tune_isolated"
    assert not {
        "chat_history",
        "conversation",
        "memory",
        "operator_profile",
        "session_memory",
    }.intersection(captured["payload"])
    assert all(
        gene["category"] == "strategy"
        for gene in captured["payload"]["selected_genes"]
    )
    assert all(
        gene["id"] != "gene_global_memory_noise"
        for gene in captured["payload"]["selected_genes"]
    )
    assert all(
        gene["id"] != "gene_other_strategy"
        for gene in captured["payload"]["selected_genes"]
    )
    assert all(
        gene["id"] != "gene_unowned_global_strategy"
        for gene in captured["payload"]["selected_genes"]
    )
    assert all(
        gene["id"] != "gene_old_package_strategy"
        for gene in captured["payload"]["selected_genes"]
    )
    shared = next(
        gene
        for gene in captured["payload"]["selected_genes"]
        if gene["id"] == "gene_shared_strategy_method"
    )
    assert shared["gdi"]["usage_count"] == 0
    assert all(
        capsule["id"] != "capsule_old_package"
        for capsule in captured["payload"]["selected_assets"]["capsules"]
    )
    assert all(
        capsule["id"] != "capsule_without_package_owner"
        for capsule in captured["payload"]["selected_assets"]["capsules"]
    )


def test_strategy_review_does_not_fallback_to_unrelated_global_news(
    tmp_path,
    monkeypatch,
):
    paths = WorkspacePaths(tmp_path)
    _seed_package(paths)
    manifest_path = paths.strategy("alpha") / "strategy.yml"
    manifest = yaml_io.load(manifest_path)
    manifest["markets"] = ["mock:BTC/USDT"]
    yaml_io.dump(manifest_path, manifest)
    package = load_package(paths, "alpha")
    monkeypatch.setattr(
        performance_module,
        "mock_news",
        lambda: [
            {
                "source": "global_feed",
                "title": "ETH upgrade ships",
                "summary": "Ethereum-only update.",
                "tickers": ["ETH"],
            }
        ],
    )

    snapshot = build_strategy_review_context(
        paths,
        package,
        policy=StrategyReviewPolicy(execution_mode="paper"),
        config_like=Config(paths=paths, data={"runtime": {"mock_mode": True}}),
    )

    assert snapshot.news_context["symbols"] == ["BTC"]
    assert snapshot.news_context["count"] == 0
    assert snapshot.news_context["items"] == []


def test_frozen_package_context_respects_forbidden_targets(tmp_path):
    paths = WorkspacePaths(tmp_path)
    _seed_package(paths)
    daily_note = paths.strategy("alpha") / "notes" / "daily.md"
    daily_note.parent.mkdir(parents=True, exist_ok=True)
    daily_note.write_text("ordinary chat-derived note", encoding="utf-8")
    package = load_package(paths, "alpha")

    snapshot = build_strategy_review_context(
        paths,
        package,
        policy=StrategyReviewPolicy(
            execution_mode="paper",
            allowed_targets=("**/*", "main.py", "strategy.yml"),
            forbidden_targets=("notes/*",),
        ),
    )

    assert "notes/daily.md" not in snapshot.package_context["files"]
    assert snapshot.package_context["excluded_files"]["notes/daily.md"] == (
        "forbidden_target"
    )


def test_package_hash_ignores_generated_runtime_artifacts(tmp_path):
    paths = WorkspacePaths(tmp_path)
    package = _seed_package(paths)
    original_hash = package.content_hash
    root = paths.strategy("alpha")

    (root / "backtests" / "bt_1").mkdir(parents=True)
    (root / "backtests" / "bt_1" / "metrics.json").write_text(
        '{"total_return_pct": 1.5}',
        encoding="utf-8",
    )
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "main.cpython-313.pyc").write_bytes(b"cache")
    (root / "helpers" / "__pycache__").mkdir(parents=True)
    (root / "helpers" / "__pycache__" / "signals.pyc").write_bytes(b"cache")

    unchanged = load_package(paths, "alpha")
    assert unchanged.content_hash == original_hash
    assert not any(path.startswith("backtests/") for path in unchanged.files)
    assert not any("__pycache__" in path for path in unchanged.files)
    assert not any(path.endswith(".pyc") for path in unchanged.files)

    (root / "main.py").write_text(
        "def run(ctx):\n    return ctx.result.hold(reason='source changed')\n",
        encoding="utf-8",
    )

    assert load_package(paths, "alpha").content_hash != original_hash


def test_tuning_discards_candidate_if_package_changes_during_preview(
    tmp_path,
    monkeypatch,
):
    paths = WorkspacePaths(tmp_path)
    _seed_package(paths)
    manifest_path = paths.strategy("alpha") / "strategy.yml"
    manifest = yaml_io.load(manifest_path)
    manifest["tuning"] = {
        "enabled": True,
        "schedule": {"type": "cron", "cron": "0 */6 * * *"},
        "guardrails": {"require_backtest": False},
    }
    yaml_io.dump(manifest_path, manifest)

    def fake_dispatch(self, target, *, payload, **kwargs):  # noqa: ANN001, ANN202
        return {
            "ok": True,
            "output": {
                "candidates": [
                    {
                        "id": "candidate_a",
                        "summary": "candidate created from frozen evidence",
                        "proposed_changes": [
                            {
                                "file": "main.py",
                                "kind": "full_file",
                                "after_content": (
                                    "def run(ctx):\n"
                                    "    return ctx.result.hold(reason='candidate')\n"
                                ),
                            }
                        ],
                        "validation_plan": ["manual_review"],
                    }
                ]
            },
        }

    original_preview_files = strategy_evolution_module._candidate_preview_package_files

    def mutate_after_preview(package, after_files):  # noqa: ANN001, ANN202
        files = original_preview_files(package, after_files)
        (package.root / "main.py").write_text(
            "def run(ctx):\n    return ctx.result.hold(reason='concurrent edit')\n",
            encoding="utf-8",
        )
        return files

    monkeypatch.setattr(
        "nerya.subagents.dispatcher.SubAgentDispatcher.dispatch",
        fake_dispatch,
    )
    monkeypatch.setattr(
        strategy_evolution_module,
        "_candidate_preview_package_files",
        mutate_after_preview,
    )

    result = StrategyEvolutionRunner(
        config=Config(paths=paths, data={"runtime": {"mock_mode": True}}),
        skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
    ).run_once("alpha", dry_run=False)

    assert result.status == "error"
    assert result.error["kind"] == "package_changed"
    assert result.proposal_id is None


def test_strategy_tuner_never_falls_back_to_the_global_prompt(tmp_path):
    paths = WorkspacePaths(tmp_path)
    _seed_package(paths)
    manifest_path = paths.strategy("alpha") / "strategy.yml"
    manifest = yaml_io.load(manifest_path)
    manifest["tuning"] = {
        "enabled": True,
        "schedule": {"type": "cron", "cron": "0 */6 * * *"},
    }
    yaml_io.dump(manifest_path, manifest)
    paths.subagents.mkdir(parents=True, exist_ok=True)
    (paths.subagents / "strategy_tuner.agent.md").write_text(
        "GLOBAL SESSION-DERIVED TUNER PROMPT",
        encoding="utf-8",
    )

    spec = StrategySubAgentRegistry(
        paths=paths,
        strategy_id="alpha",
    ).get("strategy_tuner")

    assert "GLOBAL SESSION-DERIVED" not in spec.prompt
    assert "per-strategy self-evolution" in spec.prompt


def test_tuner_dispatch_uses_the_frozen_strategy_local_prompt(tmp_path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    _seed_package(paths)
    manifest_path = paths.strategy("alpha") / "strategy.yml"
    manifest = yaml_io.load(manifest_path)
    manifest["tuning"] = {
        "enabled": True,
        "schedule": {"type": "cron", "cron": "0 */6 * * *"},
        "subagent": {
            "name": "strategy_tuner",
            "prompt_file": "subagents/strategy_tuner.agent.md",
            "tier": "medium",
        },
    }
    yaml_io.dump(manifest_path, manifest)
    prompt_path = paths.strategy("alpha") / "subagents" / "strategy_tuner.agent.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("FROZEN STRATEGY-LOCAL PROMPT", encoding="utf-8")
    package = load_package(paths, "alpha")
    snapshot = build_strategy_review_context(
        paths,
        package,
        policy=StrategyReviewPolicy(
            execution_mode="paper",
            allowed_targets=package.manifest.tuning.allowed_targets,
            forbidden_targets=package.manifest.tuning.forbidden_targets,
        ),
    )
    selected_assets = _select_tuning_assets(
        paths,
        package,
        snapshot,
        "tune_frozen_prompt",
    )
    prompt_path.write_text("MUTATED AFTER SNAPSHOT", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_dispatch(self, target, *, payload, **kwargs):  # noqa: ANN001, ANN202
        captured["inline_spec"] = kwargs.get("inline_spec")
        return {"ok": True, "output": {"proposed_changes": []}}

    monkeypatch.setattr(
        "nerya.subagents.dispatcher.SubAgentDispatcher.dispatch",
        fake_dispatch,
    )
    StrategyEvolutionRunner(
        config=Config(paths=paths, data={}),
        skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
    )._dispatch_tuner(
        pkg=package,
        snapshot=snapshot,
        selected_assets=selected_assets,
        trigger_event_id=None,
        run_id="tune_frozen_prompt",
    )

    spec = captured["inline_spec"]
    assert spec.prompt == "FROZEN STRATEGY-LOCAL PROMPT"
    assert "MUTATED AFTER SNAPSHOT" not in spec.prompt


def test_strategy_tuner_prompt_path_cannot_escape_the_package(tmp_path):
    paths = WorkspacePaths(tmp_path)
    _seed_package(paths)
    manifest_path = paths.strategy("alpha") / "strategy.yml"
    manifest = yaml_io.load(manifest_path)
    manifest["tuning"] = {
        "enabled": True,
        "schedule": {"type": "cron", "cron": "0 */6 * * *"},
        "subagent": {
            "name": "strategy_tuner",
            "prompt_file": "../../subagents/strategy_tuner.agent.md",
        },
    }
    yaml_io.dump(manifest_path, manifest)
    paths.subagents.mkdir(parents=True, exist_ok=True)
    (paths.subagents / "strategy_tuner.agent.md").write_text(
        "ESCAPED GLOBAL TUNER PROMPT",
        encoding="utf-8",
    )

    spec = StrategySubAgentRegistry(
        paths=paths,
        strategy_id="alpha",
    ).get("strategy_tuner")

    assert "ESCAPED GLOBAL" not in spec.prompt
    assert "per-strategy self-evolution" in spec.prompt
