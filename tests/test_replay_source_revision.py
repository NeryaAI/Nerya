"""Authored replay code participates in source identity; run artifacts do not."""
import json
from types import SimpleNamespace

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import create_proposal
from nerya.skills.builtin.backtest.scripts.config import BacktestConfig
from nerya.strategies import workflow_service
from nerya.strategies.verification import replay_provenance, replay_status, source_revision
from nerya.strategies.workflow_graph import MAX_FILE_BYTES, package_revision
from nerya.strategies.workflow_service import source_files, view_workflow

pytestmark = pytest.mark.smoke


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def package(tmp_path):
    paths = WorkspacePaths(tmp_path)
    root = paths.strategy("replay_source")
    write(root, "strategy.yml", "strategy_id: replay_source\nentrypoint: main.py:run\n")
    write(root, "main.py", "def run(ctx): return None\n")
    return paths, root


@pytest.mark.parametrize("name", [
    "backtests/research_backtest.py",
    "backtests/freeform_backtest.py",
    "backtests/custom_backtest.py",
    "backtests/replay_helper.py",
    "main.py",
    "replay_inputs.json",
])
def test_authored_changes_stale_canonical_replay_revision(package, name):
    paths, root = package
    path = write(root, name, '{"version": 1}\n' if name.endswith(".json") else "VALUE = 1\n")
    before, source = source_files(paths, root.name)
    assert source["omitted_files"] == []
    revision = source_revision(before)
    report = {"id": "saved", "metrics": {"verdict": "FAIL", "provenance": {
        "version": 1, "strategy_id": root.name, "proposal_id": None,
        "source_revision": revision,
    }}}
    assert replay_status(report, revision, root.name, None)["status"] == "failed"

    path.write_text(path.read_text().replace("1", "2"))
    after, source = source_files(paths, root.name)
    assert source_revision(after) != revision
    assert after[name] != before[name]
    assert source["omitted_files"] == []
    assert replay_status(report, source_revision(after), root.name, None)["status"] == "stale"


def test_run_artifacts_and_layout_do_not_change_source_revision(package):
    paths, root = package
    write(root, "backtests/research_backtest.py", "VALUE = 1\n")
    before, _ = source_files(paths, root.name)
    # RunTracker creates this before the source snapshot/provenance is captured.
    run = write(root, "backtests/20261023_120000/run.json", '{"status": "running"}\n')
    provenance = replay_provenance(
        SimpleNamespace(root=root, strategy_id=root.name, content_hash="fixture"),
        BacktestConfig(), {}, proposal_id=None, allow_mock=False,
    )
    assert provenance["source_revision"] == source_revision(before)
    assert provenance["omitted_files"] == []
    for name in (
        "backtests/20261023_120000/metrics.json",
        "backtests/20261023_120000/source/backtests/research_backtest.py",
        "backtests/run-1_20261023_120000/source/main.py",
        "backtests/saved/report.md",
        "backtests/output/research_backtest.py",
        "backtests/history/research_backtest.py",
        "backtests/__pycache__/replay_helper.py",
        "backtests/metrics.json",
        "backtests/result.json",
        "backtests/run.json",
        "backtests/provenance.json",
        "backtests/.hidden.py",
        "history/dataset.json",
        "history/research_backtest.py",
        "runs/output.json",
        "validation_report.json",
    ):
        write(root, name, "generated output\n")
    run.write_text('{"status": "completed"}\n')
    after, source = source_files(paths, root.name)
    assert after == before
    assert source_revision(after) == provenance["source_revision"]
    assert source["omitted_files"] == []
    write(root, "workflow.json", '{"version": 1}\n')
    with_layout, _ = source_files(paths, root.name)
    assert "workflow.json" in with_layout
    assert source_revision(with_layout) == source_revision(before)


@pytest.mark.parametrize("name", ["backtests/research_backtest.py", "backtests/replay_helper.py", "backtests"])
def test_replay_symlinks_are_omitted_and_never_attested(package, tmp_path, name):
    paths, root = package
    external = tmp_path / "external"
    script = write(external, "research_backtest.py", "VALUE = 'outside'\n")
    link = root / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(external if name == "backtests" else script, target_is_directory=name == "backtests")
    files, source = source_files(paths, root.name)
    assert not any(key.startswith("backtests/") for key in files)
    assert name in source["omitted_files"]
    provenance = replay_provenance(
        SimpleNamespace(root=root, strategy_id=root.name, content_hash="fixture"),
        BacktestConfig(), {}, proposal_id=None, allow_mock=False,
    )
    assert provenance["source_revision"] is None
    assert name in provenance["omitted_files"]


@pytest.mark.parametrize("kind", ["oversized", "binary", "total_limit"])
def test_replay_source_uses_existing_read_limits(package, monkeypatch, kind):
    paths, root = package
    name = "backtests/research_backtest.py"
    path = write(root, name, "VALUE = 1\n")
    if kind == "total_limit":
        monkeypatch.setattr(workflow_service, "_MAX_TOTAL_BYTES", 1)
    else:
        path.write_bytes(b"#" * (MAX_FILE_BYTES + 1) if kind == "oversized" else b"\xff")
    files, source = source_files(paths, root.name)
    assert name not in files
    assert name in source["omitted_files"]


@pytest.mark.parametrize("kind", ["strategy_package_proposal", "strategy_tuning_proposal"])
def test_replay_overlay_preserves_seed_and_stales_validation_receipt(package, kind):
    paths, root = package
    name = "backtests/research_backtest.py"
    write(root, name, "VALUE = 1\n")
    write(root, "backtests/replay_helper.py", "HELPER = 1\n")
    proposal = create_proposal(
        paths, kind=kind, summary="Replay delta", target=f"strategies/{root.name}",
        extra_files={f"after/strategies/{root.name}/{name}": "VALUE = 2\n"},
    )
    files, source = source_files(paths, root.name, proposal.id)
    assert files[name] == "VALUE = 2\n"
    assert files["main.py"] == (root / "main.py").read_text()
    assert files["backtests/replay_helper.py"] == "HELPER = 1\n"
    assert source["omitted_files"] == []
    write(proposal.path, "validation_report.json", json.dumps({
        "strategy_id": root.name, "source_revision": package_revision(files),
    }))
    assert view_workflow(paths, root.name, proposal.id)["validation_receipt"]["status"] == "current"
    staged = proposal.path / "after" / "strategies" / root.name
    write(staged, "backtests/20261023_120000/source/main.py", "old run\n")
    assert view_workflow(paths, root.name, proposal.id)["validation_receipt"]["status"] == "current"
    write(staged, name, "VALUE = 3\n")
    assert view_workflow(paths, root.name, proposal.id)["validation_receipt"]["status"] == "stale"
    assert (root / name).read_text() == "VALUE = 1\n"
