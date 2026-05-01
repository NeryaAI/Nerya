"""AgentAPI — in-process façade exposing the workspace-native agent. of the refactor wires the Gateway / SDK against the new
:class:`nerya.agent.kernel.AgentKernel`. Callers can:

* run a single turn end-to-end (``run_turn``)
* enumerate the available native tools (``list_tools``)
* open / inspect the transcripts they produced

This module deliberately stays small: it does *not* re-implement the
agent loop, it just re-exposes the kernel through a stable contract
the SDK + Gateway can lean on. The HTTP/Local server already mirrors
the same shape over the wire (see :mod:`nerya.api.routes_agent`), so
``run_turn`` here returns the same dict you get from
``POST /agent/run_turn``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..agent.kernel import AgentKernel
from ..agent.file_state import FileStateCache
from ..core import jsonl
from ..core.config import Config
from ..skills.kernel import SkillKernel
from ..tools import ToolRegistry
from ..tools.native import build_native_tool_deps, register_native_tools


@dataclass
class AgentAPI:
    config: Config
    skills: SkillKernel

    # --------------------------------------------------------------- run_turn

    def run_turn(
        self,
        *,
        trigger: dict[str, Any] | None = None,
        text: str | None = None,
        strategy_id: Optional[str] = None,
        session_id: Optional[str] = None,
        attached_skills: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Run a single agent turn and return a dashboard-shaped dict.

        Either pass a fully-formed ``trigger`` dict or simply ``text`` —
        in the latter case we synthesise an ``agent.user_message``
        trigger so the SDK feels "chat-like" out of the box.

        The return value mirrors :func:`nerya.api.routes_agent.run_turn`
        so downstream consumers (TS SDK, dashboard, tests) only have to
        learn one schema.
        """

        if trigger is None and text is None:
            raise ValueError("AgentAPI.run_turn requires either trigger= or text=")
        if trigger is None:
            trigger = {
                "source": "sdk",
                "kind": "agent.user_message",
                "payload": {"text": text},
            }

        kernel = AgentKernel(config=self.config, skills=self.skills)
        try:
            result = kernel.run_turn(
                trigger=trigger,
                strategy_id=strategy_id,
                session_id=session_id,
                attached_skills=attached_skills,
            )
        except Exception as exc:
            jsonl.append(self.config.paths.journal("errors"), {
                "kind": "sdk.run_turn.error",
                "error": f"{type(exc).__name__}: {exc}",
            })
            raise

        return {
            "trigger_event_id": result.trigger_event_id,
            "decision": result.decision,
            "actions": result.actions,
            "tool_trace": result.tool_trace,
            "budget": result.budget,
            "turn_id": result.turn_id,
            "stopped_reason": result.stopped_reason,
            "final_text": getattr(result, "final_text", ""),
            "iterations": getattr(result, "iterations", 0),
            "steps": list(result.steps or []),
            "blocks": list(result.blocks or []),
            "harness": getattr(result, "harness", "native"),
        }

    # -------------------------------------------------------------- tools list

    def list_tools(self) -> dict[str, Any]:
        """Mirror ``GET /agent/tools`` for in-process callers.

        Returns the full native tool catalog with risk, scope, and
        provenance metadata so SDK consumers can render a permissions
        surface or pre-flight a request without touching the HTTP layer.
        """

        registry = ToolRegistry()
        try:
            workspace_root = Path(self.config.paths.root)
        except Exception:
            workspace_root = Path.cwd()

        skill_roots: list[Path] = []
        try:
            for entry in self.skills.registry.list():
                root = getattr(entry, "skill_dir", None) or getattr(entry, "path", None)
                if root:
                    skill_roots.append(Path(str(root)).parent)
        except Exception:
            pass

        deps = build_native_tool_deps(
            workspace_root=workspace_root,
            skill_roots=skill_roots,
            file_state=FileStateCache(),
            paths=self.config.paths,
            config=self.config,
            skills=self.skills,
        )
        register_native_tools(registry, deps)

        items = []
        for descriptor in registry.list_tools():
            items.append({
                "name": descriptor.name,
                "description": descriptor.description,
                "namespace": descriptor.namespace,
                "risk": getattr(descriptor.risk, "value", str(descriptor.risk)),
                "permission_scope": getattr(
                    descriptor.permission_scope, "value", str(descriptor.permission_scope)
                ),
                "read_only": bool(descriptor.read_only),
                "is_concurrency_safe": bool(descriptor.is_concurrency_safe),
                "requires_fresh_read": bool(descriptor.requires_fresh_read),
                "mutates_paths": bool(descriptor.mutates_paths),
                "result_kind": descriptor.result_kind,
                "auto_approve": bool(descriptor.auto_approve),
                "tags": list(descriptor.tags or []),
                "input_schema": dict(descriptor.input_schema or {}),
            })
        items.sort(key=lambda r: (r["namespace"], r["name"]))
        return {
            "ok": True,
            "count": len(items),
            "tools": items,
            "harness": "native",
        }


__all__ = ["AgentAPI"]
