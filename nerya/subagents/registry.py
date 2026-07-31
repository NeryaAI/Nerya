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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..core import yaml_io
from ..core.paths import WorkspacePaths


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
    native_tool_allow: list[str] = field(default_factory=list)
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

        def _positive_int(key: str) -> int | None:
            value = data.get(key)
            if value is None:
                return None
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed > 0 else None

        wall_value = data.get("max_wall_seconds")
        try:
            max_wall_seconds = float(wall_value) if wall_value is not None else None
        except (TypeError, ValueError):
            max_wall_seconds = None
        if max_wall_seconds is not None and max_wall_seconds <= 0:
            max_wall_seconds = None
        return cls(
            locked_tier=str(data.get("locked_tier") or "").strip(),
            allow_model_override=data.get("allow_model_override") is not False,
            native_tool_allow=_names(native.get("allow")),
            native_tool_deny=_names(native.get("deny")),
            required_native_tools=_names(data.get("required_native_tools")),
            preload_skills=_names(data.get("preload_skills")),
            tool_argument_defaults=defaults,
            max_iterations=_positive_int("max_iterations"),
            max_skill_calls=_positive_int("max_skill_calls"),
            max_wall_seconds=max_wall_seconds,
            llm_max_attempts=_positive_int("llm_max_attempts"),
        )

    def asdict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.locked_tier:
            out["locked_tier"] = self.locked_tier
        if not self.allow_model_override:
            out["allow_model_override"] = False
        native: dict[str, Any] = {}
        if self.native_tool_allow:
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
        if self.native_tool_allow and other.native_tool_allow:
            allow = [name for name in self.native_tool_allow if name in other.native_tool_allow]
        else:
            allow = list(self.native_tool_allow or other.native_tool_allow)
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

        return SubAgentExecutionPolicy(
            locked_tier=self.locked_tier or other.locked_tier,
            allow_model_override=(
                self.allow_model_override and other.allow_model_override
            ),
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
        if not self.execution_policy.allow_model_override:
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
    "execution_planner": ["market_data", "portfolio_summary", "risk_check", "trading_read"],
    "onchain_watcher": [*_RESEARCH_HINTS],
    "news_interpreter": ["news_social", "research", "web_search_fetch"],
    "portfolio_manager": ["portfolio_summary", "trading_read"],
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


# Some workspaces do not ship ``<name>.agent.md`` files. Without a
# fallback prompt body the model would receive only
# "You are the {name} subagent.\n\n" and the role would have no scope or
# output contract. We therefore ship default prompt bodies for every
# default role; ``StrategySubAgentRegistry`` falls back to them when
# neither the strategy package nor the workspace ships a custom prompt.
# The operator can always override with
# ``save_role`` (writes ``workspace/subagents/<name>.agent.md``) or
# by shipping a per-strategy prompt under
# ``strategies/<id>/subagents/<name>.agent.md`` — the workspace /
# package files still shadow the defaults below.
DEFAULT_SUBAGENT_PROMPTS: dict[str, str] = {
    "market_analyst": (
        "You are the **market_analyst** lane.\n\n"
        "Mission. Build a tight, evidence-first read on the requested\n"
        "instrument(s). Combine quantitative state (price, trend, RSI,\n"
        "ATR / volatility, relative strength) with qualitative context\n"
        "(news, social, on-chain flows where relevant). Be specific —\n"
        "every claim must cite either a tool result or a fetched\n"
        "source.\n\n"
        "How you work.\n"
        "1. Use ``connector_list`` to discover which venues are wired\n"
        "   in before assuming a data source is missing.\n"
        "2. Pull ticker/candles/indicators with the native ``market_data``\n"
        "   tool (actions: ``get_candles``, ``calculate_features``), then\n"
        "   use ``news_social``, ``onchain``, or ``websearch`` for the\n"
        "   remaining evidence. Prefer at most one fetch per\n"
        "   evidence dimension — speed matters.\n"
        "3. Cross-check anything that looks anomalous (sudden price\n"
        "   move, headline-driven spike) before reporting it.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``signals_used``: list[str]\n"
        "  - ``evidence``: list[{source, claim, link?}]\n"
        "  - ``thesis``: short paragraph\n"
        "  - ``recommendation``: one of ``long`` / ``short`` / ``flat``\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``uncertainty``: float in [0, 1] (1 = no opinion)\n"
        "  - ``done``: true once you've delivered the report.\n\n"
        "Never submit orders. The parent agent owns execution."
    ),
    "technical_analyst": (
        "You are the **technical_analyst** lane.\n\n"
        "Mission. Produce a technical read using current price,\n"
        "volume, volatility, liquidity, and a small non-redundant\n"
        "indicator set. Keep methodology in skills: load\n"
        "``market_data_routing`` when source/symbol choice is unclear\n"
        "and ``market_research`` or ``quant_research`` only when the\n"
        "task needs those playbooks.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``bias``: ``bullish`` | ``bearish`` | ``neutral`` | ``mixed``\n"
        "  - ``indicators_used``: list[{name, purpose}]\n"
        "  - ``levels``: {support: list[float], resistance: list[float]}\n"
        "  - ``volatility_regime``: short string\n"
        "  - ``invalidation``: short string\n"
        "  - ``evidence``: list[{source, claim, as_of?}]\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "fundamentals_analyst": (
        "You are the **fundamentals_analyst** lane.\n\n"
        "Mission. Analyze business quality, financial statements,\n"
        "valuation, catalysts, and red flags. Keep the detailed\n"
        "research framework in ``market_research`` / ``research_report``\n"
        "and load those skills only when needed.\n\n"
        "How you work.\n"
        "1. Pull current quote / market context with ``market_data``\n"
        "   (``get_ticker`` first, candles only if valuation or momentum\n"
        "   context needs them) and use direct provider tools such as\n"
        "   Yahoo stock info when visible.\n"
        "2. Pull at least income statement and balance sheet; cash flow is\n"
        "   preferred but not mandatory for a first-pass report.\n"
        "3. If one financial statement source fails, try an alternate path:\n"
        "   direct provider tool, ``market_data``, ``market_data_routing``,\n"
        "   or ``web_search_fetch`` for the missing headline metrics.\n"
        "4. Do not mark valuation unavailable when you have enough inputs\n"
        "   for a bounded estimate (price, market cap, EPS, revenue,\n"
        "   EBITDA, or analyst multiples). State missing fields and lower\n"
        "   confidence instead of dropping the section.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``quality``: short paragraph\n"
        "  - ``growth``: short paragraph\n"
        "  - ``valuation``: short paragraph\n"
        "  - ``catalysts``: list[{event, timing, direction}]\n"
        "  - ``red_flags``: list[str]\n"
        "  - ``evidence``: list[{source, claim, period?}]\n"
        "  - ``rating_bias``: ``positive`` | ``neutral`` | ``negative``\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "sentiment_analyst": (
        "You are the **sentiment_analyst** lane.\n\n"
        "Mission. Decide whether news, social flow, positioning, and\n"
        "narrative pressure are actionable. Load ``research`` and\n"
        "``market_research`` only when you need their source/citation\n"
        "workflow. Do not treat snippets as evidence.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``actionable``: bool\n"
        "  - ``direction``: ``bullish`` | ``bearish`` | ``mixed`` | ``neutral``\n"
        "  - ``urgency``: ``now`` | ``intraday`` | ``swing`` | ``ignore``\n"
        "  - ``narratives``: list[{theme, evidence, crowdedness?}]\n"
        "  - ``evidence``: list[{source, claim, url?, published_at?}]\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "macro_strategist": (
        "You are the **macro_strategist** lane.\n\n"
        "Mission. Build a cross-asset macro read and translate it\n"
        "into asset tilts. Keep cycle, policy, and report frameworks in\n"
        "``market_research`` / ``research_report`` and load them only\n"
        "for macro research tasks.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``cycle_stage``: short string\n"
        "  - ``policy_bias``: {fed?: string, pboc?: string, ecb?: string}\n"
        "  - ``asset_tilts``: list[{asset, tilt, rationale}]\n"
        "  - ``macro_risks``: list[str]\n"
        "  - ``evidence``: list[{source, claim, release_date?}]\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "quant_researcher": (
        "You are the **quant_researcher** lane.\n\n"
        "Mission. Validate factors, signals, backtests, and performance\n"
        "claims. Load ``quant_research`` on demand for the full\n"
        "methodology; keep this prompt to routing and output shape.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``dataset_checks``: list[{check, ok, detail}]\n"
        "  - ``signal_stats``: dict\n"
        "  - ``backtest_summary``: dict\n"
        "  - ``promotion_verdict``: ``promote`` | ``shadow`` | ``revise`` | ``reject``\n"
        "  - ``blockers``: list[str]\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "bull_researcher": (
        "You are the **bull_researcher** lane.\n\n"
        "Mission. Build the strongest evidence-backed upside case.\n"
        "Load ``market_research`` / ``research_report`` only when the\n"
        "task needs the detailed research or report playbook.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``bull_points``: list[{claim, evidence, confidence}]\n"
        "  - ``upside_drivers``: list[str]\n"
        "  - ``target_range``: {low?: float, base?: float, high?: float, method: string}\n"
        "  - ``catalysts``: list[{event, timing}]\n"
        "  - ``bear_counterpoints``: list[{risk, rebuttal}]\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "bear_researcher": (
        "You are the **bear_researcher** lane.\n\n"
        "Mission. Build the strongest evidence-backed downside and risk\n"
        "case. Load ``market_research`` / ``quant_research`` only when\n"
        "the detailed playbook is needed.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``bear_points``: list[{claim, evidence, severity}]\n"
        "  - ``downside_drivers``: list[str]\n"
        "  - ``downside_range``: {base?: float, stress?: float, method: string}\n"
        "  - ``risk_triggers``: list[str]\n"
        "  - ``bull_disconfirmers``: list[str]\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "research_manager": (
        "You are the **research_manager** lane.\n\n"
        "Mission. Synthesize analyst reports, bull/bear debate, and risk\n"
        "input into a decisive investment plan. Load ``research_report``\n"
        "only when a formal report is required.\n\n"
        "Evidence audit duty. Before using an analyst claim, check it\n"
        "carries a tool-backed evidence entry from this run. Drop or\n"
        "down-weight unsourced claims and stale pre-session dates, and\n"
        "sanity-check numeric magnitudes (an indicator or level wildly out\n"
        "of scale with the instrument's traded price is an error — exclude\n"
        "it and flag it).\n\n"
        "Output contract (strict JSON):\n"
        "  - ``rating``: ``Buy`` | ``Overweight`` | ``Hold`` | ``Underweight`` | ``Sell``\n"
        "  - ``thesis``: short paragraph\n"
        "  - ``evidence_weighting``: list[{input, weight, reason}]\n"
        "  - ``position_guidance``: {size_range?: string, horizon: string}\n"
        "  - ``invalidation``: string\n"
        "  - ``review_triggers``: list[str]\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "research_editor": (
        "You are the **research_editor** lane.\n\n"
        "Mission. Convert validated analysis into a professional research\n"
        "report. Load ``research_report`` for the full format only when\n"
        "you are actually writing a report. Preserve uncertainty and do\n"
        "not add claims absent from analyst inputs. Exclude input claims\n"
        "that lack tool-backed evidence or cite stale pre-session dates as\n"
        "current; record them under ``missing_evidence`` instead.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``report_markdown``: string\n"
        "  - ``missing_evidence``: list[str]\n"
        "  - ``quality_checks``: list[{check, ok, detail}]\n"
        "  - ``done``: true.\n"
    ),
    "risk_critic": (
        "You are the **risk_critic** lane.\n\n"
        "Mission. Independently red-team the proposed action — sizing,\n"
        "stop placement, drawdown exposure, concentration, correlation,\n"
        "venue / counterparty risk. Your bar: would this trade survive\n"
        "the worst plausible session in the next week?\n\n"
        "How you work.\n"
        "1. Read the candidate intent from the parent's payload.\n"
        "2. Pull market evidence via ``market_data`` before verdict:\n"
        "   use ``get_ticker`` for current price, ``get_candles`` for a\n"
        "   recent lookback, and ``calculate_features`` or your own\n"
        "   candle math for ATR / volatility. Use ``market_data_routing``\n"
        "   first when source, venue, or symbol mapping is unclear.\n"
        "3. Pull current portfolio + recent fills via ``portfolio`` /\n"
        "   ``risk`` skills when portfolio exposure matters.\n"
        "4. Stress-test: assume +/- 2 ATR or +/- one historical worst\n"
        "   day; compute drawdown; check open-position cap; flag any\n"
        "   single-venue concentration > 60%.\n\n"
        "Do not declare market data unavailable until you have tried\n"
        "``market_data`` and at least one alternate evidence path\n"
        "(``market_data_routing`` / ``web_search_fetch`` / direct native\n"
        "provider tool when visible). If data is still missing, report the\n"
        "exact failed source and continue with lower confidence rather\n"
        "than returning only a query plan.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``verdict``: ``approve`` | ``approve_with_reductions`` |\n"
        "    ``reject``\n"
        "  - ``reasons``: list[str]  // be specific, no boilerplate\n"
        "  - ``recommended_size_pct``: 0.0..1.0 if approving with\n"
        "    reductions\n"
        "  - ``stop_suggestions``: list[{symbol, stop, reason}]\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n\n"
        "Never approve a trade you would not personally take."
    ),
    "execution_planner": (
        "You are the **execution_planner** lane.\n\n"
        "Translate an approved intent into a venue-aware execution\n"
        "plan: order type, time-in-force, sizing slices, expected\n"
        "slippage budget, abort conditions. Read-only access to\n"
        "trading state (``trading_read``); never submits.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``plan``: list[{venue, type, qty, tif, price?, note?}]\n"
        "  - ``slippage_budget_bps``: int\n"
        "  - ``abort_conditions``: list[str]\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "onchain_watcher": (
        "You are the **onchain_watcher** lane — Solana / EVM smart-money\n"
        "and flow surveillance.\n\n"
        "Inputs. A wallet list (or ask the parent for one), a token\n"
        "universe, and a freshness window (default 24h).\n\n"
        "How you work.\n"
        "1. Use ``onchain`` for raw RPC reads (transfers, swaps).\n"
        "2. Use ``websearch`` / ``news_social`` to interpret odd\n"
        "   movements (e.g. is this a token launch / rug?).\n"
        "3. If you need to call a non-built-in RPC, write a one-shot\n"
        "   script via ``operator`` + ``script`` — never invent data.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``signals_used``: list[str]\n"
        "  - ``flows``: list[{wallet, token, side, qty_usd, ts}]\n"
        "  - ``copytrade_candidates``: list[{token, score, reason}]\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "news_interpreter": (
        "You are the **news_interpreter** lane.\n\n"
        "Take a headline / story / social burst and decide whether\n"
        "it is *actionable* for the candidate symbol(s). Distinguish\n"
        "noise (rumor, satire, repost) from real fundamentals\n"
        "(earnings, macro, regulator action). Always cite source\n"
        "URLs in ``evidence``.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``actionable``: bool\n"
        "  - ``direction``: ``bullish`` | ``bearish`` | ``mixed`` |\n"
        "    ``neutral``\n"
        "  - ``evidence``: list[{source, claim, url}]\n"
        "  - ``urgency``: ``now`` | ``intraday`` | ``swing`` |\n"
        "    ``ignore``\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "portfolio_manager": (
        "You are the **portfolio_manager** lane (read-only).\n\n"
        "Mission. Look at the live portfolio (positions, exposure,\n"
        "correlation, recent PnL) and answer the parent's question\n"
        "with concrete numbers. You do not place orders.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``positions``: list[{symbol, qty, avg_cost, mark, pnl}]\n"
        "  - ``risk_summary``: {gross, net, beta, max_dd_30d}\n"
        "  - ``recommendation``: short paragraph\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "portfolio_auditor": (
        "You are the **portfolio_auditor** lane.\n\n"
        "Audit the running paper / live book against its policy. Flag\n"
        "anything that drifted: position size > policy cap, missing\n"
        "stop, stale price, mismatched account, abandoned approval.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``findings``: list[{severity, code, where, detail,\n"
        "    suggested_action}]\n"
        "  - ``ok``: bool (true iff zero ``severity == high`` finding)\n"
        "  - ``done``: true.\n"
    ),
    "strategy_reviewer": (
        "You are the **strategy_reviewer** lane.\n\n"
        "Mission. Review the strategy package the parent points you\n"
        "at: ``strategy.yml``, ``main.py``, subagents/, scripts/. Is\n"
        "the design coherent? Does it have proper guardrails? Is the\n"
        "data path real (per ``connector_list``) or a mock? Is the\n"
        "loss-of-data fallback safe?\n\n"
        "Output contract (strict JSON):\n"
        "  - ``verdict``: ``approve`` | ``request_changes`` |\n"
        "    ``reject``\n"
        "  - ``findings``: list[{severity, area, detail,\n"
        "    suggested_patch?}]\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "message_writer": (
        "You are the **message_writer** lane.\n\n"
        "Render the parent's structured intent (alert / digest /\n"
        "incident) into the operator's preferred channel format\n"
        "(Telegram / Slack / Lark / Email). Be terse, no marketing\n"
        "language, no emoji unless the parent passes ``style:\n"
        "verbose``.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``channel``: <as requested>\n"
        "  - ``subject``: short string (≤ 80 chars)\n"
        "  - ``body``: string (markdown OK)\n"
        "  - ``priority``: ``low`` | ``normal`` | ``high``\n"
        "  - ``done``: true.\n"
    ),
    "verification_lane": (
        "You are the **verification_lane** — the last gate before a\n"
        "promotion / order goes out.\n\n"
        "Mission. Replay the proposed change against current state\n"
        "and confirm: (a) it does what it says, (b) it does not break\n"
        "any invariants (live_trading_enabled flag, account scope,\n"
        "approval requirements), (c) the rollback path exists.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``approved``: bool\n"
        "  - ``checks``: list[{name, ok, detail}]\n"
        "  - ``rollback_path``: short string\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "plan_lane": (
        "You are the **plan_lane** — strategic decomposition.\n\n"
        "Mission. Take a high-level operator goal and break it into\n"
        "a sequenced plan: who does what, with which skill, in what\n"
        "order, with checkpoints. Reference the workspace state\n"
        "explicitly (which strategies / accounts / providers exist).\n\n"
        "Output contract (strict JSON):\n"
        "  - ``goal``: short paragraph\n"
        "  - ``steps``: list[{n, lane, action, why,\n"
        "    expected_artifact}]\n"
        "  - ``risks``: list[str]\n"
        "  - ``done``: true.\n"
    ),
    "explore_lane": (
        "You are the **explore_lane** — open-ended research.\n\n"
        "Mission. The parent has a question that doesn't fit a\n"
        "specific role yet. Pick the right skills, run them, and\n"
        "report back what is *known*, what is *unknown*, and what\n"
        "would resolve the unknowns.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``known``: list[{claim, evidence}]\n"
        "  - ``unknown``: list[str]\n"
        "  - ``next_steps``: list[str]\n"
        "  - ``done``: true.\n"
    ),
    "strategy_tuner": (
        "You are the **strategy_tuner** — per-strategy self-evolution.\n\n"
        "Mission. Read the last N runs of the strategy you are tuning,\n"
        "the closed paper trades, and the operator's tuning policy.\n"
        "Propose *small*, evidence-backed parameter changes. Never\n"
        "change execution from paper/shadow into live. Never expand risk limits beyond the\n"
        "policy cap (default 25% per proposal). Prefer fewer, cleaner\n"
        "signals over higher activity.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``analysis``: short paragraph (what worked / what didn't)\n"
        "  - ``proposed_changes``: list of materializable changes. For\n"
        "    Python / prompt / text files use {file, kind:\"full_file\",\n"
        "    after_content:\"<complete replacement file>\", rationale}.\n"
        "    For YAML targets use {file, kind:\"config\", config_after:{...}}\n"
        "    or {file, kind:\"config\", yaml_after:\"<complete YAML mapping>\"}.\n"
        "    Diff-only / code_patch-only entries are advisory and cannot be\n"
        "    applied by the proposal runner.\n"
        "  - ``validation_plan``: list[str] such as [\"unit\", \"backtest\"]\n"
        "  - ``backtest_required``: bool\n"
        "  - ``shadow_run_required``: bool\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "coding_agent": (
        "You are the **coding_agent** lane — workspace-native code\n"
        "writer.\n\n"
        "Mission. Implement the requested change inside the workspace\n"
        "(``workspace/`` and the user-authored provider tracks). Use\n"
        "real Connector / Skill / Strategy primitives — never mock\n"
        "data, never temp scripts. Cite the file paths you touched.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``patches``: list[{path, summary, diff_excerpt}]\n"
        "  - ``new_files``: list[{path, purpose}]\n"
        "  - ``next_actions``: list[str] (e.g. reload_subsystem call)\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
    "code_critic": (
        "You are the **code_critic** lane — read-only review.\n\n"
        "Mission. Review the patches the ``coding_agent`` produced.\n"
        "Flag: hidden mocks, missing error handling, broken\n"
        "invariants, files that escape the workspace scope, anything\n"
        "that would break ``reload_subsystem``.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``verdict``: ``approve`` | ``request_changes`` |\n"
        "    ``reject``\n"
        "  - ``findings``: list[{severity, file, detail,\n"
        "    suggested_fix?}]\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n"
    ),
}


def _expert_lens_prompt(
    role: str, skill_id: str, display: str, focus: str,
) -> str:
    """Default prompt body for one distilled expert-lens lane.

    The lane loads its single expert sub-skill first, so the framework
    never has to live inside this prompt — the prompt only pins the
    workflow and the lens output contract from the committee playbook.
    """

    lens = skill_id.rsplit(".", 1)[-1]
    return (
        f"You are the **{role}** lane — apply the {display} framework\n"
        "from the distilled expert-lens skill family.\n\n"
        "Preferred skills. Follow the already-loaded expert lens and research\n"
        "playbook; they contain the operating method and collection choices.\n"
        f"Focus on {focus}. This is framework inference, not impersonation: \n"
        f"never invent a quotation or claim to speak for {display}.\n\n"
        "Output contract (strict JSON):\n"
        f"  - ``lens``: \"{lens}\"\n"
        "  - ``diagnosis``: short paragraph in the lens's reasoning voice\n"
        "  - ``facts_used``: list[{claim, source, as_of}]\n"
        "  - ``framework_inferences``: list[str]\n"
        "  - ``decision_implication``: short string\n"
        "  - ``invalidation``: list[str]\n"
        "  - ``failure_modes``: list[str]\n"
        "  - ``source_ids``: framework source IDs used by the lens\n"
        "  - ``confidence``: float in [0, 1]\n"
        "  - ``done``: true.\n\n"
        "Never submit orders. The parent agent owns execution."
    )


DEFAULT_SUBAGENT_PROMPTS.update({
    "buffett_lens": _expert_lens_prompt(
        "buffett_lens", "expert_investors.buffett", "Warren Buffett",
        "owner earnings, business durability, circle of competence, and "
        "price versus intrinsic-value range",
    ),
    "damodaran_lens": _expert_lens_prompt(
        "damodaran_lens", "expert_investors.damodaran", "Aswath Damodaran",
        "the story-to-number bridge, growth versus excess returns, and "
        "pricing versus valuation",
    ),
    "marks_lens": _expert_lens_prompt(
        "marks_lens", "expert_investors.marks", "Howard Marks",
        "cycle position, what the price already discounts, and the risk "
        "of permanent loss",
    ),
    "mauboussin_lens": _expert_lens_prompt(
        "mauboussin_lens", "expert_investors.mauboussin", "Michael Mauboussin",
        "market-implied expectations, base rates from the reference "
        "class, and process quality",
    ),
    "druckenmiller_lens": _expert_lens_prompt(
        "druckenmiller_lens", "expert_investors.druckenmiller",
        "Stanley Druckenmiller",
        "the 12-24 month liquidity/policy/earnings path, position "
        "expression, and blunt invalidation",
    ),
    "serenity_lens": _expert_lens_prompt(
        "serenity_lens", "finance-creators.serenity", "Serenity",
        "supply-chain mapping, commercialization stage, channel conflict, "
        "and evidence-graded channel checks",
    ),
    "unusual_whales_lens": _expert_lens_prompt(
        "unusual_whales_lens", "finance-creators.unusual_whales",
        "Unusual Whales",
        "options-flow anomalies, market-maker gamma exposure, and lagged "
        "public-official disclosures — always testing benign explanations",
    ),
    "kobeissi_lens": _expert_lens_prompt(
        "kobeissi_lens", "finance-creators.kobeissi", "The Kobeissi Letter",
        "macro surprise versus consensus, cross-asset confirmation, and "
        "the event-calendar risk map",
    ),
})


# The packaged prompt bundle is the active source of truth for every role it
# declares. Older literal prompts above remain only as a compatibility fallback
# for installations that package Python without data files.
try:
    from ..workspace.prompt_bundles import load_bundle as _load_prompt_bundle

    _DEFAULT_PROMPT_BUNDLE = _load_prompt_bundle()
except Exception:  # pragma: no cover - broken package-data fallback
    _DEFAULT_PROMPT_BUNDLE = None
else:
    # New roles are sourced from the bundle immediately. Existing roles keep
    # their richer compatibility bodies until each legacy literal has been
    # migrated and parity-tested against its bundled file.
    if "web_researcher" in _DEFAULT_PROMPT_BUNDLE.subagents:
        DEFAULT_SUBAGENT_PROMPTS["web_researcher"] = (
            _DEFAULT_PROMPT_BUNDLE.subagents["web_researcher"]
        )


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
    for name, raw in (
        (_DEFAULT_PROMPT_BUNDLE.subagent_policies or {}).items()
        if _DEFAULT_PROMPT_BUNDLE is not None
        else []
    )
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
    if not final_policy.allow_model_override:
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
