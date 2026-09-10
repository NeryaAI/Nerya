"""Failed validation and ambiguous identities are not compensable by scores."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import list_proposals
from nerya.evolution import optimizer_feedback
from nerya.strategies import evolution
from nerya.strategies.package import load_package

pytestmark = pytest.mark.smoke


def _package(paths):
    root = paths.strategy("alpha")
    yaml_io.dump(root / "strategy.yml", {
        "version": 1, "strategy_id": "alpha", "mode": "paper",
        "entrypoint": "main.py:run", "markets": ["mock:BTC/USDT"],
        "schedule": {"type": "cron", "cron": "*/5 * * * *"},
        "tuning": {"enabled": True, "guardrails": {"require_backtest": False},
                   "schedule": {"type": "cron", "cron": "0 */6 * * *"},
                   "proposal_policy": {"allowed_targets": ["main.py"]}},
    })
    (root / "main.py").write_text("def run(ctx):\n    return {'ok': True}\n", encoding="utf-8")
    return load_package(paths, "alpha")


def _candidate(name, *, invalid_code=False):
    return {
        "candidate_id": name, "summary": name,
        "proposed_changes": [{"file": "main.py", "kind": "full_file", "after_content": (
            "import requests\ndef run(ctx):\n    return requests.get('https://invalid.test')\n"
            if invalid_code else "def run(ctx):\n    return {'ok': True, 'reviewed': True}\n"
        )}],
        "validation_plan": ["manual_review"],
    }


def test_failed_static_candidate_cannot_win_even_with_extreme_score(tmp_path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    package = _package(paths)
    original = evolution._score_tuning_candidate

    def score(**kwargs):
        value, reasons, feedback = original(**kwargs)
        return (1_000_000 if kwargs["output"]["candidate_id"] == "invalid" else value), reasons, feedback

    monkeypatch.setattr(evolution, "_score_tuning_candidate", score)
    selection = evolution._select_tuning_candidate(
        {"candidates": [_candidate("invalid", invalid_code=True), _candidate("valid")]},
        package.manifest.tuning, package, paths, run_id="selection-test", create_asset_candidates=False,
    )
    report = selection["report"]
    assert report["candidates"][0]["validation_preview"]["status"] == "failed"
    assert selection["selected_output"]["candidate_id"] == "valid"


def test_all_failed_candidates_produce_hold_without_strategy_proposal(tmp_path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    package = _package(paths)
    original_bytes = (package.root / "main.py").read_bytes()
    monkeypatch.setattr(evolution.StrategyEvolutionRunner, "_dispatch_tuner", lambda self, **kwargs: {
        "ok": True, "output": {"candidates": [
            _candidate("invalid-a", invalid_code=True), _candidate("invalid-b", invalid_code=True),
        ]},
    })
    result = evolution.StrategyEvolutionRunner(
        config=Config(paths=paths, data={"runtime": {"mock_mode": True}}),
        skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
    ).run_once("alpha", dry_run=False, operator="test")
    assert result.status == "hold"
    assert result.proposal_id is None
    assert list_proposals(paths) == []
    assert result.optimizer_report["selected_candidate_id"] is None
    assert len(result.optimizer_report["candidates"]) == 2
    from nerya.evolution.timeline import _optimizer_report_digest
    digest = _optimizer_report_digest({"optimizer_report": result.optimizer_report})
    assert digest["selection_status"] == "no_eligible_candidate"
    assert digest["eligible_count"] == 0
    assert all(row["selection_eligible"] is False for row in digest["candidates"])
    assert evolution._optimizer_metadata(result.optimizer_report)["selection_status"] == "no_eligible_candidate"
    assert (package.root / "main.py").read_bytes() == original_bytes


@pytest.mark.parametrize("selection", [
    {}, {"selected_candidate_id": "missing"},
    {"selected_candidate_id": "second", "selected_index": 0},
    {"selected_candidate_id": "second", "selected_index": True},
    {"selected_candidate_id": "second", "selection_status": "no_eligible_candidate"},
])
def test_feedback_never_guesses_or_contradicts_selected_identity(selection):
    report = {"candidates": [{"candidate_id": "first"}, {"candidate_id": "second"}], **selection}
    assert optimizer_feedback.selected_optimizer_candidate(report) is None


def test_feedback_accepts_a_unique_consistent_selected_identity():
    report = {"selected_candidate_id": "second", "selected_index": 1,
              "candidates": [{"candidate_id": "first"}, {"candidate_id": "second"}]}
    assert optimizer_feedback.selected_optimizer_candidate(report) is report["candidates"][1]


def test_duplicate_candidate_ids_are_ineligible_and_cannot_receive_feedback(tmp_path):
    paths = WorkspacePaths(tmp_path)
    package = _package(paths)
    selection = evolution._select_tuning_candidate(
        {"candidates": [_candidate("duplicate"), _candidate("duplicate"), _candidate("unique")]},
        package.manifest.tuning, package,
    )
    report = selection["report"]
    assert report["selected_candidate_id"] == "unique"
    assert report["eligible_count"] == 1
    assert all("duplicate_candidate_id" in row["blocked_reasons"] for row in report["candidates"][:2])
    assert optimizer_feedback.selected_optimizer_candidate({**report, "selected_candidate_id": "duplicate"}) is None


def test_failed_backtest_candidate_cannot_win_even_with_extreme_score(tmp_path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    package = _package(paths)
    original = evolution._score_tuning_candidate

    def score(**kwargs):
        value, reasons, feedback = original(**kwargs)
        return (1_000_000 if kwargs["output"]["candidate_id"] == "invalid" else value), reasons, feedback

    monkeypatch.setattr(evolution, "_score_tuning_candidate", score)
    monkeypatch.setattr(evolution, "_candidate_backtest_preview", lambda **kwargs: {
        "status": "failed", "score_delta": -100, "blocked_reasons": ["backtest_verdict_failed"],
    })
    invalid = _candidate("invalid")
    invalid["validation_plan"] = ["manual_review", "backtest"]
    selection = evolution._select_tuning_candidate(
        {"candidates": [invalid, _candidate("valid")]}, package.manifest.tuning, package,
        paths, run_id="backtest-selection-test", create_asset_candidates=False,
    )
    assert selection["report"]["candidates"][0]["backtest_preview"]["status"] == "failed"
    assert selection["selected_output"]["candidate_id"] == "valid"


def test_feedback_uses_recorded_index_without_reindexing_filtered_rows():
    selected = {"candidate_id": "unique", "index": 7}
    report = {"selected_candidate_id": "unique", "selected_index": 7,
              "candidates": [None, selected]}
    assert optimizer_feedback.selected_optimizer_candidate(report) is selected
