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
class SubAgentSpec:
    name: str
    prompt_path: Path
    prompt: str = ""
    allowed_skills: list[str] = field(default_factory=list)
    tier: str = "medium"

    @classmethod
    def load(cls, path: Path, *, name: str | None = None,
             allowed_skills: list[str] | None = None,
             tier: str = "medium") -> "SubAgentSpec":
        n = name or path.stem.replace(".agent", "")
        prompt = path.read_text(encoding="utf-8") if path.exists() else ""
        return cls(name=n, prompt_path=path, prompt=prompt,
                   allowed_skills=allowed_skills or [], tier=tier)


DEFAULT_SUBAGENT_SKILLS = {
    # Research/analyst lanes get ``operator`` + ``script`` so they can
    # write ad-hoc Python (yfinance, requests, ccxt, web3, …) when the
    # built-in market_data / onchain / news_social skills don't cover the
    # ask. ``websearch`` (Apr-27 2026) is added to every research lane
    # so the subagent can pull live evidence from DuckDuckGo before
    # writing a fetcher script — this is the Claude-Code-style
    # "research → write code → run code → cross-check → summarise" loop
    # the operator asked for. The dispatcher's
    # ``SUBAGENT_SKILL_DENYLIST`` still keeps trading_write / wallet /
    # script_runtime out of these lanes.
    #
    # Plan 2026-04-28 §6 — what used to be a blanket ``trading`` allow
    # is split into ``trading_read`` (read-only / planning lanes:
    # ``execution_planner``, ``portfolio_manager``, ``verification_lane``,
    # …) and ``trading_write`` (only the main agent / runner). The
    # dispatcher's denylist now enforces the read-only side, so listing
    # ``trading_read`` here grants planning context without ever giving
    # a subagent the ability to submit an order.
    "market_analyst": ["market_data", "news_social", "websearch",
                       "operator", "script", "trace", "llm"],
    "technical_analyst": [
        "market_data", "markets", "market_data_routing", "market_research",
        "quant_research", "analysis", "websearch", "operator", "script",
        "trace", "llm",
    ],
    "fundamentals_analyst": [
        "market_data", "markets", "market_data_routing", "market_research",
        "research", "research_report", "analysis", "websearch", "operator",
        "script", "trace", "llm",
    ],
    "sentiment_analyst": [
        "news_social", "research", "market_research", "market_data_routing",
        "websearch", "operator", "script", "trace", "llm",
    ],
    "macro_strategist": [
        "research", "market_research", "market_data_routing",
        "research_report", "analysis", "market_data", "websearch",
        "operator", "script", "trace", "llm",
    ],
    "quant_researcher": [
        "quant_research", "analysis", "markets", "market_data",
        "market_data_routing", "strategy_validation", "operator",
        "script", "trace", "llm",
    ],
    "bull_researcher": [
        "market_research", "research_report", "quant_research",
        "portfolio", "risk", "websearch", "llm",
    ],
    "bear_researcher": [
        "market_research", "research_report", "quant_research",
        "portfolio", "risk", "websearch", "llm",
    ],
    "research_manager": [
        "market_research", "research_report", "quant_research",
        "portfolio", "risk", "trading_read", "llm",
    ],
    "research_editor": [
        "research_report", "market_research", "quant_research",
        "analysis", "llm",
    ],
    "risk_critic": ["risk", "portfolio", "websearch", "llm"],
    "execution_planner": ["trading_read", "market_data", "llm"],
    "onchain_watcher": ["onchain", "news_social", "websearch",
                        "operator", "script", "trace", "llm"],
    "news_interpreter": ["news_social", "websearch", "operator",
                         "script", "trace", "llm"],
    "portfolio_manager": ["portfolio", "trading_read", "llm"],
    "portfolio_auditor": ["portfolio", "risk", "market_data",
                          "message", "llm"],
    "strategy_reviewer": ["strategy_review", "websearch", "llm"],
    "message_writer": ["message", "llm"],
    "verification_lane": [
        "strategy", "strategy_review", "portfolio", "risk",
        "market_data", "trading_read", "trace", "message",
        "websearch", "llm",
    ],
    "plan_lane": [
        "strategy", "strategy_review", "portfolio", "risk",
        "market_data", "trading_read", "trace", "websearch", "llm",
    ],
    "explore_lane": [
        "market_data", "news_social", "websearch", "onchain", "portfolio",
        "trading_read", "trace", "operator", "script", "llm",
    ],
    # Plan 2026-04-28 §7 — strategy_tuner is the per-strategy
    # self-evolution lane. It can read every read-only signal but never
    # ships orders or applies its own proposals. Final promotion still
    # routes through the operator's approval flow.
    "strategy_tuner": [
        "strategy", "strategy_review", "portfolio", "risk",
        "market_data", "trading_read", "operator", "script",
        "websearch", "trace", "llm",
    ],
    # Plan 04 P0 §3 — first-class coding lane. ``operator`` exposes
    # file/dir/search/write/patch/terminal + the background process
    # registry; ``script`` provides sandboxed code execution; ``trace``
    # lets the lane self-report what it did. No trading skills are
    # listed so the dispatcher denylist + workspace chroot keeps the
    # lane safe. Plan 04 P1 §2 — review/critic lane that can read but
    # not mutate the repo (no operator.write_file / patch_file / terminal —
    # the runtime enforces this through ``allowed_skills``).
    "coding_agent": ["operator", "script", "websearch", "trace", "llm"],
    "code_critic": ["operator", "trace", "strategy_review", "llm"],
}


# Apr-30 2026 — Cunzi feedback: subagents were running with empty
# ``spec.prompt`` because the workspace had no ``<name>.agent.md``
# files. The model would just get "You are the {name} subagent.\n\n"
# and nothing else, so the role was nominally invoked but had no
# personality / scope / output contract. We now ship default prompt
# bodies for every default role; ``StrategySubAgentRegistry`` falls
# back to them when neither the strategy package nor the workspace
# ships a custom prompt. The operator can always override with
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
        "2. Pull recent candles, news, and any on-chain signals via the\n"
        "   appropriate skills (``market_data``, ``news_social``,\n"
        "   ``onchain``, ``websearch``). Prefer at most one fetch per\n"
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
        "not add claims absent from analyst inputs.\n\n"
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
        "2. Pull current portfolio + recent fills via ``portfolio`` /\n"
        "   ``risk`` skills.\n"
        "3. Stress-test: assume +/- 2 ATR or +/- one historical worst\n"
        "   day; compute drawdown; check open-position cap; flag any\n"
        "   single-venue concentration > 60%.\n\n"
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
        "enable live trading. Never expand risk limits beyond the\n"
        "policy cap (default 25% per proposal). Prefer fewer, cleaner\n"
        "signals over higher activity.\n\n"
        "Output contract (strict JSON):\n"
        "  - ``analysis``: short paragraph (what worked / what didn't)\n"
        "  - ``proposed_patches``: list[{file, operation, summary,\n"
        "    diff}]\n"
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
}


def load_registry(paths: WorkspacePaths) -> dict[str, SubAgentSpec]:
    root = paths.subagents
    out: dict[str, SubAgentSpec] = {}
    if not root.exists():
        return out
    for p in sorted(root.glob("*.agent.md")):
        name = p.stem.replace(".agent", "")
        meta = _load_role_meta(root, name)
        allowed = meta.get("allowed_skills") or DEFAULT_SUBAGENT_SKILLS.get(name, [])
        tier = meta.get("tier") or DEFAULT_TIERS.get(name, "medium")
        out[name] = SubAgentSpec.load(
            p, name=name,
            allowed_skills=list(allowed),
            tier=str(tier),
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
            "prompt_path": str(spec.prompt_path),
            "prompt": spec.prompt,
        }
    if name in DEFAULT_SUBAGENT_SKILLS:
        return {
            "name": name,
            "tier": DEFAULT_TIERS.get(name, "medium"),
            "allowed_skills": list(DEFAULT_SUBAGENT_SKILLS[name]),
            "persistent": False,
            "source": "default",
            "prompt_path": None,
            "prompt": DEFAULT_SUBAGENT_PROMPTS.get(name, ""),
        }
    return None


def save_role(
    paths: WorkspacePaths,
    *,
    name: str,
    prompt: str,
    allowed_skills: Optional[list[str]] = None,
    tier: Optional[str] = None,
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

    final_skills = list(allowed_skills or DEFAULT_SUBAGENT_SKILLS.get(name, []))
    final_tier = str(tier or DEFAULT_TIERS.get(name, "medium"))

    meta = {
        "name": name,
        "tier": final_tier,
        "allowed_skills": final_skills,
    }
    yaml_io.dump(_meta_path(paths, name), meta)

    return {
        "name": name,
        "tier": final_tier,
        "allowed_skills": final_skills,
        "persistent": True,
        "source": "workspace",
        "prompt_path": str(prompt_path),
        "prompt": prompt,
    }


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
    "DEFAULT_SUBAGENT_SKILLS",
    "DEFAULT_TIERS",
    "SubAgentSpec",
    "delete_role",
    "describe_role",
    "list_roles",
    "load_registry",
    "save_role",
]
