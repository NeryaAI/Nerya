"""Exercise the shipped review template and real persistence/dispatch seams.

Authoring choices and model output are fixtures, not proof of LLM compliance or
strategy performance. No real model, exchange, schedule activation or orders.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

import pytest
from skill_fixtures import EmptySkillKernel

from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.core.time import now_iso
from nerya.evolution.patch_proposal import list_proposals
from nerya.evolution.strategy_code_generator import StrategyCodeGenerator
from nerya.strategies.evolution import StrategyEvolutionRunner
from nerya.strategies.package import load_package
from nerya.strategies.scheduler_bridge import compile_trading_schedule, compile_tuning_schedule
from nerya.strategies.state import StrategyRunRecord, StrategyRunStore
from nerya.strategies.workflow_service import view_workflow
from nerya.tools.native.strategy_runtime import _request_from_args

pytestmark = pytest.mark.smoke
SKILL = Path(__file__).parents[1] / "nerya/skills/builtin/strategy_author"
TEMPLATE = (SKILL / "templates/review-plan.md").read_text(encoding="utf-8")
WORKFLOWS = (SKILL / "references/workflows.md").read_text(encoding="utf-8")


def _candidate(paths: WorkspacePaths, kind: str):
    """Fill the actual template for two different executable Skill examples."""
    manifests = [yaml_io.loads(text) for text in re.findall(r"```yaml\n(.*?)\n```", WORKFLOWS, re.S)]
    manifest = deepcopy(next(row for row in manifests if row.get("version") == 1))
    strategy_id = f"review_template_{kind}"
    manifest.update(strategy_id=strategy_id, markets=["mock:BTC/USDT"],
                    execution_mode="script" if kind == "script" else "agent",
                    agent_task={"enabled": kind != "script"})
    if kind != "script":
        manifest.update(next(row for row in manifests if "agent_profile" in row and "version" not in row))
    tuning = yaml_io.loads(re.search(r"<!-- template:review_config -->\s*```yaml\n(.*?)\n```", TEMPLATE, re.S).group(1))["tuning"]
    tuning["schedule"].update(cron="15 0 * * *" if kind == "script" else "0 */6 * * *", timezone="Asia/Shanghai")
    tuning["lookback"].update(runs=192 if kind == "script" else 96, max_age_hours=48 if kind == "script" else 24)
    tuning["objectives"] = ["execution_quality"]
    tuning["guardrails"]["require_backtest"] = False  # Behavior checks for these observers.
    manifest["tuning"] = tuning
    values = {
        "strategy_id": strategy_id,
        "strategy_summary": "15m closed-candle SMA20 observer, no orders." if kind == "script" else "15m closed-candle MACD12/26/9 crossover dispatch, no orders.",
        "review_schedule": f"Cron {tuning['schedule']['cron']} Asia/Shanghai, disabled; {tuning['lookback']['runs']} runs in {tuning['lookback']['max_age_hours']} hours.",
        "review_focus": "SMA parameter use, closed-bar freshness and deduplication." if kind == "script" else "MACD crossover correctness, skipped-branch zero calls and dispatched task evidence.",
        "evidence_rules": "No fills expected. With no runs, explain missing evidence; inspect errors before changing signal rules.",
        "allowed_changes": "main.py and the review prompt only; preserve markets, timeframe, no-order policy and disabled schedules.",
        "validation_rules": "Use saved branch tests for closed/stale/duplicate candles and no orders; retain prior source for rollback. No return claims.",
    }
    prompt = re.search(r"<!-- template:review_prompt -->\s*```markdown\n(.*?)\n```", TEMPLATE, re.S).group(1)
    for key, value in values.items():
        prompt = prompt.replace("{{" + key + "}}", value)
    assert "{{" not in prompt
    snippets = dict(re.findall(r"<!-- example:(\w+) -->\s*```python\n(.*?)\n```", WORKFLOWS, re.S))
    files = {
        "strategy.yml": yaml_io.dumps(manifest),
        "main.py": snippets["common"] + "\n" + snippets[kind] + "\n",
        "strategy.md": "Fixture observer. Independent inactive review: subagents/strategy_tuner.agent.md",
        tuning["subagent"]["prompt_file"]: prompt,
    }
    request = _request_from_args({"strategy_id": strategy_id, "strategy_class": "agent" if kind != "script" else "scalping",
        "execution_mode": manifest["execution_mode"], "markets": manifest["markets"], "accounts": ["paper_main"],
        "create_tuning": True, "files": files})
    result = StrategyCodeGenerator(paths).generate(request, require_valid=True, initial_state="draft")
    assert result.validation.ok, result.validation.asdict()
    assert result.proposal is not None
    assert result.files[tuning["subagent"]["prompt_file"]] == prompt
    return strategy_id, result, prompt


def _install_fixture(paths: WorkspacePaths, strategy_id: str, files: dict[str, str]):
    # Test-only package installation, never a runtime apply/promotion shortcut.
    for name, content in files.items():
        target = paths.strategy(strategy_id) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return load_package(paths, strategy_id)


@pytest.mark.parametrize("kind", ["script", "macd_agent"])
def test_tailored_plan_saves_with_strategy_and_compiles_an_independent_inactive_schedule(tmp_path, kind):
    paths = WorkspacePaths(tmp_path)
    strategy_id, result, prompt = _candidate(paths, kind)
    assert not paths.strategy(strategy_id).exists()
    assert not paths.triggers_schedules_file.exists()
    view = view_workflow(paths, strategy_id, result.proposal.id)
    reviewer = next(row for row in view["evolution"]["nodes"] if row["id"] == "agent:tuner")
    assert reviewer["content"] == prompt
    assert view["manifest"]["agent_task"]["enabled"] is (kind != "script")
    assert "strategy_tuner" not in view["manifest"]["subagents"]
    package = _install_fixture(paths, strategy_id, result.files)
    trading, review = compile_trading_schedule(package), compile_tuning_schedule(package)
    assert trading.enabled is review.enabled is False
    assert trading.id != review.id and trading.target != review.target
    assert review.target == "skill:strategy.run_tuning"
    assert review.timezone == "Asia/Shanghai"
    assert review.cron == package.manifest.tuning.schedule.cron
    assert review.payload["trading"] is False
    assert trading.target == ("skill:strategy.run_tick" if kind == "script" else "skill:strategy.agent_task")


def test_each_review_gets_its_saved_plan_and_fresh_evidence_and_records_the_public_run_plan(tmp_path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    strategy_id, result, prompt = _candidate(paths, "script")
    package = _install_fixture(paths, strategy_id, result.files)
    captured = []

    def dispatch(self, target, *, payload, **kwargs):
        captured.append((payload, kwargs["inline_spec"].prompt))
        # Explicit fake model output: test transport and audit, not LLM reasoning.
        return {"ok": True, "output": {"summary": "Fixture review, no change.", "proposed_changes": [],
            "review_plan": {"strategy_id": strategy_id, "package_hash": payload["performance"]["package_hash"],
                "run_id": payload["run_id"], "checks": ["inspect new run"] if payload["performance"]["runs_considered"] else ["defer: no evidence"]}}}

    monkeypatch.setattr("nerya.subagents.dispatcher.SubAgentDispatcher.dispatch", dispatch)
    runner = StrategyEvolutionRunner(config=Config(paths=paths, data={"runtime": {"mock_mode": True}}), skills=EmptySkillKernel())
    first = runner.run_once(strategy_id, dry_run=True)
    stamp = now_iso()
    StrategyRunStore(paths, strategy_id).write(StrategyRunRecord(run_id="new_evidence", strategy_id=strategy_id,
        package_hash=package.content_hash, mode="paper", started_at=stamp, finished_at=stamp, duration_ms=1, status="error"))
    second = runner.run_once(strategy_id, dry_run=True)
    assert len(captured) == 2
    assert all(saved_prompt == prompt for _, saved_prompt in captured)
    assert captured[0][0]["performance"]["runs_considered"] == 0
    assert captured[1][0]["performance"]["evidence_scope"]["selected_run_ids"] == ["new_evidence"]
    assert first.subagent_output["review_plan"]["checks"] != second.subagent_output["review_plan"]["checks"]
    for review in (first, second):
        assert review.error is None, review.asdict()
        assert review.proposal_id is None
        audit = json.loads(Path(review.audit_path).read_text())
        assert audit["subagent_output"]["review_plan"]["run_id"] == review.run_id
        # Free-text model output is redacted; the audit's authoritative identity
        # lives outside it. Do not weaken redaction to retain a duplicated hash.
        assert audit["package_hash"] == package.content_hash
        assert review.subagent_output["review_plan"]["package_hash"] == package.content_hash
    assert len(list_proposals(paths)) == 1  # Only the authored draft, no forced patch.
    assert load_package(paths, strategy_id).content_hash == package.content_hash
    assert not paths.triggers_schedules_file.exists()


@pytest.mark.parametrize("schedule, expected", [(None, False), ({"cron": "0 9 * * *"}, False),
    ({"cron": "0 9 * * *", "enabled": False}, False), ({"cron": "0 9 * * *", "enabled": True}, True)])
def test_new_review_scaffolds_default_off_but_preserve_explicit_schedule_settings(tmp_path, schedule, expected):
    tuning = {"enabled": True}
    if schedule is not None:
        tuning["schedule"] = schedule
    request = _request_from_args({"strategy_id": "review_defaults", "markets": ["mock:BTC/USDT"], "accounts": ["paper_main"],
        "create_tuning": True, "files": {"strategy.yml": yaml_io.dumps({"tuning": tuning})}})
    result = StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(request, create_proposal_record=False)
    assert yaml_io.loads(result.files["strategy.yml"])["tuning"]["schedule"]["enabled"] is expected


def test_explicit_no_review_remains_disabled(tmp_path):
    paths = WorkspacePaths(tmp_path)
    request = _request_from_args({"strategy_id": "no_review", "markets": ["mock:BTC/USDT"], "accounts": ["paper_main"],
        "execution_mode": "script", "create_tuning": False, "files": {"strategy.yml": "tuning: {enabled: false}\n"}})
    result = StrategyCodeGenerator(paths).generate(request, create_proposal_record=False)
    package = _install_fixture(paths, "no_review", result.files)
    assert compile_tuning_schedule(package) is None
    assert "subagents/strategy_tuner.agent.md" not in result.files


def test_authoring_skill_requires_separate_plan_before_completion():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert text.index("**Implement the strategy.**") < text.index("**Build the strategy-specific review plan.**") < text.index("**Verify the saved version.**")
    assert 'file="templates/review-plan.md"' in text
    assert "tuning.schedule.enabled:false" in text
    assert "Completion includes the saved review plan" in text
    assert "every review invocation" in (SKILL / "references/review.md").read_text(encoding="utf-8")
