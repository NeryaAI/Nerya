"""Script runner. Loads an approved script and runs it inside the sandbox."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable

from ..core import jsonl
from ..core.config import Config
from ..core.errors import ScriptNotApproved, ScriptSandboxViolation
from ..core.ids import script_run_id
from ..core.time import now_iso
from ..llm.session import (
    LLMSession, LLMPolicy,
    set_active_session, reset_active_session,
)
from .manifest import ScriptManifest, load_manifest
from .sandbox import sandbox
from .script_context import ScriptContext
from .static_analyzer import analyze, has_errors


# Type alias for the callable that a caller wires in to let a script
# reach back into the skill runtime. Scripts are not allowed to import
# the skill kernel themselves (runtime boundary rule), so this callable
# is the single entrypoint.
SkillInvoker = Callable[[str, str, dict[str, Any]], dict[str, Any]]


def _session_from_manifest(caller: str, manifest: ScriptManifest) -> LLMSession:
    mp = manifest.llm_policy
    policy = LLMPolicy(
        allowed_tiers=list(mp.allowed_tiers),
        allowed_tasks=list(mp.allowed_tasks),
        max_calls_per_run=int(mp.max_calls_per_run),
        max_tokens_per_run=int(mp.max_tokens_per_run),
        max_cost_usd_per_run=float(mp.max_cost_usd_per_day),
        high_tier_requires_approval=bool(mp.high_tier_requires_approval),
    )
    return LLMSession(caller=caller, policy=policy)


def _script_dir(config: Config, script_id: str, *, stage: str = "approved") -> Path:
    root = config.paths.scripts_approved if stage == "approved" else config.paths.scripts_pending
    return root / script_id


def _build_script_context(
    config: Config, skill_invoker: SkillInvoker | None,
) -> ScriptContext | None:
    """Construct a :class:`ScriptContext` for a script run.

    ``skill_invoker`` is a callable with signature
    ``(skill_id, action, payload) -> dict`` that the outer caller
    (usually the ``script`` skill action, which lives in the skills
    layer) provides. Scripts MUST NOT import the skill kernel directly
    — that would violate the ``scripts``→``skills`` boundary rule in

    Returns ``None`` if no invoker is wired; the script then runs
    without a ``ctx`` argument (legacy behaviour).
    """
    if skill_invoker is None:
        return None

    def _call(*, skill_id: str, action: str,
              payload: dict[str, Any]) -> Any:
        return skill_invoker(skill_id, action, payload)

    return ScriptContext(config=config, _call_skill=_call)


def run_script(
    config: Config,
    script_id: str,
    *,
    args: dict[str, Any] | None = None,
    skill_invoker: SkillInvoker | None = None,
) -> dict[str, Any]:
    run_id = script_run_id()
    script_dir = _script_dir(config, script_id, stage="approved")
    if not script_dir.exists():
        raise ScriptNotApproved(f"script {script_id} not approved")

    manifest_path = script_dir / "manifest.yml"
    manifest: ScriptManifest = load_manifest(manifest_path)
    if manifest.state != "approved":
        raise ScriptNotApproved(
            f"script {script_id} manifest state={manifest.state}"
        )

    script_path = script_dir / f"{manifest.id}.py"
    if not script_path.exists():
        # fallback: first .py file
        candidates = [p for p in script_dir.glob("*.py")]
        if not candidates:
            raise ScriptNotApproved(f"no script body for {script_id}")
        script_path = candidates[0]

    findings = analyze(script_path)
    if has_errors(findings):
        raise ScriptSandboxViolation(
            f"static analyzer errors: {[f.message for f in findings if f.severity=='error']}"
        )

    spec = importlib.util.spec_from_file_location(f"nerya_script_{script_id}", script_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)

    jsonl.append(config.paths.journal("scripts"), {
        "kind": "script.run.start",
        "script_id": script_id, "script_run_id": run_id,
        "ts": now_iso(),
    })

    session = _session_from_manifest(f"script:{script_id}", manifest)
    token = set_active_session(session)
    try:
        with sandbox():
            spec.loader.exec_module(mod)
            entry = getattr(mod, manifest.entry)
            call_args = dict(args or {})
            # If the script entry accepts ``ctx``, hand it a constrained
            # :class:`ScriptContext` that can only reach whitelisted
            # skill actions (no wallet / order / LLM paths).
            import inspect as _inspect
            sig = _inspect.signature(entry)
            if "ctx" in sig.parameters and "ctx" not in call_args:
                ctx = _build_script_context(config, skill_invoker)
                if ctx is not None:
                    call_args["ctx"] = ctx
            result = entry(**call_args)
    except Exception as exc:
        jsonl.append(config.paths.journal("scripts"), {
            "kind": "script.run.error",
            "script_id": script_id, "script_run_id": run_id,
            "error": f"{type(exc).__name__}: {exc}",
            "llm_session": session.snapshot(),
        })
        raise
    finally:
        reset_active_session(token)

    jsonl.append(config.paths.journal("scripts"), {
        "kind": "script.run.done",
        "script_id": script_id, "script_run_id": run_id,
        "result_summary": {"type": type(result).__name__},
        "llm_session": session.snapshot(),
    })
    return {"script_run_id": run_id, "script_id": script_id,
            "result": result if isinstance(result, (dict, list)) else None,
            "llm_session": session.snapshot()}
