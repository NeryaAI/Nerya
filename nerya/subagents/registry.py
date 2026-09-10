"""SubAgent registry — reads prompts from workspace/subagents/*.agent.md.

Beyond the read path, this module also exposes a small *write* path the
operator and the model both use to manage **persistent roles**:

* :func:`save_role` upserts a role (prompt body + allowed skills + tier)
  under ``<workspace>/subagents/<name>.agent.md`` and a sibling
  ``<name>.role.yaml`` that carries the structured fields.
* :func:`delete_role` removes both files.
* :func:`describe_role` returns the merged record (prompt + skills + tier
  + persistent flag) so dashboards / native tools render a single shape
  for default and operator-defined roles.

The two-file layout keeps the prompt body free of YAML noise (so an
operator can edit it as plain Markdown) while still letting us pin
structured config like ``allowed_skills``. When ``<name>.role.yaml`` is
missing we fall back to the entries in :data:`DEFAULT_SUBAGENT_SKILLS` /
:data:`DEFAULT_TIERS`.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..core import yaml_io
from ..core.paths import WorkspacePaths
from ..workspace.prompt_bundles import load_bundle as _load_prompt_bundle


_LOG = logging.getLogger(__name__)
_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass
class SubAgentExecutionPolicy:
    """Declarative runtime constraints for a subagent role.

    Role prompts decide *what* to do. This policy only supplies generic
    execution boundaries: tool visibility, argument defaults, budgets, and an
    optional locked tier. It is serialisable in ``*.role.yaml`` and in prompt
    bundle manifests, so the runtime never needs role-name branches.
    """

    locked_tier: str = ""
    allow_model_override: bool = True
    model_override_scope: str = "any"
    # None inherits the parent surface; [] deliberately exposes no native tools.
    native_tool_allow: list[str] | None = None
    native_tool_deny: list[str] = field(default_factory=list)
    required_native_tools: list[str] = field(default_factory=list)
    preload_skills: list[str] = field(default_factory=list)
    tool_argument_defaults: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_iterations: int | None = None
    max_skill_calls: int | None = None
    max_wall_seconds: float | None = None
    llm_max_attempts: int | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> "SubAgentExecutionPolicy":
        if isinstance(raw, cls):
            return cls.from_dict(raw.asdict())
        data = raw if isinstance(raw, dict) else {}
        native = data.get("native_tools") if isinstance(data.get("native_tools"), dict) else {}
        defaults_raw = data.get("tool_argument_defaults")
        defaults = {
            str(name): dict(values)
            for name, values in (defaults_raw.items() if isinstance(defaults_raw, dict) else [])
            if isinstance(values, dict)
        }

        def _names(value: Any) -> list[str]:
            if not isinstance(value, (list, tuple)):
                return []
            return [str(item).strip() for item in value if str(item).strip()]

        def _limit(key: str, cast: Any = int, minimum: float = 0) -> Any:
            value = data.get(key)
            if value is None:
                return None
            try:
                parsed = cast(value)
                if not math.isfinite(parsed) or parsed < minimum:
                    raise ValueError
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{key} must be finite and >= {minimum}") from exc
            return parsed

        allow_raw = native.get("allow")
        if allow_raw is not None and not isinstance(allow_raw, (list, tuple)):
            raise ValueError("native_tools.allow must be a list or null")
        return cls(
            locked_tier=str(data.get("locked_tier") or "").strip(),
            allow_model_override=data.get("allow_model_override") is not False,
            model_override_scope=(
                str(data.get("model_override_scope") or "any").strip().lower()
                if str(data.get("model_override_scope") or "any").strip().lower()
                in {"any", "tier_routes", "none"}
                else "any"
            ),
            native_tool_allow=None if allow_raw is None else _names(allow_raw),
            native_tool_deny=_names(native.get("deny")),
            required_native_tools=_names(data.get("required_native_tools")),
            preload_skills=_names(data.get("preload_skills")),
            tool_argument_defaults=defaults,
            max_iterations=_limit("max_iterations", minimum=1),
            max_skill_calls=_limit("max_skill_calls"),
            max_wall_seconds=_limit("max_wall_seconds", float),
            llm_max_attempts=_limit("llm_max_attempts", minimum=1),
        )

    def asdict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.locked_tier:
            out["locked_tier"] = self.locked_tier
        if not self.allow_model_override:
            out["allow_model_override"] = False
        if self.model_override_scope != "any":
            out["model_override_scope"] = self.model_override_scope
        native: dict[str, Any] = {}
        if self.native_tool_allow is not None:
            native["allow"] = list(self.native_tool_allow)
        if self.native_tool_deny:
            native["deny"] = list(self.native_tool_deny)
        if native:
            out["native_tools"] = native
        if self.required_native_tools:
            out["required_native_tools"] = list(self.required_native_tools)
        if self.preload_skills:
            out["preload_skills"] = list(self.preload_skills)
        if self.tool_argument_defaults:
            out["tool_argument_defaults"] = {
                name: dict(values)
                for name, values in self.tool_argument_defaults.items()
            }
        for key in (
            "max_iterations",
            "max_skill_calls",
            "max_wall_seconds",
            "llm_max_attempts",
        ):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    def merged(self, override: Any) -> "SubAgentExecutionPolicy":
        """Merge a downstream role policy without widening base constraints."""

        other = SubAgentExecutionPolicy.from_dict(override)
        if self.native_tool_allow is None:
            allow = other.native_tool_allow
        elif other.native_tool_allow is None:
            allow = list(self.native_tool_allow)
        else:
            allow = [name for name in self.native_tool_allow if name in other.native_tool_allow]
        defaults = {
            name: dict(values) for name, values in self.tool_argument_defaults.items()
        }
        for name, values in other.tool_argument_defaults.items():
            defaults.setdefault(name, {}).update(values)

        def _bounded(base: Any, incoming: Any) -> Any:
            if base is None:
                return incoming
            if incoming is None:
                return base
            return min(base, incoming)

        scope_order = {"any": 0, "tier_routes": 1, "none": 2}
        model_override_scope = max(
            (self.model_override_scope, other.model_override_scope),
            key=lambda value: scope_order.get(value, 0),
        )

        return SubAgentExecutionPolicy(
            locked_tier=self.locked_tier or other.locked_tier,
            allow_model_override=(
                self.allow_model_override and other.allow_model_override
            ),
            model_override_scope=model_override_scope,
            native_tool_allow=allow,
            native_tool_deny=sorted(set(self.native_tool_deny) | set(other.native_tool_deny)),
            required_native_tools=list(dict.fromkeys([
                *self.required_native_tools,
                *other.required_native_tools,
            ])),
            preload_skills=list(dict.fromkeys([
                *self.preload_skills,
                *other.preload_skills,
            ])),
            tool_argument_defaults=defaults,
            max_iterations=_bounded(self.max_iterations, other.max_iterations),
            max_skill_calls=_bounded(self.max_skill_calls, other.max_skill_calls),
            max_wall_seconds=_bounded(self.max_wall_seconds, other.max_wall_seconds),
            llm_max_attempts=_bounded(self.llm_max_attempts, other.llm_max_attempts),
        )


@dataclass
class SubAgentSpec:
    name: str
    prompt_path: Path
    prompt: str = ""
    allowed_skills: list[str] = field(default_factory=list)
    tier: str = "medium"
    canonical_name: str | None = None
    # Optional per-role model override. When set, the runtime routes this
    # role's LLM calls to ``provider``/``model`` instead of the tier's
    # default pair (tier still governs task gating and budgets).
    provider: str = ""
    model: str = ""
    execution_policy: SubAgentExecutionPolicy = field(
        default_factory=SubAgentExecutionPolicy,
    )

    def __post_init__(self) -> None:
        if not self.canonical_name:
            self.canonical_name = self.name
        if not isinstance(self.execution_policy, SubAgentExecutionPolicy):
            self.execution_policy = SubAgentExecutionPolicy.from_dict(
                self.execution_policy,
            )
        if self.execution_policy.locked_tier:
            self.tier = self.execution_policy.locked_tier
        if (
            not self.execution_policy.allow_model_override
            or self.execution_policy.model_override_scope == "none"
        ):
            self.provider = ""
            self.model = ""

    @classmethod
    def load(cls, path: Path, *, name: str | None = None,
             allowed_skills: list[str] | None = None,
             tier: str = "medium",
             canonical_name: str | None = None,
             provider: str = "",
             model: str = "",
             execution_policy: Any = None) -> "SubAgentSpec":
        n = name or path.stem.replace(".agent", "")
        prompt = path.read_text(encoding="utf-8") if path.exists() else ""
        return cls(name=n, prompt_path=path, prompt=prompt,
                   allowed_skills=allowed_skills or [], tier=tier,
                   canonical_name=canonical_name,
                   provider=provider, model=model,
                   execution_policy=SubAgentExecutionPolicy.from_dict(execution_policy))


_RESEARCH_HINTS = [
    "market_data", "markets", "market_data_routing", "research",
    "web_search_fetch", "browser",
]
_MARKET_RESEARCH_HINTS = [*_RESEARCH_HINTS, "market_research"]
_DECISION_HINTS = [*_MARKET_RESEARCH_HINTS, "portfolio_summary", "risk_check"]

DEFAULT_SUBAGENT_SKILLS = {
    "market_analyst": [*_MARKET_RESEARCH_HINTS, "analysis"],
    "technical_analyst": [*_MARKET_RESEARCH_HINTS, "analysis", "quant_research"],
    "fundamentals_analyst": [*_MARKET_RESEARCH_HINTS, "research_report", "analysis"],
    "sentiment_analyst": [*_RESEARCH_HINTS, "news_social"],
    "macro_strategist": [*_MARKET_RESEARCH_HINTS, "research_report", "analysis"],
    "quant_researcher": [
        "market_data", "markets", "market_data_routing", "analysis",
        "quant_research", "backtest",
    ],
    "bull_researcher": [*_DECISION_HINTS, "research_report", "quant_research"],
    "bear_researcher": [*_DECISION_HINTS, "research_report", "quant_research"],
    "research_manager": [*_DECISION_HINTS, "research_report"],
    "research_editor": ["research_report", "market_research", "analysis", "llm"],
    "risk_critic": [*_MARKET_RESEARCH_HINTS, "risk_check", "portfolio_summary"],
    "execution_planner": ["market_data", "portfolio_summary", "risk_check"],
    "onchain_watcher": [*_RESEARCH_HINTS],
    "news_interpreter": ["news_social", "research", "web_search_fetch"],
    "portfolio_manager": ["portfolio_summary"],
    "portfolio_auditor": ["portfolio_summary", "risk_check", "market_data", "notify"],
    "strategy_reviewer": ["strategy_author", "backtest", "coding"],
    "message_writer": ["notify", "llm"],
    "verification_lane": [
        "strategy_author", "backtest", "portfolio_summary", "risk_check",
        "market_data", "coding",
    ],
    "plan_lane": [
        "strategy_author", "markets", "market_data_routing",
        "portfolio_summary", "risk_check",
    ],
    "explore_lane": [*_MARKET_RESEARCH_HINTS, "news_social", "analysis"],
    "strategy_tuner": [
        "strategy_author", "backtest", "markets", "analysis",
        "quant_research", "portfolio_summary", "risk_check",
    ],
    "coding_agent": ["coding", "research"],
    "code_critic": ["coding", "analysis"],
    # Distilled investor lenses — each lane preloads exactly one
    # expert sub-skill (expert_investors.<name>) so a five-expert
    # committee never drags all five playbooks into every lane.
    "buffett_lens": [
        "expert_investors.buffett", *_MARKET_RESEARCH_HINTS,
        "research_report",
    ],
    "damodaran_lens": [
        "expert_investors.damodaran", *_MARKET_RESEARCH_HINTS,
        "research_report", "analysis",
    ],
    "marks_lens": [
        "expert_investors.marks", *_MARKET_RESEARCH_HINTS,
        "research_report", "risk_check",
    ],
    "mauboussin_lens": [
        "expert_investors.mauboussin", *_MARKET_RESEARCH_HINTS,
        "research_report", "quant_research",
    ],
    "druckenmiller_lens": [
        "expert_investors.druckenmiller", *_MARKET_RESEARCH_HINTS,
        "research_report", "news_social",
    ],
    "serenity_lens": [
        "finance-creators.serenity", *_MARKET_RESEARCH_HINTS,
        "research_report", "news_social",
    ],
    "unusual_whales_lens": [
        "finance-creators.unusual_whales", *_MARKET_RESEARCH_HINTS,
        "research_report", "news_social",
    ],
    "kobeissi_lens": [
        "finance-creators.kobeissi", *_MARKET_RESEARCH_HINTS,
        "research_report", "news_social",
    ],
    # Dedicated web-data collection lane. Cheap (light tier) by design:
    # it fetches with the built-in search-engine chain + browser fallback,
    # persists the *complete* raw captures under
    # ``workspace/state/research_data/`` and hands the file paths back so
    # analyst / expert lanes read the full data instead of re-fetching.
    "web_researcher": [
        "research", "web_search_fetch", "browser", "news_social",
    ],
}


DEFAULT_SUBAGENT_PROFILES: dict[str, str] = {
    "fundamental_analyst": "fundamentals_analyst",
    "financial_analyst": "fundamentals_analyst",
    "valuation_analyst": "fundamentals_analyst",
    "valuation_reviewer": "fundamentals_analyst",
    "dcf_analyst": "fundamentals_analyst",
    "dcf_modeler": "fundamentals_analyst",
    "sec_analyst": "fundamentals_analyst",
    "sec_filing_analyst": "fundamentals_analyst",
    "sec_filing_reviewer": "fundamentals_analyst",
    "filing_reviewer": "fundamentals_analyst",
    "investor_perspective": "fundamentals_analyst",
    "guru_perspective": "fundamentals_analyst",
    "investment_gurus": "fundamentals_analyst",
    # Distilled expert lenses: a role requested under the bare expert
    # name executes on the matching lens lane. Pure data — discovery
    # stays with role_list and the expert hub skills.
    "buffett": "buffett_lens",
    "damodaran": "damodaran_lens",
    "marks": "marks_lens",
    "howard_marks": "marks_lens",
    "mauboussin": "mauboussin_lens",
    "druckenmiller": "druckenmiller_lens",
    "serenity": "serenity_lens",
    "unusual_whales": "unusual_whales_lens",
    "kobeissi": "kobeissi_lens",
    # Web-data collection synonyms all land on the dedicated researcher
    # lane so ad-hoc names like "web_scraper" reuse its prompt/tier.
    "web_scraper": "web_researcher",
    "data_scout": "web_researcher",
    "web_research": "web_researcher",
    "data_collector": "web_researcher",
}


def canonical_subagent_name(name: str) -> str:
    """Return the execution profile for a requested role name.

    TeamStore and event streams keep the requested role identity. The
    canonical profile only selects default prompt, skills, budget, and
    evidence policy when the model or operator uses a near-synonym role name.
    """

    raw = str(name or "").strip()
    if not raw:
        return raw
    if raw in DEFAULT_SUBAGENT_SKILLS:
        return raw
    normalised = raw.lower().replace("-", "_").replace(" ", "_")
    if normalised in DEFAULT_SUBAGENT_SKILLS:
        return normalised
    explicit = DEFAULT_SUBAGENT_PROFILES.get(normalised)
    if explicit:
        return explicit
    return raw


# One declared source of role instructions. Missing package data is an installation error.

_DEFAULT_PROMPT_BUNDLE = _load_prompt_bundle()
DEFAULT_SUBAGENT_PROMPTS: dict[str, str] = dict(_DEFAULT_PROMPT_BUNDLE.subagents)


DEFAULT_TIERS = {
    "market_analyst": "medium",
    "technical_analyst": "medium",
    "fundamentals_analyst": "medium",
    "sentiment_analyst": "light",
    "macro_strategist": "medium",
    "quant_researcher": "high",
    "bull_researcher": "medium",
    "bear_researcher": "medium",
    "research_manager": "high",
    "research_editor": "medium",
    "risk_critic": "medium",
    "execution_planner": "medium",
    "onchain_watcher": "medium",
    "news_interpreter": "light",
    "portfolio_manager": "medium",
    "portfolio_auditor": "light",
    "strategy_reviewer": "high",
    "strategy_tuner": "high",
    "message_writer": "light",
    "verification_lane": "high",
    "plan_lane": "high",
    "explore_lane": "medium",
    "coding_agent": "high",
    "code_critic": "high",
    "buffett_lens": "medium",
    "damodaran_lens": "medium",
    "marks_lens": "medium",
    "mauboussin_lens": "medium",
    "druckenmiller_lens": "medium",
    "serenity_lens": "medium",
    "unusual_whales_lens": "medium",
    "kobeissi_lens": "medium",
    # Data collection is deliberately cheap: the researcher only drives
    # search/fetch tools and summarises what it saved, so the light tier
    # is enough. The bundle execution policy locks this role to light.
    "web_researcher": "light",
}


DEFAULT_SUBAGENT_EXECUTION_POLICIES: dict[str, SubAgentExecutionPolicy] = {
    name: SubAgentExecutionPolicy.from_dict(raw)
    for name, raw in _DEFAULT_PROMPT_BUNDLE.subagent_policies.items()
}


def default_execution_policy(
    name: str,
    override: Any = None,
) -> SubAgentExecutionPolicy:
    """Resolve a canonical bundle policy plus an optional stricter override."""

    canonical = canonical_subagent_name(name)
    base = DEFAULT_SUBAGENT_EXECUTION_POLICIES.get(
        canonical,
        SubAgentExecutionPolicy(),
    )
    return SubAgentExecutionPolicy.from_dict(base).merged(override)


def load_registry(paths: WorkspacePaths) -> dict[str, SubAgentSpec]:
    root = paths.subagents
    out: dict[str, SubAgentSpec] = {}
    if not root.exists():
        return out
    for p in sorted(root.glob("*.agent.md")):
        name = p.stem.replace(".agent", "")
        canonical = canonical_subagent_name(name)
        meta = _load_role_meta(root, name)
        allowed = meta.get("allowed_skills") or DEFAULT_SUBAGENT_SKILLS.get(canonical, [])
        tier = meta.get("tier") or DEFAULT_TIERS.get(canonical, "medium")
        out[name] = SubAgentSpec.load(
            p, name=name,
            allowed_skills=list(allowed),
            tier=str(tier),
            canonical_name=canonical,
            provider=str(meta.get("provider") or ""),
            model=str(meta.get("model") or ""),
            execution_policy=default_execution_policy(
                canonical,
                meta.get("execution_policy"),
            ),
        )
    return out


# ---------------------------------------------------------------------------
# Persistent role CRUD
# ---------------------------------------------------------------------------


def _validate_role_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            f"role name must match [A-Za-z0-9_]+, got {name!r}"
        )
    return name


def _prompt_path(paths: WorkspacePaths, name: str) -> Path:
    return paths.subagents / f"{name}.agent.md"


def _meta_path(paths: WorkspacePaths, name: str) -> Path:
    return paths.subagents / f"{name}.role.yaml"


def _load_role_meta(root: Path, name: str) -> dict[str, Any]:
    p = root / f"{name}.role.yaml"
    if not p.exists():
        return {}
    try:
        data = yaml_io.load(p, default={}) or {}
    except Exception:
        _LOG.exception("failed to read role meta %s", p)
        return {}
    return data if isinstance(data, dict) else {}


def list_roles(paths: WorkspacePaths) -> list[dict[str, Any]]:
    """Return every role visible to the agent — workspace + defaults.

    Workspace entries shadow default entries with the same name. The
    output shape matches the dashboard's expectations: ``name`` /
    ``tier`` / ``allowed_skills`` / ``persistent`` (True if the
    operator edited it on disk) / ``prompt_path`` (or ``None`` when
    the role exists only as a default).
    """

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    workspace = load_registry(paths)
    for name, spec in sorted(workspace.items()):
        out.append({
            "name": name,
            "tier": spec.tier,
            "allowed_skills": list(spec.allowed_skills),
            "persistent": True,
            "source": "workspace",
            "canonical_name": spec.canonical_name or name,
            "provider": spec.provider,
            "model": spec.model,
            "execution_policy": spec.execution_policy.asdict(),
            "prompt_path": str(spec.prompt_path),
            "prompt_excerpt": (spec.prompt or "")[:280],
        })
        seen.add(name)

    for name, allowed in sorted(DEFAULT_SUBAGENT_SKILLS.items()):
        if name in seen:
            continue
        out.append({
            "name": name,
            "tier": DEFAULT_TIERS.get(name, "medium"),
            "allowed_skills": list(allowed),
            "persistent": False,
            "source": "default",
            "canonical_name": name,
            "provider": "",
            "model": "",
            "execution_policy": default_execution_policy(name).asdict(),
            "prompt_path": None,
            "prompt_excerpt": "",
        })
    return out


def describe_role(paths: WorkspacePaths, name: str) -> Optional[dict[str, Any]]:
    """Return the full record for one role, or ``None`` if it doesn't exist."""

    _validate_role_name(name)
    workspace = load_registry(paths)
    if name in workspace:
        spec = workspace[name]
        return {
            "name": name,
            "tier": spec.tier,
            "allowed_skills": list(spec.allowed_skills),
            "persistent": True,
            "source": "workspace",
            "canonical_name": spec.canonical_name or name,
            "provider": spec.provider,
            "model": spec.model,
            "execution_policy": spec.execution_policy.asdict(),
            "prompt_path": str(spec.prompt_path),
            "prompt": spec.prompt,
        }
    canonical = canonical_subagent_name(name)
    if canonical in DEFAULT_SUBAGENT_SKILLS:
        return {
            "name": name,
            "tier": DEFAULT_TIERS.get(canonical, "medium"),
            "allowed_skills": list(DEFAULT_SUBAGENT_SKILLS[canonical]),
            "persistent": False,
            "source": "default" if canonical == name else "default_profile",
            "canonical_name": canonical,
            "provider": "",
            "model": "",
            "execution_policy": default_execution_policy(canonical).asdict(),
            "prompt_path": None,
            "prompt": DEFAULT_SUBAGENT_PROMPTS.get(canonical, ""),
        }
    return None


def save_role(
    paths: WorkspacePaths,
    *,
    name: str,
    prompt: str,
    allowed_skills: Optional[list[str]] = None,
    tier: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    execution_policy: Any = None,
) -> dict[str, Any]:
    """Upsert a persistent role. Creates ``<name>.agent.md`` + ``<name>.role.yaml``.

    Existing files are overwritten. Returns the new record (same shape
    as :func:`describe_role`). The denylist on ``allowed_skills`` is not
    enforced here — the dispatcher applies it at runtime so operators
    can write whatever they want and the harness still keeps trading
    surfaces safe.
    """

    _validate_role_name(name)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    root = paths.subagents
    root.mkdir(parents=True, exist_ok=True)

    prompt_path = _prompt_path(paths, name)
    prompt_path.write_text(prompt, encoding="utf-8")

    canonical = canonical_subagent_name(name)
    final_skills = list(allowed_skills or DEFAULT_SUBAGENT_SKILLS.get(canonical, []))
    final_tier = str(tier or DEFAULT_TIERS.get(canonical, "medium"))
    final_provider = str(provider or "").strip()
    final_model = str(model or "").strip()
    final_policy = default_execution_policy(canonical, execution_policy)
    if final_policy.locked_tier:
        final_tier = final_policy.locked_tier
    if (
        not final_policy.allow_model_override
        or final_policy.model_override_scope == "none"
    ):
        final_provider = ""
        final_model = ""

    meta = {
        "name": name,
        "tier": final_tier,
        "allowed_skills": final_skills,
        "canonical_name": canonical,
    }
    if final_provider:
        meta["provider"] = final_provider
    if final_model:
        meta["model"] = final_model
    if final_policy.asdict():
        meta["execution_policy"] = final_policy.asdict()
    yaml_io.dump(_meta_path(paths, name), meta)

    return {
        "name": name,
        "tier": final_tier,
        "allowed_skills": final_skills,
        "canonical_name": canonical,
        "provider": final_provider,
        "model": final_model,
        "execution_policy": final_policy.asdict(),
        "persistent": True,
        "source": "workspace",
        "prompt_path": str(prompt_path),
        "prompt": prompt,
    }


# Safe, read-only skill set handed to an ad-hoc role when neither an inline
# spec nor a canonical default names any skills. Every entry is a research /
# analysis surface — none of them are on ``SUBAGENT_SKILL_DENYLIST`` (trading /
# trading_write / wallet / script_runtime), so a synthesised role can gather
# evidence and reason but never reaches a live-trading or signer surface.
GENERIC_ADHOC_SKILLS: tuple[str, ...] = (
    "research",
    "web_search",
    "web_search_fetch",
    "news_social",
    "market_research",
    "analysis",
    "llm",
    "trace",
    "browser",
)


def generic_role_prompt(name: str) -> str:
    """Default prompt body for a role with no registered/inline prompt.

    The lead agent can spin up a brand-new role name (e.g. ``equity_researcher``
    or ``spacex_valuation``) without first calling ``save_role``; this body
    gives that ephemeral role real scope, an evidence discipline, and an output
    contract so it never runs blank.
    """

    label = (name or "specialist").replace("_", " ").strip() or "specialist"
    return (
        f"You are the **{name}** lane — an ad-hoc specialist the lead agent "
        "spun up on demand because no pre-registered role matched the task. "
        f"Act as a focused {label}.\n\n"
        "Mission. Execute the assignment in your task payload and any "
        "role-specific instructions with evidence-first rigor. The role name "
        "and the shared team task define your scope; stay inside it.\n\n"
        "How you work.\n"
        "1. Read the shared team task and your payload (``target`` / ``focus`` "
        "fields) before doing anything else.\n"
        "2. Gather evidence with the tools you are granted (web_search / "
        "web_search_fetch / news_social / market_data when available). Every "
        "material claim must cite a tool result or a fetched source. For "
        "company primary sources (IR pages, filings, annual reports) prefer "
        "web_fetch — it renders JS pages via the configured browser engine "
        "automatically; open an interactive ``browser`` session (via "
        "script_run browser_session.py) only when navigation or clicks are "
        "required.\n"
        "3. If a required source, credential, feed, or dataset is missing, say "
        "so plainly and report the evidence gap — never invent mock, "
        "placeholder, synthetic, or proxy data, and mark every estimate or "
        "assumption explicitly.\n\n"
        "Output. Return a compact JSON object with your findings plus an "
        "``evidence`` array of ``{source, claim}`` entries and a short "
        "``summary``. Answer in the requested output/analysis language."
    )


def build_inline_spec(
    paths: WorkspacePaths,
    *,
    name: str,
    prompt: Optional[str] = None,
    allowed_skills: Optional[list[str]] = None,
    tier: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    execution_policy: Any = None,
) -> SubAgentSpec:
    """Build an *ephemeral* role spec from inline fields (never written to disk).

    Unlike :func:`save_role`, this does not touch the workspace: the spec lives
    only for the current dispatch. It lets the lead agent define a temporary
    role inline (in ``team_run`` / ``subagent_run``) instead of being forced to
    reuse a registered role or persist a new one. Layered defaults keep the
    role useful even when only a name is supplied: explicit inline prompt/skills
    win, then the canonical default profile, then the generic ad-hoc fallback.
    """

    canonical = canonical_subagent_name(name)
    body = (prompt or "").strip()
    if not body:
        canonical_prompt = DEFAULT_SUBAGENT_PROMPTS.get(canonical, "") or ""
        body = canonical_prompt if canonical_prompt.strip() else generic_role_prompt(name)
    skills = [str(s).strip() for s in (allowed_skills or []) if str(s).strip()]
    if not skills:
        skills = list(DEFAULT_SUBAGENT_SKILLS.get(canonical, []))
    if not skills:
        skills = list(GENERIC_ADHOC_SKILLS)
    final_tier = str(tier or DEFAULT_TIERS.get(canonical, "medium"))
    final_policy = default_execution_policy(canonical, execution_policy)
    return SubAgentSpec(
        name=name,
        prompt_path=_prompt_path(paths, name),
        prompt=body,
        allowed_skills=skills,
        tier=final_tier,
        canonical_name=canonical,
        provider=str(provider or "").strip(),
        model=str(model or "").strip(),
        execution_policy=final_policy,
    )


def delete_role(paths: WorkspacePaths, name: str) -> bool:
    """Remove a persistent role. Returns ``True`` if anything was deleted.

    Default roles are not removable (they live in code) — calling this
    on a default name is a no-op and returns ``False``.
    """

    _validate_role_name(name)
    deleted = False
    for p in (_prompt_path(paths, name), _meta_path(paths, name)):
        try:
            if p.exists():
                p.unlink()
                deleted = True
        except OSError:
            _LOG.exception("failed to delete role file %s", p)
    return deleted


__all__ = [
    "DEFAULT_SUBAGENT_PROMPTS",
    "DEFAULT_SUBAGENT_EXECUTION_POLICIES",
    "DEFAULT_SUBAGENT_PROFILES",
    "DEFAULT_SUBAGENT_SKILLS",
    "DEFAULT_TIERS",
    "GENERIC_ADHOC_SKILLS",
    "SubAgentSpec",
    "SubAgentExecutionPolicy",
    "build_inline_spec",
    "canonical_subagent_name",
    "delete_role",
    "describe_role",
    "default_execution_policy",
    "generic_role_prompt",
    "list_roles",
    "load_registry",
    "save_role",
]
