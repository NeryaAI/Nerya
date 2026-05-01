"""Strategy-scoped subagent registry.

Plan ref: ``2026-04-28-agent-generated-strategy-runtime-refactor.md`` §6.

The global :func:`nerya.subagents.registry.load_registry` returns
operator-defined ``*.agent.md`` prompts under ``workspace/subagents/``.
That is the wrong scope for agent-generated strategies: each
strategy package ships its own subagent prompts under
``workspace/strategies/<strategy_id>/subagents/<name>.agent.md`` and
the runner must resolve those *first* so two strategies can ship a
``market_analyst`` subagent with different prompts without colliding.

This module owns the resolution policy:

1. If ``strategy_id`` is provided and the package declares the
   subagent in its manifest, the strategy-local prompt wins.
2. Otherwise we fall back to the global ``workspace/subagents``
   registry.
3. If neither has the prompt, a stub :class:`SubAgentSpec` with an
   empty body is returned so the dispatcher's existing journal /
   error path keeps working.

``allowed_skills`` and ``tier`` are still resolved by name through
the same defaults the global registry uses; per-strategy overrides
will be added in Phase 7 alongside the tuning config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..core.errors import TradingError
from ..core.paths import WorkspacePaths
from ..strategies.package import StrategyPackage, load_package
from .registry import (
    DEFAULT_SUBAGENT_PROMPTS,
    DEFAULT_SUBAGENT_SKILLS,
    DEFAULT_TIERS,
    SubAgentSpec,
    load_registry,
)


@dataclass
class StrategySubAgentRegistry:
    """Per-strategy subagent resolver.

    Construct one per ``strategy_id`` per run. The dispatcher caches a
    single instance per :class:`SubAgentDispatcher` and re-creates it
    when ``strategy_id`` changes, so we don't keep stale package
    references after a hot promotion.
    """

    paths: WorkspacePaths
    strategy_id: Optional[str] = None
    _package: Optional[StrategyPackage] = field(default=None, init=False, repr=False)
    _global: Optional[dict[str, SubAgentSpec]] = field(
        default=None, init=False, repr=False
    )

    def _load_global(self) -> dict[str, SubAgentSpec]:
        if self._global is None:
            self._global = load_registry(self.paths)
        return self._global

    def _load_package(self) -> Optional[StrategyPackage]:
        if not self.strategy_id:
            return None
        if self._package is None:
            try:
                self._package = load_package(self.paths, self.strategy_id)
            except TradingError:
                self._package = None
        return self._package

    def _strategy_prompt_path(self, name: str) -> Optional[Path]:
        pkg = self._load_package()
        if pkg is None:
            return None
        if name not in pkg.manifest.subagents:
            return None
        path = pkg.subagents_dir / f"{name}.agent.md"
        return path if path.exists() else None

    def get(self, name: str) -> SubAgentSpec:
        path = self._strategy_prompt_path(name)
        if path is not None:
            return SubAgentSpec.load(
                path,
                name=name,
                allowed_skills=list(DEFAULT_SUBAGENT_SKILLS.get(name, [])),
                tier=DEFAULT_TIERS.get(name, "medium"),
            )
        spec = self._load_global().get(name)
        if spec is not None:
            # Apr-30 2026 — Cunzi fix: even when the workspace ships a
            # ``<name>.agent.md`` we may have read an empty file (the
            # operator created it but didn't fill it in). Fall back to
            # the default body in that case so the model never runs
            # with a blank role prompt.
            if not (spec.prompt or "").strip():
                spec.prompt = DEFAULT_SUBAGENT_PROMPTS.get(name, spec.prompt)
            return spec
        return SubAgentSpec(
            name=name,
            prompt_path=self.paths.subagents / f"{name}.agent.md",
            # Apr-30 2026 — ship the default prompt body so the role
            # has personality / scope / output contract even when no
            # file exists on disk. The operator can override by
            # writing ``workspace/subagents/<name>.agent.md``.
            prompt=DEFAULT_SUBAGENT_PROMPTS.get(name, ""),
            allowed_skills=list(DEFAULT_SUBAGENT_SKILLS.get(name, [])),
            tier=DEFAULT_TIERS.get(name, "medium"),
        )

    def list_names(self) -> list[str]:
        names = set(self._load_global().keys())
        pkg = self._load_package()
        if pkg is not None:
            names.update(pkg.manifest.subagents)
        return sorted(names)


def resolve_spec(
    paths: WorkspacePaths,
    name: str,
    *,
    strategy_id: Optional[str] = None,
) -> SubAgentSpec:
    """Convenience wrapper for callers that don't want to keep the registry."""

    return StrategySubAgentRegistry(paths=paths, strategy_id=strategy_id).get(name)


__all__ = ["StrategySubAgentRegistry", "resolve_spec"]
