"""Regression at the producer seam: accepted structure is not passing tests."""
from __future__ import annotations

import sys
import json

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import create_proposal, list_proposals
from nerya.strategies.validator import validate_proposal_files
from nerya.tools.native.strategy_runtime import (
    strategy_submit_proposal_handler, strategy_validate_handler,
)
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


def _files():
    return {
        "strategy.yml": (
            "id: candidate_behavior\ntitle: EMA contract\nclass: trend\nmode: paper\n"
            "markets: ['mock:BTC/USDT']\naccounts: [paper_main]\n"
            "schedule: {type: cron, cron: '0 * * * *', enabled: false}\n"
        ),
        "main.py": '''from nerya.strategies import StrategyResult
def _ema(values, period):
    k = 2.0 / (period + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema

def _detect_cross(short, long):
    if len(short) < 2 or len(long) < 2:
        return None
    if short[-1] > long[-1] and short[-2] <= long[-2]:
        return 'golden'
    if short[-1] < long[-1] and short[-2] >= long[-2]:
        return 'death'
    return None

def run(ctx):
    return StrategyResult.hold(reason='no_signal')
''',
        "tests/test_contract.py": '''from main import _ema, _detect_cross
def test_ema_known_values():
    assert abs(_ema([1, 2, 3], 2)[2] - 11/9) < 1e-9
def test_detect_golden_cross():
    assert _detect_cross([1, 2, 4], [1, 3, 3]) == 'golden'
def test_detect_death_cross():
    assert _detect_cross([4, 3, 1], [3, 4, 4]) == 'death'
def test_no_cross():
    assert _detect_cross([1, 1, 1], [2, 2, 2]) is None
def test_insufficient_data():
    assert _detect_cross([1], [1]) is None
''',
    }


def _proposal(tmp_path, files):
    cfg = Config(paths=WorkspacePaths(tmp_path), data={})
    proposal = create_proposal(
        cfg.paths, kind="strategy_package_proposal", summary="EMA contract",
        extra_files={f"after/strategies/candidate_behavior/{name}": content
                     for name, content in files.items()},
    )
    return cfg, proposal


@pytest.mark.skipif(sys.platform != "darwin", reason="real Seatbelt acceptance")
def test_producer_reports_exact_contract_failures_and_blocks_submission(tmp_path):
    cfg, proposal = _proposal(tmp_path, _files())
    result = strategy_validate_handler(
        ToolCall(name="strategy_validate", arguments={"proposal_id": proposal.id}),
        config=cfg,
    )
    data = result.content[0].data
    assert data["ok"] is False, "structurally valid package with two failed tests was accepted"
    assert data["structural"]["status"] == "passed"
    behavior = data["behavioral"]
    assert behavior["status"] == "failed"
    assert behavior["exit_code"] == 1
    assert behavior["summary"]["failed"] == 2
    assert behavior["summary"]["passed"] == 3
    nodeids = {failure["nodeid"] for failure in behavior["failures"]}
    assert any(node.endswith("test_ema_known_values") for node in nodeids)
    assert any(node.endswith("test_detect_death_cross") for node in nodeids)
    assert "11/9" in behavior["stdout"]
    assert "None" in behavior["stdout"]
    assert data["package_hash"]
    assert behavior["source_revision"] == data["package_hash"]
    assert result.semantic_success is False
    checks = {check["check_id"]: check["status"] for check in data["checks"]}
    assert checks["strategy_structure"] == "passed"
    assert checks["strategy_behavior"] == "failed"
    receipt_checks = {check["check_id"]: check["status"]
                      for check in result.metadata["verification"]["checks"]}
    assert receipt_checks["strategy_behavior"] == "failed"
    submitted = strategy_submit_proposal_handler(
        ToolCall(name="strategy_submit_proposal", arguments={"proposal_id": proposal.id}),
        config=cfg,
    )
    assert submitted.is_error
    hint = submitted.error.recovery_hint
    assert hint["validation"]["behavioral"]["summary"]["failed"] == 2
    assert any("test_ema_known_values" in issue["where"] for issue in hint["blockers"])
    assert {p.id: p.state for p in list_proposals(cfg.paths)}[proposal.id] == "draft"


@pytest.mark.skipif(sys.platform != "darwin", reason="real Seatbelt acceptance")
def test_repaired_fixture_passes_real_runner_and_is_bound_to_new_source():
    files = _files()
    before = validate_proposal_files(strategy_id="candidate_behavior", files=files)
    files["tests/test_contract.py"] = files["tests/test_contract.py"].replace("11/9", "23/9").replace(
        "[4, 3, 1], [3, 4, 4]", "[4, 5, 1], [3, 4, 4]",
    )
    after = validate_proposal_files(strategy_id="candidate_behavior", files=files).asdict()
    assert after["ok"] is True, after
    assert after["behavioral"]["status"] == "passed"
    assert after["behavioral"]["summary"]["passed"] == 5
    assert after["package_hash"] != before.package_hash


def test_structural_only_request_does_not_claim_behavior_passed():
    result = validate_proposal_files(strategy_id="candidate_behavior", files=_files(), smoke_test=False)
    data = result.asdict()
    assert data["structural"]["status"] == "passed"
    assert data["behavioral"]["status"] == "not_run"
    assert data["behavioral"]["exit_code"] is None


def test_unsafe_proposal_paths_are_rejected_before_materializing(tmp_path):
    files = _files()
    files[str(tmp_path / "escaped.py")] = "raise RuntimeError('escape')"
    result = validate_proposal_files(strategy_id="candidate_behavior", files=files)
    assert not result.ok
    assert any(issue.code == "invalid_package_path" for issue in result.blockers)
    assert not (tmp_path / "escaped.py").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="real Seatbelt acceptance")
def test_generator_persists_failed_behavior_in_validation_report(tmp_path):
    from nerya.evolution.strategy_code_generator import StrategyCodeGenerator, StrategyGenerationRequest

    generated = StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(
        StrategyGenerationRequest(strategy_id="candidate_behavior", markets=("mock:BTC/USDT",),
                                  accounts=("paper_main",), files=_files()),
        validate=True, require_valid=False, create_proposal_record=True,
    )
    assert generated.proposal is not None
    report = json.loads((generated.proposal.path / "validation_report.json").read_text())
    assert report["ok"] is False
    assert report["structural"]["status"] == "passed"
    assert report["behavioral"]["summary"]["failed"] == 2
    assert report["behavioral"]["summary"]["passed"] == 3


@pytest.mark.parametrize("status", ["unavailable", "timeout", "error", "not_run"])
def test_incomplete_execution_is_a_submission_blocker(monkeypatch, status):
    from nerya.strategies import candidate_validation

    monkeypatch.setattr(candidate_validation, "run_candidate_validation", lambda *a, **kw: {
        "smoke_issues": [], "behavioral": {
            "status": status, "exit_code": None, "summary": {}, "failures": [],
            "reason": "Isolated runner did not complete",
        },
    })
    result = validate_proposal_files(strategy_id="candidate_behavior", files=_files())
    assert not result.ok
    assert result.structural["status"] == "passed"
    assert result.behavioral["status"] == status
    assert any(issue.code == f"candidate_tests_{status}" for issue in result.blockers)


def test_structural_failure_never_executes_candidate(monkeypatch):
    from nerya.strategies import candidate_validation

    def unexpected_run(*a, **kw):
        pytest.fail("candidate executed despite structural blockers")

    monkeypatch.setattr(candidate_validation, "run_candidate_validation", unexpected_run)
    files = _files()
    files["main.py"] += "\nimport socket\n"
    result = validate_proposal_files(strategy_id="candidate_behavior", files=files).asdict()
    assert result["ok"] is False
    assert result["structural"]["status"] == "failed"
    assert result["behavioral"]["status"] == "not_run"


@pytest.mark.parametrize("entry", ["/tmp/outside.py:run", "../outside.py:run"])
def test_entrypoint_cannot_escape_candidate(entry):
    files = _files()
    files["strategy.yml"] += f"entrypoint: {entry}\n"
    result = validate_proposal_files(strategy_id="candidate_behavior", files=files)
    assert not result.ok
    assert any(issue.code == "invalid_entrypoint_path" for issue in result.blockers)
