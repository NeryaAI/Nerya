"""Structured hook lifecycle for agent turns.

Phase 3 — first-class, best-effort hooks that fire at each turn boundary:

* ``before_turn`` — trigger received, about to plan.
* ``after_plan`` — planner chose tier / subagents / skills.
* ``after_subagents`` — subagent outputs aggregated (may be empty).
* ``before_think`` — main LLM is about to be called.
* ``after_think`` — LLM decision parsed.
* ``after_act`` — one skill action dispatched (fires per action).
* ``after_observe`` — post-action observation recorded for re-plan.
* ``before_close`` — reflection / self-improvement about to run.
* ``after_turn`` — turn finished (success or error).

Hooks are loaded from two sources, both safe and sandboxed:

1. **Workspace python hooks** — ``workspace/hooks/<phase>.py`` with a
   ``def run(ctx): ...`` entrypoint. Loaded with a direct ``runpy``
   style import so operators can drop a file and reload the kernel
   without touching source.
2. **Config hooks** — ``agent.hooks.<phase>`` in ``nerya.yml`` can list
   named built-in hooks (e.g. ``log_turn``, ``emit_trace_event``).

All hook invocations are wrapped in a try/except; hook failures are
journaled under ``journals/errors.jsonl`` and *never* abort the turn.

The kernel does **not** depend on any particular hook being registered.
Absent hooks are simply skipped.
"""

from __future__ import annotations

import importlib.util
import logging
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core import jsonl
from ..core.config import Config

log = logging.getLogger("nerya.agent.hooks")


HOOK_PHASES: tuple[str, ...] = (
    "before_turn",
    "after_plan",
    "after_subagents",
    "before_think",
    "after_think",
    "after_act",
    "after_observe",
    "before_close",
    "after_turn",
    "after_session",
)


@dataclass
class HookContext:
    """Data passed to every hook invocation.

    The ``data`` dict is phase-specific; hooks should treat fields as
    optional.
    """

    phase: str
    turn_id: str
    trigger_event_id: str | None = None
    strategy_id: str | None = None
    session_id: str | None = None
    iteration: int = 0
    data: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


HookFn = Callable[[HookContext], None]


class HookRegistry:
    """Collects hooks by phase from workspace + config.

    The registry is recomputed on demand (``fire`` is the only public
    method). Loading is cheap — no network, no heavy imports — so we
    can re-discover in long-running sessions if the operator drops a
    new file into ``workspace/hooks``.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._hooks: dict[str, list[HookFn]] = {p: [] for p in HOOK_PHASES}
        self._loaded = False

    def _load_python_hooks(self) -> None:
        root = Path(self.config.paths.root) / "hooks"
        if not root.is_dir():
            return
        for phase in HOOK_PHASES:
            py = root / f"{phase}.py"
            if not py.is_file():
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    f"nerya_hook_{phase}", py,
                )
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                fn = getattr(mod, "run", None)
                if callable(fn):
                    self._hooks[phase].append(fn)
            except Exception as exc:
                log.warning("failed to load hook %s: %s", py, exc)

    def _load_builtin_hooks(self) -> None:
        """Resolve named hooks from ``agent.hooks.<phase>``.

        Each entry can be a string naming a built-in hook (``log_turn``,
        ``emit_trace_event``). Unknown names are skipped, not errored —
        absent hooks are a no-op by design.
        """
        declared = {}
        try:
            declared = self.config.get("agent.hooks") or {}
        except Exception:
            declared = {}
        if not isinstance(declared, dict):
            return
        for phase, names in declared.items():
            if phase not in HOOK_PHASES:
                continue
            if isinstance(names, str):
                names = [names]
            for name in names or []:
                fn = _BUILTIN_HOOKS.get(str(name))
                if fn is not None:
                    self._hooks[phase].append(fn)

    def ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._load_python_hooks()
        self._load_builtin_hooks()
        self._loaded = True

    def clear(self) -> None:
        for p in HOOK_PHASES:
            self._hooks[p] = []
        self._loaded = False

    def register(self, phase: str, fn: HookFn) -> None:
        """Programmatic hook registration (tests + internal callers)."""
        if phase not in HOOK_PHASES:
            raise ValueError(f"unknown hook phase: {phase}")
        self._hooks[phase].append(fn)
        self._loaded = True

    def hooks_for(self, phase: str) -> Iterable[HookFn]:
        self.ensure_loaded()
        return tuple(self._hooks.get(phase, ()))

    def fire(self, phase: str, ctx: HookContext) -> None:
        """Fire every hook for ``phase``.

        Individual hook failures are journaled under
        ``journals/errors.jsonl`` and swallowed. The turn itself never
        aborts because of a hook.
        """
        self.ensure_loaded()
        hooks = self._hooks.get(phase, ())
        if not hooks:
            return
        for fn in hooks:
            t0 = time.monotonic()
            try:
                fn(ctx)
            except Exception as exc:
                elapsed = int((time.monotonic() - t0) * 1000)
                try:
                    jsonl.append(self.config.paths.journal("errors"), {
                        "kind": "agent.hook.error",
                        "phase": phase,
                        "turn_id": ctx.turn_id,
                        "strategy_id": ctx.strategy_id,
                        "error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(limit=2),
                        "wall_ms": elapsed,
                    })
                except Exception:
                    pass


# ---------------------------------------------------------------- built-ins

def _builtin_log_turn(ctx: HookContext) -> None:
    log.info(
        "hook %s turn=%s strategy=%s iter=%s keys=%s",
        ctx.phase, ctx.turn_id, ctx.strategy_id, ctx.iteration,
        sorted(ctx.data.keys())[:6],
    )


def _builtin_emit_trace_event(ctx: HookContext) -> None:
    """Emit a ``trace.hook`` event on the turn-steps journal.

    Uses the same on-disk journaling path the kernel uses so every
    hook invocation is audit-visible without a new journal.
    """
    cfg: Config | None = _CURRENT_CONFIG.get(ctx.turn_id)
    if cfg is None:
        return
    try:
        jsonl.append(cfg.paths.journal("turn_steps"), {
            "kind": "agent.hook.trace",
            "phase": ctx.phase,
            "turn_id": ctx.turn_id,
            "strategy_id": ctx.strategy_id,
            "iteration": ctx.iteration,
            "detail": ctx.data,
        })
    except Exception:
        pass


_BUILTIN_HOOKS: dict[str, HookFn] = {
    "log_turn": _builtin_log_turn,
    "emit_trace_event": _builtin_emit_trace_event,
}


# Lightweight bridge so ``emit_trace_event`` can journal without every
# hook having to carry a Config reference. Kernel sets/clears per turn.
_CURRENT_CONFIG: dict[str, Config] = {}


def _bind_config(turn_id: str, cfg: Config) -> None:
    _CURRENT_CONFIG[turn_id] = cfg


def _unbind_config(turn_id: str) -> None:
    _CURRENT_CONFIG.pop(turn_id, None)


__all__ = [
    "HOOK_PHASES",
    "HookContext",
    "HookRegistry",
    "HookFn",
]
