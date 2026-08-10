from __future__ import annotations

import json

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import create_proposal
from nerya.evolution.validation_plan import (
    _run_candidate_eval_step,
    build_validation_plan,
    run_validation_plan,
    write_validation_plan,
)


pytestmark = pytest.mark.smoke


def _candidate_plan(paths: WorkspacePaths, command: str):
    plan = build_validation_plan(
        [{"type": "unit_test", "command": command}],
        source="candidate-test",
    )
    write_validation_plan(paths, plan)
    return plan


def test_candidate_unit_test_runs_merged_challenger(tmp_path, monkeypatch):
    paths = WorkspacePaths(root=tmp_path)
    (tmp_path / "test_candidate.py").write_text(
        "def test_candidate():\n    assert True\n",
        encoding="utf-8",
    )
    plan = _candidate_plan(paths, "python -m pytest test_candidate.py -q")
    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="candidate changes a passing test",
        initial_state="approved",
        evidence_refs=["test:candidate"],
        validation_plan_id=plan.id,
        extra_files={
            "after/test_candidate.py": "def test_candidate():\n    assert False\n",
        },
    )
    monkeypatch.setenv("NERYA_PROFILE", "operator-profile-must-not-win")

    result = run_validation_plan(paths, proposal_id=proposal.id, dry_run=False)

    assert result["ok"] is False
    step = result["run"]["steps"][0]
    assert step["status"] == "failed"
    assert step["workspace"] == step["cwd"]
    assert "challenger" in step["cwd"]
    assert (tmp_path / "test_candidate.py").read_text(encoding="utf-8").endswith(
        "assert True\n"
    )


def test_candidate_unit_test_can_repair_a_failing_base(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    (tmp_path / "test_candidate.py").write_text(
        "def test_candidate():\n    assert False\n",
        encoding="utf-8",
    )
    plan = _candidate_plan(paths, "python -m pytest test_candidate.py -q")
    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="candidate repairs a failing test",
        initial_state="approved",
        evidence_refs=["test:candidate-repair"],
        validation_plan_id=plan.id,
        extra_files={
            "after/test_candidate.py": "def test_candidate():\n    assert True\n",
        },
    )

    result = run_validation_plan(paths, proposal_id=proposal.id, dry_run=False)

    assert result["ok"] is True
    assert result["run"]["steps"][0]["status"] == "passed"


@pytest.mark.parametrize(
    ("baseline_passed", "challenger_passed", "expected_status", "field"),
    [
        (True, False, "failed", "regressions"),
        (False, True, "passed", "improvements"),
    ],
)
def test_eval_candidate_comparison_records_transitions(
    tmp_path,
    monkeypatch,
    baseline_passed,
    challenger_passed,
    expected_status,
    field,
):
    baseline = WorkspacePaths(root=tmp_path / "baseline")
    challenger = WorkspacePaths(root=tmp_path / "challenger")

    def fake_run(paths, **kwargs):
        passed = baseline_passed if paths.root.name == "baseline" else challenger_passed
        summary = {
            "ok": passed,
            "results": [{"scenario_id": "scenario-x", "passed": passed}],
        }
        return {
            "index": kwargs["index"],
            "type": kwargs["step_type"],
            "status": "passed" if passed else "failed",
            "required": kwargs["required"],
            "stdout": json.dumps(summary),
            "stderr": "",
            "evidence_ref": kwargs["evidence_ref"],
        }

    monkeypatch.setattr(
        "nerya.evolution.validation_plan._run_validation_command",
        fake_run,
    )
    result = _run_candidate_eval_step(
        challenger,
        baseline_paths=baseline,
        run_id="vrn_test",
        index=0,
        command="python -m nerya.evals --module nerya.evals.scenarios",
        required=True,
        evidence_ref="validation:vrn_test:step:0",
    )

    assert result["status"] == expected_status
    assert len(result[field]) == 1
    assert result["same_command"] is True
