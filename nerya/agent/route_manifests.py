"""versioned planner route manifests.

the runtime ships a set of named, versioned planner profiles (``coding-v1``,
``operator-v1`` …) so operators can pin a known-good preset rather than
hand-edit `nerya.yml`. Nerya now exposes the same shape:

* a small bundled registry of manifests keyed by ``id`` (``trading-v1``,
  ``general-operator-v1``, ``minimal-v1``);
* an optional external manifest under
  ``$workspace/route_manifests/<id>.yml`` so operators can pin custom
  presets without forking Nerya;
* a ``planner.manifest`` config selector. When set, the manifest's
  ``routes`` and ``fallback`` win over the freeform
  ``planner.routes`` / ``planner.fallback`` keys. When unset the planner
  falls through to the legacy free-form config (``DEFAULT_CONFIG``).

The bundled manifests are intentionally *capability-tagged* (each route
declares the broad capability families it needs) so a future skill
selector can match by tag rather than hard-coded skill id; today the
``skills`` list still drives the `SkillRuntime`, but the tags travel
through the capability matrix endpoint for downstream UIs.

This file does *not* import anything from `agent` itself so importing it
from `planner.py` is safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..core import yaml_io
from ..core.paths import WorkspacePaths


@dataclass(frozen=True)
class RouteManifest:
    """A named, versioned planner preset.

    Attributes
    ----------
    id:
        Stable identifier (``"trading-v1"``). The ``-vN`` suffix is the
        contract version; bumping it is how operators opt-in to schema
        changes.
    name:
        Human-readable name shown in the dashboard / CLI.
    description:
        One-paragraph description of what kind of workspace this preset
        is meant for.
    version:
        Integer schema version. ``1`` today; bump when the route shape
        changes incompatibly.
    mode:
        Coarse mode label (``trading``, ``general_operator``, ``coding``,
        ``minimal``…). Surfaces in the capability matrix / docs.
    routes:
        Same shape as the legacy ``planner.routes`` table.
    fallback:
        Default route name when no pattern matches.
    capabilities:
        Optional metadata listing the capability tags this preset
        expects. Today this is only documentation; in the future a
        skill selector can match installed skills against these tags.
    """

    id: str
    name: str
    description: str
    version: int
    mode: str
    routes: dict[str, Any]
    fallback: str
    capabilities: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "mode": self.mode,
            "routes": dict(self.routes),
            "fallback": self.fallback,
            "capabilities": list(self.capabilities),
        }


_TRADING_V1 = RouteManifest(
    id="trading-v1",
    name="Trading default",
    description=(
        "Nerya's historical trading workspace preset. Wires news / "
        "on-chain / risk / strategy lanes to their dedicated subagents "
        "and reserves the high tier for verification + planning."
    ),
    version=1,
    mode="trading",
    capabilities=[
        "market_data", "portfolio", "trading", "risk",
        "news_social", "websearch", "onchain", "strategy", "strategy_review",
        "subagent", "trace", "memory", "message", "workspace",
        "market_data_routing", "market_research", "quant_research",
        "research_report",
    ],
    routes={
        "price_signal": {
            "match": ["price.*"],
            "subagents": ["market_analyst", "risk_critic"],
            "skills": ["market_data", "portfolio", "trading",
                       "risk", "message"],
            "tier": "medium",
        },
        "news_signal": {
            "match": ["news.*", "social.*"],
            "subagents": ["news_interpreter", "market_analyst"],
            "skills": ["news_social", "websearch", "market_data", "trading",
                       "risk", "message"],
            "tier": "medium",
            "escalate_high_on": {
                "payload.impact": ["high", "critical"],
                "payload.headline_contains": ["breaking", "urgent"],
            },
        },
        "onchain_signal": {
            "match": ["onchain.*"],
            "subagents": ["onchain_watcher", "market_analyst"],
            "skills": ["onchain", "market_data", "portfolio",
                       "risk", "message"],
            "tier": "medium",
        },
        "portfolio_review": {
            "match": ["portfolio.heartbeat", "portfolio.rebalance"],
            "subagents": ["portfolio_auditor"],
            "skills": ["portfolio", "market_data", "risk", "message"],
            "tier": "light",
        },
        "risk_alert": {
            "match": ["risk.*", "kill_switch"],
            "subagents": ["risk_critic"],
            "skills": ["portfolio", "risk", "trading", "message"],
            "tier": "medium",
        },
        "sdk_order": {
            "match": ["sdk_order*", "manual.order"],
            "subagents": [],
            "skills": ["trading"],
            "tier": "light",
        },
        "strategy_review": {
            "match": ["strategy.*"],
            "subagents": ["market_analyst"],
            "skills": ["strategy_review", "trading", "portfolio"],
            "tier": "medium",
        },
        "verification": {
            "match": [
                "certification.*", "verification.*",
                "gate.promote", "gate.check",
            ],
            "subagents": ["verification_lane"],
            "skills": [
                "strategy", "strategy_review", "portfolio",
                "risk", "market_data", "trace", "message",
            ],
            "tier": "high",
        },
        "planning": {
            "match": [
                "plan.*", "strategy.plan", "user.plan",
            ],
            "subagents": ["plan_lane", "explore_lane"],
            "skills": [
                "strategy", "strategy_review", "portfolio",
                "risk", "market_data", "trace",
            ],
            "tier": "high",
        },
        "exploration": {
            "match": [
                "explore.*", "research.*", "scan.*",
            ],
            "subagents": ["explore_lane"],
            "skills": [
                "market_data", "news_social", "websearch", "onchain",
                "portfolio", "trace", "operator", "research", "analysis",
                "market_data_routing", "market_research", "quant_research",
                "research_report",
            ],
            "tier": "medium",
        },
        "investment_committee": {
            "match": [
                "investment.committee", "research.committee",
                "trade.committee", "committee.review",
            ],
            "team_template": "investment_committee_team",
            "subagents": [],
            "skills": [
                "market_data", "news_social", "websearch", "portfolio",
                "risk", "trace", "operator", "team", "message",
                "research", "analysis", "market_data_routing",
                "market_research", "quant_research", "research_report",
            ],
            "tier": "high",
        },
        "user_chat": {
            # ``manual.*`` chat triggers should not fall through to the bare
            # ``generic`` lane (only market_data /
            # trading / message), which silently filtered out
            # ``create_strategy`` etc. and let the LLM hallucinate success.
            # Match the full set of chat-shaped triggers so a free-form
            # operator prompt always gets the rich skill catalogue.
            #
            # ``workspace`` is the consolidated read-only introspection
            # skill — without it in the lane allow-list the agent loses
            # the ability to query its own strategies / scripts /
            # triggers / portfolio / intent defaults on demand, which is
            # exactly the "no workspace control" failure mode operators
            # complain about. Always include it for chat-shaped lanes.
            "match": [
                "user.chat", "user.message", "chat", "prompt",
                "manual", "manual.*", "operator.*",
            ],
            "subagents": ["market_analyst"],
            "skills": [
                "market_data", "portfolio", "trading", "risk",
                "message", "news_social", "websearch", "onchain",
                "strategy", "strategy_review", "sdk_writer",
                "exchange", "exchange_author", "script", "trigger",
                "evolution", "capability_developer", "subagent", "trace",
                "memory", "operator", "team", "strategy_validation",
                "workspace", "research", "analysis",
                "market_data_routing", "market_research",
                "quant_research", "research_report",
            ],
            "tier": "medium",
            "escalate_high_on": {
                "text_contains": [
                    "urgent", "now", "immediately", "kill", "stop",
                    "emergency", "panic", "liquidate",
                    "write", "script", "schedule", "cron",
                    "create subagent", "spawn", "orchestrate",
                    "postmortem", "backtest", "macd", "rsi",
                    "strategy", "committee",
                    # escalate when the operator explicitly asks
                    # for an agent team / multi-subagent research pass; on
                    # the medium tier the LLM tends to merely *list* team
                    # templates instead of actually launching one.
                    "team", "agents team", "agent team", "deep research",
                    # Also escalate live-research requests so the higher
                    # tier has enough room to chain lookup → script author
                    # → run script → summarise without running out of
                    # iteration budget mid-investigation.
                    "research", "web search", "google", "duckduckgo",
                    "look up",
                ],
            },
        },
        "generic": {
            # The ``*`` fallback also gets the broad chat skill set so an
            # unfamiliar trigger kind cannot blackhole an LLM action.
            # ``workspace`` is included so the agent can always answer
            # "what's in my workspace?" regardless of which lane the
            # trigger fell through to. Restricted profiles can override
            # this from workspace yml.
            "match": ["*"],
            "subagents": ["market_analyst"],
            "skills": [
                "market_data", "portfolio", "trading", "risk",
                "message", "news_social", "websearch", "onchain",
                "strategy", "strategy_review",
                "evolution", "capability_developer", "subagent", "trace",
                "memory", "script", "trigger",
                "operator", "team", "strategy_validation",
                "workspace", "research", "analysis",
                "market_data_routing", "market_research",
                "quant_research", "research_report",
            ],
            "tier": "light",
        },
    },
    fallback="generic",
)


_GENERAL_OPERATOR_V1 = RouteManifest(
    id="general-operator-v1",
    name="General operator",
    description=(
        "runtime preset for non-trading workspaces. Drops the "
        "trading-only lanes and instead routes user chat through "
        "memory / subagent / trace / message skills. Useful when "
        "Nerya is hosting an operator agent without trading "
        "permissions."
    ),
    version=1,
    mode="general_operator",
    capabilities=[
        "memory", "subagent", "trace", "message", "script",
        "evolution", "capability_developer", "workspace",
    ],
    routes={
        "user_chat": {
            "match": ["user.chat", "user.message", "chat", "prompt"],
            "subagents": [],
            "skills": [
                "message", "memory", "subagent", "trace",
                "evolution", "capability_developer",
                "script", "trigger", "workspace",
            ],
            "tier": "medium",
            "escalate_high_on": {
                "text_contains": [
                    "urgent", "now", "immediately",
                    "create subagent", "spawn", "orchestrate",
                    "postmortem", "committee",
                ],
            },
        },
        "exploration": {
            "match": ["explore.*", "research.*", "scan.*"],
            "subagents": ["explore_lane"],
            "skills": ["memory", "trace", "subagent"],
            "tier": "medium",
        },
        "planning": {
            "match": ["plan.*", "user.plan"],
            "subagents": ["plan_lane"],
            "skills": ["memory", "trace", "subagent"],
            "tier": "high",
        },
        "generic": {
            "match": ["*"],
            "subagents": [],
            "skills": ["message", "memory", "trace", "workspace"],
            "tier": "light",
        },
    },
    fallback="generic",
)


_MINIMAL_V1 = RouteManifest(
    id="minimal-v1",
    name="Minimal",
    description=(
        "Bare-minimum preset for boot smoke tests / restricted "
        "workspaces. Only ``message`` + ``trace`` are exposed, every "
        "kind falls through to ``generic``."
    ),
    version=1,
    mode="minimal",
    capabilities=["message", "trace"],
    routes={
        "generic": {
            "match": ["*"],
            "subagents": [],
            "skills": ["message", "trace"],
            "tier": "light",
        },
    },
    fallback="generic",
)


_BUILTIN: dict[str, RouteManifest] = {
    m.id: m for m in (_TRADING_V1, _GENERAL_OPERATOR_V1, _MINIMAL_V1)
}


def builtin_manifests() -> list[RouteManifest]:
    """Return all bundled manifests in stable id order."""

    return [_BUILTIN[k] for k in sorted(_BUILTIN)]


def list_manifest_ids(paths: WorkspacePaths | None = None) -> list[str]:
    """Return bundled + workspace-defined manifest ids (deduped, sorted)."""

    ids: set[str] = set(_BUILTIN.keys())
    if paths is not None:
        external = _external_manifest_dir(paths)
        if external.is_dir():
            for entry in external.iterdir():
                if entry.suffix.lower() in {".yml", ".yaml"} and entry.is_file():
                    ids.add(entry.stem)
    return sorted(ids)


def _external_manifest_dir(paths: WorkspacePaths) -> Path:
    return paths.root / "route_manifests"


def _coerce_manifest_payload(
    payload: dict[str, Any], *, manifest_id: str
) -> RouteManifest:
    routes = payload.get("routes")
    if not isinstance(routes, dict) or not routes:
        raise ValueError(
            f"route manifest {manifest_id!r} is missing a non-empty "
            "'routes' table"
        )
    fallback = payload.get("fallback") or "generic"
    return RouteManifest(
        id=str(payload.get("id") or manifest_id),
        name=str(payload.get("name") or manifest_id),
        description=str(payload.get("description") or ""),
        version=int(payload.get("version") or 1),
        mode=str(payload.get("mode") or "custom"),
        routes=dict(routes),
        fallback=str(fallback),
        capabilities=list(payload.get("capabilities") or []),
    )


def load_manifest(
    manifest_id: str, paths: WorkspacePaths | None = None
) -> RouteManifest:
    """Load a manifest by id.

    External (workspace-defined) manifests under
    ``$workspace/route_manifests/<id>.yml`` win over bundled manifests
    so operators can override a preset without forking Nerya. Raises
    :class:`KeyError` when the id is unknown.
    """

    if paths is not None:
        external = _external_manifest_dir(paths) / f"{manifest_id}.yml"
        if external.is_file():
            payload = yaml_io.load(external, default={}) or {}
            if isinstance(payload, dict):
                return _coerce_manifest_payload(payload, manifest_id=manifest_id)
        external_yaml = _external_manifest_dir(paths) / f"{manifest_id}.yaml"
        if external_yaml.is_file():
            payload = yaml_io.load(external_yaml, default={}) or {}
            if isinstance(payload, dict):
                return _coerce_manifest_payload(payload, manifest_id=manifest_id)
    if manifest_id in _BUILTIN:
        return _BUILTIN[manifest_id]
    raise KeyError(f"unknown route manifest id: {manifest_id!r}")


def resolve_routes(
    config: Any,
    paths: WorkspacePaths | None = None,
) -> tuple[dict[str, Any], str, str | None]:
    """Resolve the active route table for a config.

    Returns ``(routes, fallback, manifest_id)``. ``manifest_id`` is the
    selected manifest id when ``agent.planner.manifest`` is configured
    or ``None`` when the workspace is using the freeform
    ``agent.planner.routes`` table.
    """

    manifest_id: str | None = None
    if config is not None and hasattr(config, "get"):
        manifest_id = config.get("agent.planner.manifest") or None

    if manifest_id:
        manifest = load_manifest(manifest_id, paths=paths)
        return dict(manifest.routes), manifest.fallback, manifest.id
    return {}, "generic", None


def manifest_summary(
    paths: WorkspacePaths | None = None,
) -> list[dict[str, Any]]:
    """Return a JSON-friendly list describing every available manifest."""

    seen: dict[str, RouteManifest] = {m.id: m for m in builtin_manifests()}
    if paths is not None:
        external = _external_manifest_dir(paths)
        if external.is_dir():
            for entry in sorted(external.iterdir()):
                if entry.suffix.lower() not in {".yml", ".yaml"}:
                    continue
                payload = yaml_io.load(entry, default={}) or {}
                if not isinstance(payload, dict):
                    continue
                manifest_id = entry.stem
                seen[manifest_id] = _coerce_manifest_payload(
                    payload, manifest_id=manifest_id
                )
    return [m.as_dict() for m in seen.values()]


def normalised_capabilities(manifest: RouteManifest) -> list[str]:
    """Return ``manifest.capabilities`` deduped while preserving order."""

    out: list[str] = []
    seen: set[str] = set()
    for cap in _walk(manifest.capabilities):
        if cap in seen:
            continue
        seen.add(cap)
        out.append(cap)
    return out


def _walk(values: Iterable[Any]) -> Iterable[str]:
    for v in values or []:
        if isinstance(v, str) and v:
            yield v
