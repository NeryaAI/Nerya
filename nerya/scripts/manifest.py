"""Script manifest — describes capabilities, LLM policy, limits."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import yaml_io
from ..core.errors import ScriptError


@dataclass
class LLMPolicy:
    allowed_tiers: list[str] = field(default_factory=lambda: ["light"])
    allowed_tasks: list[str] = field(default_factory=list)
    max_calls_per_run: int = 5
    max_tokens_per_run: int = 4000
    max_cost_usd_per_day: float = 1.0
    high_tier_requires_approval: bool = True


@dataclass
class ScriptManifest:
    id: str
    version: str
    title: str
    description: str
    entry: str = "run"
    llm_policy: LLMPolicy = field(default_factory=LLMPolicy)
    trigger_kinds: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    state: str = "pending"   # pending | approved | rejected
    path: Path | None = None


def load_manifest(path: Path) -> ScriptManifest:
    doc = yaml_io.load(path)
    if not doc:
        raise ScriptError(f"empty manifest: {path}")
    p = doc.get("llm_policy") or {}
    policy = LLMPolicy(
        allowed_tiers=list(p.get("allowed_tiers") or ["light"]),
        allowed_tasks=list(p.get("allowed_tasks") or []),
        max_calls_per_run=int(p.get("max_calls_per_run", 5)),
        max_tokens_per_run=int(p.get("max_tokens_per_run", 4000)),
        max_cost_usd_per_day=float(p.get("max_cost_usd_per_day", 1)),
        high_tier_requires_approval=bool(p.get("high_tier_requires_approval", True)),
    )
    return ScriptManifest(
        id=doc["id"],
        version=str(doc.get("version", "0.1.0")),
        title=doc.get("title", doc["id"]),
        description=doc.get("description", ""),
        entry=doc.get("entry", "run"),
        llm_policy=policy,
        trigger_kinds=list(doc.get("trigger_kinds") or []),
        permissions=list(doc.get("permissions") or []),
        state=doc.get("state", "pending"),
        path=path.parent,
    )
