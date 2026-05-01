"""SkillRuntime — dispatches skill actions, honoring risk/approval/journal."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Callable

import time

from ..core import devmode
from ..core.config import Config
from ..core.errors import LLMError, SkillActionError, SkillPermissionError, SkillError
from ..core.errors import IntentValidationError, RiskRejection, ApprovalPending
from ..core.errors import SecurityError, SkillNotFoundError
from ..core.ids import skill_call_id
from ..core.time import now_iso
from .flow import run_flow
from .registry import SkillRegistry, SkillEntry
from .permissions import manifest_permissions
from .schema import SkillSchemaError, validate_payload


@dataclass
class SkillCallContext:
    """What the skill action sees. Injected by the runtime, not constructed by the skill."""
    config: Config
    registry: SkillRegistry
    caller: str                      # "agent", "subagent:<name>", "script:<id>"
    strategy_id: str | None = None
    session_id: str | None = None
    trigger_event_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def get_skill(self, skill_id: str) -> SkillEntry:
        return self.registry.get(skill_id)


class SkillRuntime:
    def __init__(self, config: Config, registry: SkillRegistry):
        self.config = config
        self.registry = registry

    def call(
        self,
        skill_id: str,
        action_name: str,
        *,
        payload: dict[str, Any],
        caller: str,
        strategy_id: str | None = None,
        session_id: str | None = None,
        trigger_event_id: str | None = None,
        extras: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = self.registry.get(skill_id)
        spec = entry.spec(action_name)

        handler: Callable | None = entry.actions.get(action_name)
        if handler is None and not spec.flow:
            raise SkillNotFoundError(
                f"action {skill_id}.{action_name} has no handler and no flow"
            )

        # basic permission — caller cannot call a skill if the skill manifest
        # forbids the caller type.
        if spec.context_policy == "subagent_only" and not caller.startswith("subagent:"):
            raise SkillPermissionError(
                f"{skill_id}.{action_name} requires subagent caller"
            )

        # Apr-27 2026: alias common LLM-emitted keys before validation.
        # Subagent LLMs (and Claude-/Gemini-shaped main agents) frequently
        # emit ``command`` instead of ``cmd`` for shell-style actions and
        # ``timeout_sec`` instead of ``timeout_s``. Without this
        # normalisation every such call hits the schema validator,
        # bounces back to the LLM with an ``IntentValidationError``,
        # burns an iteration, and adds noise to the agent reflection
        # journal. Only rewrite when the canonical key is missing so
        # we never overwrite an explicit operator/agent value.
        if isinstance(payload, dict):
            ALIASES = {
                ("operator", "terminal"): {
                    "command": "cmd",
                    "timeout_sec": "timeout_s",
                },
                ("operator", "process_start"): {
                    "command": "cmd",
                },
                ("operator", "shell"): {
                    "command": "cmd",
                    "timeout_sec": "timeout_s",
                },
            }
            alias_map = ALIASES.get((skill_id, action_name)) or {}
            for src, dst in alias_map.items():
                if src in payload and dst not in payload:
                    payload[dst] = payload[src]

        # Phase 6 — validate the payload against the declared input_schema
        # at dispatch time so schema mismatches never reach the trading
        # kernel. Missing/empty schema is a no-op for backward compat.
        try:
            validate_payload(payload, spec.input_schema)
        except SkillSchemaError as exc:
            raise IntentValidationError(
                f"{skill_id}.{action_name} input invalid: {exc}"
            ) from exc

        # Auto-inject the active LLMSession (set by the script runner)
        # so llm_skill actions can gate on the caller's script policy.
        extras_merged = dict(extras or {})
        if "llm_session" not in extras_merged:
            try:
                from ..llm.session import get_active_session
                sess = get_active_session()
                if sess is not None:
                    extras_merged["llm_session"] = sess
            except Exception:
                pass

        ctx = SkillCallContext(
            config=self.config,
            registry=self.registry,
            caller=caller,
            strategy_id=strategy_id,
            session_id=session_id,
            trigger_event_id=trigger_event_id,
            extras=extras_merged,
        )

        call_id = skill_call_id()
        journal_path = self.config.paths.journal("skills")
        from ..core import jsonl
        # Phase 6 — attribute every skill call with the manifest version,
        # caller identity class, and declared tags so downstream audit /
        # observability can answer "who called what version of which
        # skill" without re-deriving it later.
        caller_kind = _caller_kind(caller)
        if spec.journal:
            # Plan 02 P0 §5 — explain *why* this skill was loaded so the
            # transcript / dashboard can answer "which skill ran and from
            # where". ``loaded_via`` is one of:
            #   - ``builtin``: shipped with the runtime
            #   - ``user_installed``: workspace/skills/installed/<id>
            #   - ``procedural``: SKILL.md fallback (no Python actions)
            #   - ``user_root``: workspace/skills/<id>/ or ~/.nerya/skills/
            loaded_via = (entry.manifest.source or "builtin").strip() or "builtin"
            manifest_path = str(entry.manifest.path) if entry.manifest.path else ""
            jsonl.append(journal_path, {
                "kind": "skill.call.start",
                "skill_id": skill_id,
                "skill_version": entry.manifest.version,
                "action": action_name,
                "skill_call_id": call_id,
                "caller": caller,
                "caller_kind": caller_kind,
                "strategy_id": strategy_id,
                "session_id": session_id,
                "trigger_event_id": trigger_event_id,
                "payload_keys": sorted(list(payload.keys())),
                "tags": sorted(list(spec.tags or []) + list(entry.manifest.tags or [])),
                "loaded_via": loaded_via,
                "manifest_path": manifest_path,
                "permissions": list(spec.permissions or entry.manifest.permissions or []),
            })

        started = time.time()
        try:
            if handler is not None:
                result = handler(ctx=ctx, **payload)
            else:
                result = run_flow(
                    spec.flow,
                    payload=payload,
                    runtime=self,
                    caller=caller,
                    strategy_id=strategy_id,
                    session_id=session_id,
                )
        except Exception as exc:
            if spec.journal:
                jsonl.append(journal_path, {
                    "kind": "skill.call.error",
                    "skill_id": skill_id,
                    "skill_version": entry.manifest.version,
                    "action": action_name,
                    "skill_call_id": call_id,
                    "caller": caller,
                    "caller_kind": caller_kind,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            devmode.record_tool_call(
                tool=f"skill.{skill_id}.{action_name}",
                args=payload,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=round((time.time() - started) * 1000, 2),
                caller=caller,
            )
            devmode.record_error(exc, where=f"skill.{skill_id}.{action_name}",
                                  context={"caller": caller, "strategy_id": strategy_id})
            # Pass-through typed Nerya errors so callers can still match
            # them. Only unknown/unexpected exceptions get wrapped.
            if isinstance(exc, (SkillError, LLMError, IntentValidationError,
                                RiskRejection, ApprovalPending, SecurityError)):
                raise
            raise SkillActionError(f"{skill_id}.{action_name} failed: {exc}") from exc
        devmode.record_tool_call(
            tool=f"skill.{skill_id}.{action_name}",
            args=payload,
            result=result,
            elapsed_ms=round((time.time() - started) * 1000, 2),
            caller=caller,
        )

        # Plan 16 P1 §1 — Hermes-style tool-result overflow.
        # When a skill returns a payload that would balloon the planner's
        # context, persist the original to ``state/tool_results/`` and
        # replace the in-memory result with a reference card. Skills can
        # opt out by declaring ``no_overflow_spool`` in their tags; the
        # threshold is governed by ``agent.harness.result_overflow_threshold_bytes``
        # (set to 0 to disable globally).
        result, overflow_ref = _maybe_spool_oversized(
            result,
            config=self.config,
            skill_id=skill_id,
            action_name=action_name,
            tags=set(spec.tags or []) | set(entry.manifest.tags or []),
        )
        if overflow_ref is not None and spec.journal:
            jsonl.append(journal_path, {
                "kind": "skill.call.overflow",
                "skill_id": skill_id,
                "skill_version": entry.manifest.version,
                "action": action_name,
                "skill_call_id": call_id,
                "caller": caller,
                "ref_id": overflow_ref.ref_id,
                "bytes": overflow_ref.bytes,
                "kind_payload": overflow_ref.kind,
            })

        if spec.journal:
            jsonl.append(journal_path, {
                "kind": "skill.call.done",
                "skill_id": skill_id,
                "skill_version": entry.manifest.version,
                "action": action_name,
                "skill_call_id": call_id,
                "caller": caller,
                "caller_kind": caller_kind,
                "result_keys": sorted(list((result or {}).keys())) if isinstance(result, dict) else None,
                "overflow_ref": overflow_ref.ref_id if overflow_ref else None,
            })
            # per-strategy history, if we have one
            if strategy_id:
                from ..strategy_history.store import record_skill_call
                record_skill_call(
                    self.config.paths,
                    strategy_id=strategy_id,
                    session_id=session_id,
                    skill_id=skill_id, action=action_name,
                    caller=caller, payload_keys=sorted(list(payload.keys())),
                    result_summary=_summarize(result),
                    ts=now_iso(),
                )
        return result if isinstance(result, dict) else {"result": result}


def _summarize(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return {"keys": sorted(list(result.keys()))}
    return {"type": type(result).__name__}


# --------------------------------------------------------- caller attribution
_KNOWN_CALLER_PREFIXES = (
    "agent", "subagent", "script", "sdk", "api", "cli",
    "dashboard", "cron", "replay",
)


def _caller_kind(caller: str) -> str:
    """Classify a caller string into a coarse kind for attribution.

    Callers are strings like ``"agent"``, ``"subagent:market_analyst"``,
    ``"script:my_script"``, ``"sdk:trader"``, ``"cli"``, etc. The kind
    is the bit before the first ``":"`` (or the whole string if there is
    none). Unknown prefixes get classified as ``"other"``.
    """
    if not caller:
        return "unknown"
    prefix = caller.split(":", 1)[0].strip().lower()
    if prefix in _KNOWN_CALLER_PREFIXES:
        return prefix
    return "other"


# --------------------------------------------------------- result overflow
def _result_overflow_threshold(config: Config) -> int:
    """Return the configured threshold in bytes (0 disables spooling)."""
    try:
        harness_cfg = (config.data or {}).get("agent", {}).get("harness", {}) or {}
        threshold = int(harness_cfg.get("result_overflow_threshold_bytes", 65_536))
    except Exception:
        threshold = 65_536
    return max(0, threshold)


def _maybe_spool_oversized(
    result: Any,
    *,
    config: Config,
    skill_id: str,
    action_name: str,
    tags: set[str],
):
    """If ``result`` is too large, persist it and return a reference card.

    Returns ``(result_or_envelope, ResultRef | None)``. Skills that mark
    themselves with the ``no_overflow_spool`` tag are exempt — useful for
    skills that legitimately return large payloads consumed verbatim by
    the next action (e.g. ``operator.read_file`` which already has its
    own truncation). The serialised JSON form is the size we measure, so
    this lines up with what the planner ultimately renders.
    """
    threshold = _result_overflow_threshold(config)
    if threshold <= 0:
        return result, None
    if "no_overflow_spool" in tags:
        return result, None
    if not isinstance(result, (dict, list, str, bytes, bytearray)):
        return result, None
    try:
        encoded = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
    except Exception:
        try:
            encoded = str(result).encode("utf-8")
        except Exception:
            return result, None
    if len(encoded) <= threshold:
        return result, None
    try:
        from ..harness.result_store import ResultStore
    except Exception:
        return result, None
    try:
        store = ResultStore(config.paths)
    except Exception:
        return result, None
    summary = f"oversized result from {skill_id}.{action_name} ({len(encoded)} bytes)"
    try:
        ref = store.store(result, kind="skill_result", summary=summary)
    except Exception:
        return result, None
    envelope: dict[str, Any] = {
        "_overflow": True,
        "ref_id": ref.ref_id,
        "bytes": ref.bytes,
        "summary": summary,
        "skill_id": skill_id,
        "action": action_name,
    }
    if isinstance(result, dict):
        for key in ("status", "ok", "error", "kind"):
            if key in result:
                envelope[key] = result[key]
    return envelope, ref
