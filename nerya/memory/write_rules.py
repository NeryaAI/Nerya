"""Declarative policy: what Nerya remembers, for how long, and when to dedupe.

Until now the only memory-write policy was implicit: anything written
to ``memory/*.md`` got picked up by the optional vector watcher; nothing
else was stored. That hides a lot of decisions (do we keep raw chat
turns? user preferences? trade outcomes? errors?) and makes it hard to
explain to an operator what their long-term memory contains.

This module lifts those decisions into a small config schema operators
can read and edit:

.. code-block:: yaml

    memory:
      write_rules:
        session_summary:
          enabled: true
          retention_days: 90
          max_entries: 500
          dedupe: by_hash       # by_hash | by_key | none
          target_files: ["memory/global.md"]
        learning:
          enabled: true
          retention_days: 365
          max_entries: 1000
          dedupe: by_key
          target_files: ["memory/global.md", "memory/skill_learnings.md"]
        signal:
          enabled: false        # opt-in — chatty; vector index recommended
          retention_days: 30
          max_entries: 5000
          dedupe: by_hash
          target_files: ["memory/signals.md"]
        error:
          enabled: true
          retention_days: 180
          max_entries: 1000
          dedupe: by_hash
          target_files: ["memory/mistakes.md"]
        decision:
          enabled: true
          retention_days: 365
          max_entries: 1000
          dedupe: by_key
          target_files: ["memory/decisions.md"]

Categories are intentionally a closed set; new ones require a code
change so the dashboard / writer / search can stay consistent. Custom
keys for one-off captures live as ``key`` arguments on the existing
fact-index API; this module only governs *typed* memory streams.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core import yaml_io
from ..core.config import Config


__all__ = [
    "MEMORY_CATEGORIES",
    "MemoryCategory",
    "MemoryWriteRule",
    "DEFAULT_WRITE_RULES",
    "DEDUPE_STRATEGIES",
    "NOTEBOOK_CATEGORIES",
    "NOTEBOOK_TARGET_BY_CATEGORY",
    "load_write_rules",
    "save_write_rules",
    "validate_write_rules",
]


DEDUPE_STRATEGIES: tuple[str, ...] = ("none", "by_hash", "by_key")


# Closed set of typed memory streams. ``description`` is shown in the
# dashboard; ``default_target_files`` are the markdown surfaces a
# write of this category appends to (so the existing memsearch watcher
# picks it up).
@dataclass(frozen=True)
class MemoryCategory:
    id: str
    name: str
    description: str
    default_target_files: tuple[str, ...]
    default_retention_days: int
    default_max_entries: int
    default_dedupe: str  # "none" | "by_hash" | "by_key"
    default_enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "default_target_files": list(self.default_target_files),
            "default_retention_days": self.default_retention_days,
            "default_max_entries": self.default_max_entries,
            "default_dedupe": self.default_dedupe,
            "default_enabled": self.default_enabled,
        }


MEMORY_CATEGORIES: tuple[MemoryCategory, ...] = (
    MemoryCategory(
        id="session_summary",
        name="Session summary",
        description="LLM-generated digest at the end of a chat session. One per session.",
        default_target_files=("memory/global.md",),
        default_retention_days=90,
        default_max_entries=500,
        default_dedupe="by_hash",
    ),
    MemoryCategory(
        id="learning",
        name="Learning",
        description="A specific lesson or rule extracted from operator behaviour or outcomes.",
        default_target_files=("memory/global.md", "memory/skill_learnings.md"),
        default_retention_days=365,
        default_max_entries=1000,
        default_dedupe="by_key",
    ),
    MemoryCategory(
        id="preference",
        name="Preference",
        description="Operator stated preference (risk appetite, language, time horizon, …).",
        default_target_files=("memory/global.md",),
        default_retention_days=365,
        default_max_entries=500,
        default_dedupe="by_key",
    ),
    MemoryCategory(
        id="signal",
        name="Signal",
        description="Routine inbound signals (news, market events). High volume — opt-in.",
        default_target_files=("memory/signals.md",),
        default_retention_days=30,
        default_max_entries=5000,
        default_dedupe="by_hash",
        default_enabled=False,
    ),
    MemoryCategory(
        id="decision",
        name="Decision",
        description="A trade decision, plan, or strategic call worth remembering.",
        default_target_files=("memory/decisions.md",),
        default_retention_days=365,
        default_max_entries=1000,
        default_dedupe="by_key",
    ),
    MemoryCategory(
        id="error",
        name="Error / mistake",
        description="A loss, miss, or runtime failure worth remembering for postmortems.",
        default_target_files=("memory/mistakes.md",),
        default_retention_days=180,
        default_max_entries=1000,
        default_dedupe="by_hash",
    ),
    # Notebook categories: bounded curated text injected into the system
    # prompt from the on-disk notebook files. The writer dispatches
    # these to ``MemoryNotebook`` instead of the markdown append path.
    # ``retention_days`` / ``max_entries`` are unused for notebook rows
    # (the char-budget is the real cap) but we keep the same schema so
    # operators don't see a different shape per category.
    MemoryCategory(
        id="notebook_agent",
        name="Agent notebook",
        description=(
            "Bounded curated notes the agent keeps about its own environment, "
            "tools, project conventions. Char-limited; injected verbatim into "
            "the system prompt at session start."
        ),
        default_target_files=("memory/notebook/AGENT.md",),
        default_retention_days=0,
        default_max_entries=0,
        default_dedupe="by_hash",
    ),
    MemoryCategory(
        id="notebook_operator",
        name="Operator profile",
        description=(
            "Bounded curated notes about the human operator (preferences, "
            "communication style, policy decisions). Char-limited; injected "
            "verbatim into the system prompt at session start."
        ),
        default_target_files=("memory/notebook/OPERATOR.md",),
        default_retention_days=0,
        default_max_entries=0,
        default_dedupe="by_key",
    ),
)


# Categories whose target store is the bounded :class:`MemoryNotebook`,
# not a free-form markdown append. The writer uses this set to route
# captures to ``MemoryNotebook.add()`` (which enforces char limits +
# scans for prompt injection) instead of the regular fact log path.
NOTEBOOK_CATEGORIES: frozenset[str] = frozenset({
    "notebook_agent",
    "notebook_operator",
})


# Map a notebook category to the :class:`MemoryNotebook` target id. The
# notebook only knows ``"agent"`` / ``"operator"`` — the writer
# translates from the categorised view into that two-bucket world.
NOTEBOOK_TARGET_BY_CATEGORY: dict[str, str] = {
    "notebook_agent": "agent",
    "notebook_operator": "operator",
}

_CATEGORY_BY_ID: dict[str, MemoryCategory] = {c.id: c for c in MEMORY_CATEGORIES}


@dataclass
class MemoryWriteRule:
    """Operator-overridden version of a :class:`MemoryCategory`."""

    category: str
    enabled: bool
    retention_days: int
    max_entries: int
    dedupe: str
    target_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "enabled": self.enabled,
            "retention_days": self.retention_days,
            "max_entries": self.max_entries,
            "dedupe": self.dedupe,
            "target_files": list(self.target_files),
        }


def _rule_from_category(category: MemoryCategory) -> MemoryWriteRule:
    return MemoryWriteRule(
        category=category.id,
        enabled=category.default_enabled,
        retention_days=category.default_retention_days,
        max_entries=category.default_max_entries,
        dedupe=category.default_dedupe,
        target_files=list(category.default_target_files),
    )


DEFAULT_WRITE_RULES: dict[str, MemoryWriteRule] = {
    c.id: _rule_from_category(c) for c in MEMORY_CATEGORIES
}


def _coerce_rule(category_id: str, raw: Any) -> MemoryWriteRule:
    """Build a :class:`MemoryWriteRule` from a partial dict, filling defaults.

    Missing keys fall back to the category default so a YAML file that
    only sets ``enabled: false`` still validates.
    """

    base = _CATEGORY_BY_ID.get(category_id)
    if base is None:
        # Unknown category — return a rule that's safe to read but
        # validate() will flag it.
        base_default = _rule_from_category(MEMORY_CATEGORIES[0])
        return MemoryWriteRule(
            category=category_id,
            enabled=False,
            retention_days=0,
            max_entries=0,
            dedupe="none",
            target_files=[],
        )
    rule = _rule_from_category(base)
    rule.category = category_id
    if isinstance(raw, dict):
        if "enabled" in raw and raw["enabled"] is not None:
            rule.enabled = bool(raw["enabled"])
        if "retention_days" in raw and raw["retention_days"] is not None:
            try:
                rule.retention_days = max(0, int(raw["retention_days"]))
            except (TypeError, ValueError):
                pass
        if "max_entries" in raw and raw["max_entries"] is not None:
            try:
                rule.max_entries = max(0, int(raw["max_entries"]))
            except (TypeError, ValueError):
                pass
        if "dedupe" in raw and raw["dedupe"] is not None:
            ded = str(raw["dedupe"]).strip().lower()
            if ded in DEDUPE_STRATEGIES:
                rule.dedupe = ded
        if "target_files" in raw and isinstance(raw["target_files"], list):
            rule.target_files = [str(p).strip() for p in raw["target_files"] if str(p or "").strip()]
    return rule


def load_write_rules(config: Config) -> dict[str, MemoryWriteRule]:
    """Read the active write rules from ``config.yaml``.

    Returns the merged map (defaults + operator overrides). Categories
    not present in the file fall back to their bundled defaults.
    """

    raw = config.get("memory.write_rules") or {}
    if not isinstance(raw, dict):
        raw = {}
    out: dict[str, MemoryWriteRule] = {}
    for cat in MEMORY_CATEGORIES:
        out[cat.id] = _coerce_rule(cat.id, raw.get(cat.id))
    return out


def save_write_rules(
    config: Config, rules: dict[str, dict[str, Any]] | dict[str, MemoryWriteRule]
) -> dict[str, MemoryWriteRule]:
    """Persist operator-edited write rules into ``config.yaml``.

    Unknown categories raise :class:`ValueError`. Missing fields keep
    the bundled defaults, so a partial patch is fine.
    """

    existing = yaml_io.load(config.paths.config, default={}) or {}
    if not isinstance(existing, dict):
        existing = {}
    memory = existing.setdefault("memory", {})
    if not isinstance(memory, dict):
        memory = {}
        existing["memory"] = memory
    persisted = memory.setdefault("write_rules", {})
    if not isinstance(persisted, dict):
        persisted = {}
        memory["write_rules"] = persisted

    merged: dict[str, MemoryWriteRule] = dict(load_write_rules(config))
    for cat_id, patch in (rules or {}).items():
        if cat_id not in _CATEGORY_BY_ID:
            raise ValueError(f"unknown memory category: {cat_id!r}")
        if isinstance(patch, MemoryWriteRule):
            patch_dict = patch.to_dict()
            patch_dict.pop("category", None)
        elif isinstance(patch, dict):
            patch_dict = dict(patch)
        else:
            raise ValueError(f"{cat_id}: rule must be an object")
        rule = _coerce_rule(cat_id, patch_dict)
        merged[cat_id] = rule
        persisted[cat_id] = rule.to_dict()

    yaml_io.dump(config.paths.config, existing)
    config.data.setdefault("memory", {})
    if not isinstance(config.data["memory"], dict):
        config.data["memory"] = {}
    config.data["memory"]["write_rules"] = persisted
    return merged


def validate_write_rules(rules: dict[str, MemoryWriteRule]) -> list[str]:
    """Return a list of human-readable problems with the rule set.

    The dashboard surfaces these as warnings; an empty list means the
    rule set is fully valid.
    """

    problems: list[str] = []
    for cat_id, rule in rules.items():
        if cat_id not in _CATEGORY_BY_ID:
            problems.append(f"{cat_id}: unknown category")
            continue
        if rule.dedupe not in DEDUPE_STRATEGIES:
            problems.append(f"{cat_id}: dedupe must be one of {DEDUPE_STRATEGIES}")
        if rule.retention_days < 0:
            problems.append(f"{cat_id}: retention_days must be >= 0")
        if rule.max_entries < 0:
            problems.append(f"{cat_id}: max_entries must be >= 0")
    return problems
