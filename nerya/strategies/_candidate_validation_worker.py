"""Trusted bootstrap, launched by candidate_validation inside Seatbelt only."""
from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import resource
import sys
import traceback
import zoneinfo


def main() -> int:
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    base = Path(request["base"])
    candidate = base / "candidate"
    scratch = base / "scratch"
    # Hard limits are set before any dependency, candidate, or conftest import.
    for name, value in ((resource.RLIMIT_CPU, request["cpu_seconds"]),
                        (resource.RLIMIT_FSIZE, request["file_bytes"]),
                        (resource.RLIMIT_NOFILE, 128), (resource.RLIMIT_CORE, 0)):
        resource.setrlimit(name, (value, value))
    with open(os.devnull, "rb") as stdin:
        os.dup2(stdin.fileno(), 0)
    # -S suppresses .pth/sitecustomize execution; dependencies are explicit.
    sys.path[:0] = [request["source"], *request["dependencies"]]
    report = {
        "version": 1, "status": "not_run", "exit_code": 0, "smoke_issues": [],
        "summary": dict.fromkeys(("collected", "passed", "failed", "skipped", "errors",
                                  "xfailed", "xpassed", "deselected"), 0),
        "failures": [],
    }
    max_items = request["max_items"]
    text_limit = request["text_limit"]

    def failure(nodeid, phase, message):
        if len(report["failures"]) < max_items:
            report["failures"].append({
                "nodeid": str(nodeid)[:text_limit], "phase": str(phase)[:text_limit],
                "message": str(message)[:text_limit],
            })

    try:
        zoneinfo.reset_tzpath(request["timezone_roots"])
        # Import the real SDK before adding candidate paths; never stub types.
        # Explicit package loading avoids listing the repository root.
        source = Path(request["source"]) / "nerya"
        spec = importlib.util.spec_from_file_location(
            "nerya", source / "__init__.py", submodule_search_locations=[str(source)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["nerya"] = module
        spec.loader.exec_module(module)
        from nerya.strategies.package import load_package_from_dir
        from nerya.strategies.validator import _smoke_test_import

        package = load_package_from_dir(candidate)
        entrypoint = Path(package.manifest.entrypoint_module)
        if entrypoint.is_absolute() or ".." in entrypoint.parts:
            raise ValueError("candidate entrypoint must stay inside the package")
        candidate_path = str(candidate)
        sys.path.insert(0, candidate_path)
        try:
            if request["smoke_test"]:
                report["smoke_issues"] = [issue.asdict() for issue in _smoke_test_import(package)][:max_items]
            if request["run_tests"]:
                import pytest

                class Reporter:
                    def pytest_collection_finish(self, session):
                        report["summary"]["collected"] = len(session.items)

                    def pytest_deselected(self, items):
                        report["summary"]["deselected"] += len(items)

                    def pytest_collectreport(self, report):
                        if report.failed:
                            summary["errors"] += 1
                            failure(report.nodeid, "collect", report.longreprtext)

                    def pytest_runtest_logreport(self, report):
                        if report.failed:
                            summary["failed" if report.when == "call" else "errors"] += 1
                            failure(report.nodeid, report.when, report.longreprtext)
                        elif report.skipped:
                            summary["xfailed" if hasattr(report, "wasxfail") else "skipped"] += 1
                        elif report.when == "call" and report.passed:
                            summary["xpassed" if hasattr(report, "wasxfail") else "passed"] += 1

                summary = report["summary"]
                # Empty trusted config prevents candidate/ancestor pytest.ini,
                # pyproject, addopts, pythonpath and required_plugins injection.
                # Conftest remains supported, exclusively in this sandbox.
                args = ["-c", str(base / "pytest.ini"), "-o", "addopts=",
                        "-o", "pythonpath=", "-o", "testpaths=",
                        "--rootdir", str(candidate), "--confcutdir", str(candidate),
                        "--basetemp", str(scratch / "pytest"), "-p", "no:cacheprovider",
                        "--color=no", "--tb=short", "-rA", "-q", str(candidate)]
                code = int(pytest.main(args, plugins=[Reporter()]))
                report["exit_code"] = code
                report["status"] = {0: "passed", 1: "failed", 5: "not_present"}.get(code, "error")
                if code == 2 and summary["errors"]:
                    report["status"] = "failed"
                elif code == 0 and not summary["passed"]:
                    report["status"] = "not_run" if summary["skipped"] or summary["xfailed"] else "error"
        finally:
            sys.path.remove(candidate_path)
    except BaseException as exc:
        # SystemExit, KeyboardInterrupt, missing dependencies, startup errors
        # must never become successful validation.
        report["status"] = "error"
        report["exit_code"] = 3
        message = f"{type(exc).__name__}: {exc}"
        failure("", "worker", message)
        if request["smoke_test"]:
            report["smoke_issues"].append({
                "severity": "blocker", "code": "candidate_worker_error",
                "message": message[:text_limit], "where": "sandbox",
            })
        traceback.print_exc()
    for issue in report["smoke_issues"]:
        for key in ("severity", "code", "message", "where"):
            issue[key] = str(issue[key])[:text_limit]
    encoded = json.dumps(report, ensure_ascii=True).encode()
    while len(encoded) > request["report_bytes"] and report["failures"]:
        report["failures"].pop()
        encoded = json.dumps(report, ensure_ascii=True).encode()
    if len(encoded) > request["report_bytes"]:
        raise ValueError("worker report exceeds byte limit")
    (scratch / "result.json").write_bytes(encoded)
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
