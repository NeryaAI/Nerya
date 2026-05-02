from __future__ import annotations

from types import SimpleNamespace

from nerya.api import routes_evolution
from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.validation_plan import build_validation_plan
from nerya.llm.gateway import LLMCall
from nerya.strategies.evolution import StrategyEvolutionRunner, _filter_changes
from nerya.subagents.runtime import SubAgentRuntime
import pytest

pytestmark = pytest.mark.smoke


class _Guardrails:
    max_patch_files = 2


class _Cfg:
    forbidden_targets = ["limits.yml", "accounts/*"]
    allowed_targets = ["main.py", "config.yml"]
    guardrails = _Guardrails()


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

    class FakeRegistry:
        def list(self):  # noqa: ANN201
            return []

    class FakeSkills:
        registry = FakeRegistry()

    class FakeLLM:
        def call(self, **kwargs):  # noqa: ANN201
            assert "Tune alpha with small patches only." in kwargs["prompt"]
            assert "prefer fewer false positives" in kwargs["prompt"]
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
    assert result.audit_path
    audit_path = root / "reviews" / f"tuning_{result.run_id}_audit.json"
    assert audit_path.exists()
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "Tune alpha with small patches only." in audit_text
    assert "prefer fewer false positives" in audit_text
    assert "tighten signal filter" in audit_text

    route_map = {(method, path): handler for method, path, handler in routes_evolution.routes()}
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
    titles = [
        artifact["title"]
        for section in process["sections"]
        for artifact in section["artifacts"]
    ]
    assert "Role prompt" in titles
    assert "Subagent payload" in titles
    assert "Subagent output" in titles
