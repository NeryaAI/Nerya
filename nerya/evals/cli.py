"""CLI driver for the eval harness.

``python -m nerya.evals --module <dotted.module>`` loads scenarios and
runs them against the real agent loop (with the scripted transcript
backend standing in for the LLM). This is the executable surface the
evolution validation plans call for ``eval_scenario`` steps, so a
prompt / skill / config proposal can prove the agent still behaves
before it is applied.

Scenario modules export either:

* ``SCENARIOS`` — an iterable of :class:`~nerya.evals.scenario.EvalScenario`; or
* ``build_scenarios()`` — a zero-arg callable returning that iterable.

Exit code is ``0`` when every scenario passes, ``1`` otherwise. A JSON
summary is printed to stdout so validation runs can archive it as
evidence.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from typing import Any, Iterable


# Validation commands execute in a subprocess and therefore must not import
# arbitrary agent-authored Python.  Keep the executable baseline tied to the
# package-owned catalog; adding another suite requires an explicit code change
# (and a corresponding regression review), rather than a prompt-supplied path.
REGISTERED_SCENARIO_MODULES = frozenset({"nerya.evals.scenarios"})


def _load_scenarios(module_ref: str) -> Iterable[Any]:
    """Load scenarios from a package-owned, registered catalog.

    Importing a caller-supplied dotted module or file executes its top-level
    code.  The eval CLI is also used by evolution validation, so the module
    boundary is deliberately fail-closed: only registered catalogs may be
    imported.  The built-in catalog exports ``build_scenarios`` and is rebuilt
    for every invocation.
    """

    normalized = str(module_ref or "").strip()
    if normalized not in REGISTERED_SCENARIO_MODULES:
        raise PermissionError(
            f"scenario_module_not_registered:{normalized or '<empty>'}"
        )
    module = importlib.import_module(normalized)

    if hasattr(module, "SCENARIOS"):
        return list(module.SCENARIOS)
    if hasattr(module, "build_scenarios"):
        return list(module.build_scenarios())
    if hasattr(module, "SCENARIO_TEMPLATES"):
        templates = module.SCENARIO_TEMPLATES
        if not isinstance(templates, dict):
            raise TypeError("SCENARIO_TEMPLATES must be a mapping")
        return [builder() for builder in templates.values()]
    raise AttributeError(
        f"scenario module {module_ref!r} must export SCENARIOS or build_scenarios()"
    )


def _build_runner(workspace: str | None):
    """Assemble the gateway / registry / orchestrator harness."""

    from ..core.config import load_config
    from ..llm.gateway import LLMGateway
    from ..tools import (
        NativeToolExecutor,
        PermissionContext,
        PermissionEngine,
        PermissionMode,
    )
    from ..tools.native.bootstrap import (
        build_native_tool_deps,
        register_native_tools,
    )
    from ..tools.orchestrator import ToolOrchestrator
    from ..tools.registry import ToolRegistry
    from ..skills.kernel import SkillKernel
    from .runner import EvalRunner

    config = load_config(workspace)
    skills = SkillKernel.boot(config)
    deps = build_native_tool_deps(
        workspace_root=config.paths.root,
        skill_roots=[config.paths.skills],
        paths=config.paths,
        config=config,
        skills=skills,
    )
    registry = ToolRegistry()
    register_native_tools(registry, deps)
    executor = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=PermissionMode.AUTO),
    )
    # Child/subagent native calls must share this parent-owned executor so
    # schema repair, permission decisions, approvals, and per-call risk checks
    # remain identical to the root loop during evals.
    deps.executor = executor
    orchestrator = ToolOrchestrator(registry=registry, executor=executor)
    gateway = LLMGateway(config)
    return EvalRunner(
        gateway=gateway,
        registry=registry,
        orchestrator=orchestrator,
        context={"config": config, "skills": skills, "deps": deps},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m nerya.evals")
    parser.add_argument(
        "--module",
        required=True,
        help=(
            "registered scenario catalog module; currently "
            "nerya.evals.scenarios"
        ),
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="workspace root (defaults to the active profile resolution)",
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="stop at the first failing scenario",
    )
    args = parser.parse_args(argv)

    try:
        scenarios = _load_scenarios(args.module)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"scenario_load_failed: {exc}"}))
        return 1
    if not scenarios:
        print(json.dumps({"ok": False, "error": "no_scenarios"}))
        return 1

    try:
        runner = _build_runner(args.workspace)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"harness_build_failed: {exc}"}))
        return 1

    results = runner.run_many(scenarios, stop_on_failure=args.stop_on_failure)
    passed = sum(1 for r in results if r.verdict.passed)
    summary = {
        "ok": passed == len(results),
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": [r.asdict() for r in results],
    }
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
