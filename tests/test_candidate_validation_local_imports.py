"""Regression for sibling imports during isolated candidate test collection."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import sysconfig

import pytest


pytestmark = pytest.mark.smoke


def test_candidate_tests_can_import_main_and_its_sibling(tmp_path: Path) -> None:
    base = tmp_path / "worker-root"
    candidate = base / "candidate"
    scratch = base / "scratch"
    candidate_tests = candidate / "tests"
    candidate_tests.mkdir(parents=True)
    scratch.mkdir()
    (base / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (candidate / "strategy.yml").write_text(
        "id: local_imports\nmode: paper\nschedule: {type: cron, cron: '0 * * * *'}\n",
        encoding="utf-8",
    )
    (candidate / "strategy_execution.py").write_text(
        "def result():\n    return 'sibling-import-ok'\n",
        encoding="utf-8",
    )
    (candidate / "main.py").write_text(
        "from strategy_execution import result\n\n"
        "def run(ctx):\n    return result()\n",
        encoding="utf-8",
    )
    (candidate_tests / "test_contract.py").write_text(
        "from main import run\n\n"
        "def test_contract():\n    assert run(None) == 'sibling-import-ok'\n",
        encoding="utf-8",
    )

    request = {
        "base": str(base),
        "source": str(Path(__file__).parents[1]),
        "dependencies": sorted({
            str(Path(sysconfig.get_path(name)).resolve())
            for name in ("purelib", "platlib")
            if sysconfig.get_path(name)
        }),
        "smoke_test": True,
        "run_tests": True,
        "timezone_roots": [],
        "cpu_seconds": 10,
        "file_bytes": 8 * 1024 * 1024,
        "max_items": 20,
        "text_limit": 4000,
        "report_bytes": 64 * 1024,
    }
    request_path = base / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    worker = Path(__file__).parents[1] / "nerya" / "strategies" / "_candidate_validation_worker.py"

    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-S", str(worker), str(request_path)],
        cwd=candidate,
        capture_output=True,
        text=True,
        check=False,
    )

    report = json.loads((scratch / "result.json").read_text(encoding="utf-8"))
    assert completed.returncode == 0, completed.stderr
    assert report["status"] == "passed", report
    assert report["summary"]["collected"] == 1, report
    assert report["summary"]["passed"] == 1, report
    assert report["smoke_issues"] == [], report
