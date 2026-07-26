"""recipe manifests for chat demo suggestions.

the runtime ships its onboarding suggestions as data (TOML/YAML "recipe"
manifests) so the operator UI can render different starter prompts
depending on which skills / capabilities are installed. Nerya now does
the same: built-in recipes live in :data:`_BUILTIN_RECIPES`, workspaces
can drop additional recipes under
``$workspace/recipes/<id>.yml``, and the runtime selects only those
whose ``capabilities`` (skills / categories / agent-actions) are
present in the current workspace.

A consumer (today the dashboard chat empty-state, tomorrow the gateway
``/help`` and CLI ``nerya skills suggest``) only needs to call
:func:`available_recipes(client)` to receive the live list.

Design goals:

* **No hardcoded skill ids in the UI**: skills become available, the
  recipe appears; skills get disabled, the recipe disappears.
* **Versioned**: every recipe declares an ``id`` + integer ``version``
  so workspaces can pin a snapshot.
* **External overrides win**: a workspace recipe with the same id as a
  bundled recipe replaces it (so operators can edit the BTCUSDT prompt
  copy without forking Nerya).
* **Capability-tagged**: recipes declare ``required_skills`` and
  ``required_actions`` (the action ids exposed in the capability matrix
  endpoint). A recipe is only available if every required skill /
  action is present.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..core import yaml_io
from ..core.paths import WorkspacePaths


@dataclass(frozen=True)
class Recipe:
    """A single starter recipe (chat suggestion, /help example, …)."""

    id: str
    title: str
    body: str
    prompt: str
    version: int = 1
    category: str = "general"
    required_skills: tuple[str, ...] = ()
    required_actions: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "prompt": self.prompt,
            "version": self.version,
            "category": self.category,
            "required_skills": list(self.required_skills),
            "required_actions": list(self.required_actions),
            "tags": list(self.tags),
        }


_BUILTIN_RECIPES: tuple[Recipe, ...] = (
    Recipe(
        id="monitoring_script_btc_macd",
        title="Write a monitoring script",
        body=(
            "Monitor BTCUSDT 5m bars for a MACD cross and propose an "
            "approval-gated script."
        ),
        prompt=(
            "Write me a Python script (paper mode only, using the Nerya "
            "script sandbox) that monitors BTCUSDT 5-minute bars on "
            "Binance. On a MACD golden cross (12,26,9) open a small long, "
            "on a death cross close it. Include a 1% trailing stop. Save "
            "it as btc_macd_5m.py and propose it as a script for approval."
        ),
        category="trading",
        required_skills=("script", "trading"),
        tags=("script", "macd", "btc"),
    ),
    Recipe(
        id="create_subagent_narrative_watcher",
        title="Create a subagent",
        body=(
            "Spawn a narrative_watcher subagent that tracks news + social "
            "sentiment."
        ),
        prompt=(
            "Create a new subagent called narrative_watcher that tracks "
            "crypto news and social sentiment, and writes a daily brief. "
            "Generate its .agent.md with a clear system prompt."
        ),
        category="ops",
        required_skills=("subagent",),
        tags=("subagent", "news"),
    ),
    Recipe(
        id="schedule_portfolio_heartbeat",
        title="Schedule a heartbeat",
        body=(
            "Every 60s, have the main agent review the portfolio and flag "
            "risk."
        ),
        prompt=(
            "Schedule a recurring portfolio heartbeat every 60 seconds. "
            "The main agent should review open positions, unrealized P&L, "
            "and flag any risk breaches via the message skill."
        ),
        category="trading",
        required_skills=("trigger", "portfolio", "risk", "message"),
        tags=("schedule", "portfolio"),
    ),
    Recipe(
        id="postmortem_strategy_reviewer",
        title="Run a postmortem",
        body="Orchestrate strategy_reviewer + risk_critic on a losing strategy.",
        prompt=(
            "Run a postmortem on a recent losing strategy. Orchestrate "
            "strategy_reviewer + risk_critic — root cause, execution "
            "quality, and one concrete proposal. Write the proposal into "
            "evolution/."
        ),
        category="trading",
        required_skills=("strategy_review", "subagent"),
        tags=("postmortem", "strategy"),
    ),
    Recipe(
        id="memory_recall_demo",
        title="Recall yesterday's notes",
        body="Search session memory for the last decision you made about XYZ.",
        prompt=(
            "Use the memory skill to recall the last decision I logged "
            "about position sizing, summarise it in 5 bullets, and link "
            "back to the originating session."
        ),
        category="ops",
        required_skills=("memory",),
        tags=("memory", "recall"),
    ),
    Recipe(
        id="exchange_setup",
        title="Add an exchange",
        body="Connect a new exchange via the operator skill.",
        prompt=(
            "Walk me through adding Bybit as a new exchange. Use the "
            "operator skill to validate creds, mark the connection as "
            "paper-only, and confirm the venue is now visible in the "
            "capability matrix."
        ),
        category="trading",
        required_skills=("operator", "exchange_author"),
        tags=("exchange", "setup"),
    ),
)


def _recipes_dir(paths: WorkspacePaths) -> Path:
    return paths.root / "recipes"


def _coerce_recipe(payload: dict[str, Any], *, recipe_id: str) -> Recipe:
    title = payload.get("title")
    prompt = payload.get("prompt")
    if not title or not prompt:
        raise ValueError(
            f"recipe {recipe_id!r} requires non-empty 'title' and 'prompt'"
        )
    return Recipe(
        id=str(payload.get("id") or recipe_id),
        title=str(title),
        body=str(payload.get("body") or ""),
        prompt=str(prompt),
        version=int(payload.get("version") or 1),
        category=str(payload.get("category") or "general"),
        required_skills=tuple(_strs(payload.get("required_skills"))),
        required_actions=tuple(_strs(payload.get("required_actions"))),
        tags=tuple(_strs(payload.get("tags"))),
    )


def _strs(values: Any) -> Iterable[str]:
    if not values:
        return ()
    if isinstance(values, str):
        return (values,) if values else ()
    out: list[str] = []
    for v in values:
        if isinstance(v, str) and v:
            out.append(v)
    return tuple(out)


def _walk_external(paths: WorkspacePaths) -> list[Recipe]:
    folder = _recipes_dir(paths)
    if not folder.is_dir():
        return []
    out: list[Recipe] = []
    for entry in sorted(folder.iterdir()):
        if entry.suffix.lower() not in {".yml", ".yaml"}:
            continue
        try:
            payload = yaml_io.load(entry, default={}) or {}
        except Exception:  # pragma: no cover - defensive
            continue
        if not isinstance(payload, dict):
            continue
        try:
            out.append(_coerce_recipe(payload, recipe_id=entry.stem))
        except ValueError:
            continue
    return out


def all_recipes(paths: WorkspacePaths | None = None) -> list[Recipe]:
    """Return bundled + workspace recipes (workspace wins on id)."""

    table: dict[str, Recipe] = {r.id: r for r in _BUILTIN_RECIPES}
    if paths is not None:
        for r in _walk_external(paths):
            table[r.id] = r
    return sorted(table.values(), key=lambda r: r.id)


def _capability_set(client) -> tuple[frozenset[str], frozenset[str]]:
    skill_ids: set[str] = set()
    action_ids: set[str] = set()
    skills = getattr(client, "skills", None)
    if skills is None:
        return frozenset(), frozenset()
    try:
        entries = list(skills.registry.list())
    except Exception:
        return frozenset(), frozenset()
    for entry in entries:
        manifest = getattr(entry, "manifest", None)
        if manifest is None:
            continue
        sid = getattr(manifest, "id", "")
        if sid:
            skill_ids.add(sid)
        actions = getattr(manifest, "actions", {}) or {}
        for name in actions.keys():
            action_ids.add(f"{sid}.{name}")
    return frozenset(skill_ids), frozenset(action_ids)


def is_available(
    recipe: Recipe,
    skill_ids: frozenset[str],
    action_ids: frozenset[str],
) -> bool:
    if recipe.required_skills and not set(recipe.required_skills).issubset(skill_ids):
        return False
    if recipe.required_actions and not set(recipe.required_actions).issubset(action_ids):
        return False
    return True


def available_recipes(client) -> list[dict[str, Any]]:
    """Return the recipes whose capability requirements are met by the
    *currently installed* skills/actions in ``client``.
    """

    paths = getattr(getattr(client, "config", None), "paths", None)
    skill_ids, action_ids = _capability_set(client)
    out: list[dict[str, Any]] = []
    for recipe in all_recipes(paths):
        if not is_available(recipe, skill_ids, action_ids):
            continue
        out.append(recipe.as_dict())
    return out


def recipe_summary(client) -> dict[str, Any]:
    """Return ``{available: [...], all: [...]}`` for the capability matrix."""

    paths = getattr(getattr(client, "config", None), "paths", None)
    skill_ids, action_ids = _capability_set(client)
    available: list[dict[str, Any]] = []
    everything: list[dict[str, Any]] = []
    for recipe in all_recipes(paths):
        row = recipe.as_dict()
        row["available"] = is_available(recipe, skill_ids, action_ids)
        everything.append(row)
        if row["available"]:
            available.append({k: v for k, v in row.items() if k != "available"})
    return {"available": available, "all": everything}
