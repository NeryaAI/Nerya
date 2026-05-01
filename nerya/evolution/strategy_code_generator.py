"""Generate strategy package proposals.

Plan ref: ``2026-04-28-agent-generated-strategy-runtime-refactor.md`` §5.4 / §11 Phase 4.

The agent calls this module to *propose* a new strategy package; the
generator never mutates ``workspace/strategies/`` directly. It builds:

* ``strategy.md``                — operator-readable playbook.
* ``strategy.yml``               — typed manifest validated by
  :mod:`nerya.strategies.package`.
* ``main.py``                    — entrypoint scaffolded for one of
  three strategy classes (``scalping`` / ``trend`` / ``news``).
* ``subagents/<name>.agent.md``  — strategy-scoped subagent prompts
  (only when the manifest declares them).
* ``tests/test_contract.py``     — minimal contract test that the
  validator+CI re-runs.

Why we ship a typed generator
-----------------------------
The agent could in principle just dump arbitrary Python and YAML. In
practice we want every generated package to:

* parse cleanly through :func:`nerya.strategies.package._parse_manifest`
  (so the runner doesn't have to keep schema migrations forever);
* pass :func:`nerya.strategies.validator.validate_proposal_files`
  (so promotion can be a one-click action for operators);
* read like the templates in plan §9 so reviewers don't have to
  context-switch between three writing styles.

This generator centralises those defaults. The agent can override
any field via :class:`StrategyGenerationRequest`; the generator
fills in conservative defaults for anything missing.

Output shape
------------
The generator returns a :class:`StrategyGenerationResult` with two
parts:

* ``files``     — a flat ``rel_path -> content`` map ready for the
  validator and for ``create_proposal(extra_files={"after/" + rel: c, ...})``.
* ``proposal``  — the ``Proposal`` returned by
  :func:`nerya.evolution.patch_proposal.create_proposal`.

The proposal is created with kind ``strategy_package_proposal``;
:mod:`nerya.evolution.promotion` already understands ``after/<rel>``
files and copies them into the workspace on approval.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

from ..core import yaml_io
from ..core.errors import NeryaError
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from ..strategies.validator import StrategyValidation, validate_proposal_files
from .patch_proposal import Proposal, create_proposal


_LOG = logging.getLogger(__name__)

_VALID_CLASSES: frozenset[str] = frozenset({"scalping", "trend", "news"})
_VALID_MODES: frozenset[str] = frozenset({"paper", "shadow", "live"})
_STRATEGY_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")


# ---------------------------------------------------------------------------
# Request / result types
# ---------------------------------------------------------------------------


@dataclass
class StrategyGenerationRequest:
    """Inputs from the agent.

    The minimum a caller must provide is ``strategy_id``, ``markets``,
    and ``accounts``; everything else has a sensible default keyed
    off ``strategy_class``. The agent is encouraged to pass through
    its raw natural-language prompt as ``prompt`` so it lands inside
    the generated ``strategy.md`` for the operator to review.
    """

    strategy_id: str
    title: str = ""
    description: str = ""
    prompt: str = ""
    strategy_class: str = "scalping"  # scalping | trend | news
    mode: str = "paper"
    markets: tuple[str, ...] = ()
    accounts: tuple[str, ...] = ()
    schedule_cron: str = ""
    schedule_every_seconds: Optional[int] = None
    news_sources: tuple[str, ...] = ()
    subagents: tuple[str, ...] = ()
    policy_overrides: dict[str, Any] = field(default_factory=dict)
    llm_policy_overrides: dict[str, Any] = field(default_factory=dict)
    create_tuning: bool = True
    tuning_prompt: str = ""
    tuning_cron: str = "0 */6 * * *"
    tuning_objectives: tuple[str, ...] = ()
    extra_subagent_prompts: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    """Optional package-rooted file overrides.

    Keys are paths relative to ``strategies/<id>/`` (e.g. ``main.py``,
    ``tests/test_main.py``, ``strategy.md``). Values fully replace the
    default template output for that path. Use this to inject the
    actual strategy logic, custom backtests, or a richer ``strategy.md``
    when the agent has already drafted them — the templates are a
    fallback, not a constraint.
    """


@dataclass
class StrategyGenerationResult:
    """Generator output.

    ``files`` are package-rooted (``strategy.yml``, ``main.py``,
    ``subagents/foo.agent.md`` ...). The proposal copy already
    includes the ``after/strategies/<id>/`` prefix.
    """

    request: StrategyGenerationRequest
    files: dict[str, str]
    proposal: Optional[Proposal] = None
    validation: Optional[StrategyValidation] = None

    def asdict(self) -> dict[str, Any]:
        return {
            "request": asdict(self.request),
            "files": dict(self.files),
            "proposal_id": (self.proposal.id if self.proposal else None),
            "validation": (self.validation.asdict() if self.validation else None),
        }


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class StrategyCodeGenerator:
    """Produce strategy package files + the resulting :class:`Proposal`.

    Stateless on purpose — every call builds files from the request
    and writes a fresh proposal. We don't cache anything between
    calls because the agent regenerates with new prompts on each
    iteration; caching would just risk stale output.
    """

    def __init__(self, paths: WorkspacePaths):
        self.paths = paths

    def generate(
        self,
        request: StrategyGenerationRequest,
        *,
        validate: bool = True,
        create_proposal_record: bool = True,
    ) -> StrategyGenerationResult:
        """Build files for ``request`` and (optionally) write the proposal."""

        self._validate_request(request)
        files = self._build_files(request)

        validation: Optional[StrategyValidation] = None
        if validate:
            validation = validate_proposal_files(
                strategy_id=request.strategy_id, files=files
            )

        proposal: Optional[Proposal] = None
        if create_proposal_record:
            extra_files: dict[str, str] = {}
            for rel, content in files.items():
                # PROMOTION COPIES `after/<rel>` -> `<workspace>/<rel>`.
                # Strategies live under `strategies/<id>/`, so prepend that.
                extra_files[f"after/strategies/{request.strategy_id}/{rel}"] = content
            if validation is not None:
                extra_files["validation_report.json"] = _json_dumps(
                    validation.asdict()
                )
            summary = (
                request.title
                or f"Strategy package proposal for {request.strategy_id}"
            )
            proposal = create_proposal(
                self.paths,
                kind="strategy_package_proposal",
                summary=summary,
                rationale=_rationale(request),
                test_plan=_test_plan(request, validation),
                rollback=_rollback(request),
                target=f"strategies/{request.strategy_id}",
                extra_files=extra_files,
                initial_state="pending_review",
            )

        return StrategyGenerationResult(
            request=request,
            files=files,
            proposal=proposal,
            validation=validation,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_request(req: StrategyGenerationRequest) -> None:
        if not _STRATEGY_ID_RE.match(req.strategy_id or ""):
            raise NeryaError(
                f"strategy_id must match {_STRATEGY_ID_RE.pattern}, got {req.strategy_id!r}"
            )
        if req.strategy_class not in _VALID_CLASSES:
            raise NeryaError(
                f"strategy_class must be one of {sorted(_VALID_CLASSES)!r}, "
                f"got {req.strategy_class!r}"
            )
        if req.mode not in _VALID_MODES:
            raise NeryaError(
                f"mode must be one of {sorted(_VALID_MODES)!r}, got {req.mode!r}"
            )
        if not req.markets:
            raise NeryaError("strategy must declare at least one market")
        if not req.accounts:
            raise NeryaError("strategy must declare at least one account")
        if req.schedule_cron and req.schedule_every_seconds:
            raise NeryaError(
                "set schedule_cron OR schedule_every_seconds, not both"
            )

    def _build_files(self, req: StrategyGenerationRequest) -> dict[str, str]:
        files: dict[str, str] = {}
        manifest = self._render_manifest(req)
        files["strategy.yml"] = yaml_io.dumps(manifest)
        files["strategy.md"] = self._render_playbook(req)
        files["main.py"] = self._render_main(req)
        files["tests/test_contract.py"] = self._render_contract_test(req)
        # The manifest may have *added* subagents on top of req.subagents
        # (e.g. defaulting market_analyst for trend strategies). Honour
        # whatever ended up in the manifest so prompt files line up.
        manifest_subagents = list(manifest.get("subagents") or [])
        for name in manifest_subagents:
            prompt = req.extra_subagent_prompts.get(name) or _default_subagent_prompt(
                name=name,
                strategy_id=req.strategy_id,
                strategy_class=req.strategy_class,
                markets=req.markets,
            )
            files[f"subagents/{name}.agent.md"] = prompt
        if req.create_tuning:
            files["subagents/strategy_tuner.agent.md"] = _tuning_subagent_prompt(req)
        # Apply caller-supplied overrides last so the agent's draft of
        # `main.py` / `tests/test_main.py` / `strategy.md` always wins
        # over the stock template. The defaults above stay in for any
        # path the caller didn't override.
        for rel, content in (req.files or {}).items():
            rel_norm = str(rel).replace("\\", "/").lstrip("/")
            if not rel_norm:
                continue
            # Block escapes — proposal writers must not break out of the
            # strategy package.
            if rel_norm.startswith("../") or "/../" in rel_norm:
                raise NeryaError(
                    f"file path escapes strategy package: {rel!r}"
                )
            files[rel_norm] = str(content)
        return files

    @staticmethod
    def _render_manifest(req: StrategyGenerationRequest) -> dict[str, Any]:
        sched = _build_schedule(req)
        policy = _default_policy(req.strategy_class)
        policy.update(req.policy_overrides or {})
        llm_policy = _default_llm_policy(req.strategy_class)
        llm_policy.update(req.llm_policy_overrides or {})

        # Surface trigger_kinds so the dashboard list view can render
        # the badge ("schedule" / "event") even when the operator has
        # not yet sync'd schedules.yml. The runtime schedule itself
        # still lives under ``schedule:``; this is purely the dashboard
        # taxonomy field.
        kinds: list[str] = ["schedule"]
        if req.news_sources:
            kinds.append("event")

        # subagents are only added when the caller asks for them: a
        # strategy that lists a role *must* actually call
        # ``ctx.subagents.run(...)`` (or ``ctx.team.run(...)``) inside
        # main.py during the tick, otherwise the role just adds noise.
        # The author skill is responsible for deciding which roles the
        # strategy needs and either reusing one from the workspace
        # Agents library or shipping a per-strategy override.
        effective_subagents: list[str] = list(req.subagents)

        manifest: dict[str, Any] = {
            "version": 1,
            "strategy_id": req.strategy_id,
            "title": req.title or req.strategy_id,
            "description": req.description or _default_description(req),
            "mode": req.mode,
            "entrypoint": "main.py:run",
            "markets": list(req.markets),
            "accounts": list(req.accounts),
            "schedule": sched,
            "trigger_kinds": kinds,
            "policy": policy,
            "llm_policy": llm_policy,
            "subagents": effective_subagents,
            "news_sources": list(req.news_sources),
        }
        if req.create_tuning:
            manifest["tuning"] = _tuning_block(req)
        return manifest

    @staticmethod
    def _render_playbook(req: StrategyGenerationRequest) -> str:
        body = req.prompt.strip() or _default_playbook(req)
        head = f"# {req.title or req.strategy_id}\n\n"
        head += f"_class: {req.strategy_class}; mode: {req.mode}; generated: {now_iso()}_\n\n"
        return head + body + "\n"

    @staticmethod
    def _render_main(req: StrategyGenerationRequest) -> str:
        return _MAIN_TEMPLATES[req.strategy_class](req)

    @staticmethod
    def _render_contract_test(req: StrategyGenerationRequest) -> str:
        return _CONTRACT_TEST_TEMPLATE.format(strategy_id=req.strategy_id)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _default_description(req: StrategyGenerationRequest) -> str:
    return (
        f"Auto-generated {req.strategy_class} strategy for "
        f"{', '.join(req.markets) or '<markets>'}; mode={req.mode}."
    )


def _default_playbook(req: StrategyGenerationRequest) -> str:
    base = (
        "## Intent\n\n"
        f"This is an auto-generated {req.strategy_class} strategy targeting "
        f"`{', '.join(req.markets)}`.\n\n"
        "## Policy\n\n"
        "* Strategy may only place orders via `ctx.trading.submit_intent`.\n"
        "* All LLM calls are tier-policed by the runner.\n"
        "* Subagents return recommendations only; the runner submits trades.\n\n"
        "## Operator Notes\n\n"
        "Edit `strategy.yml` to tune risk caps, schedule, or LLM tiers.\n"
        "Edit `main.py` to refine the signal logic.\n"
    )
    return base


def _build_schedule(req: StrategyGenerationRequest) -> dict[str, Any]:
    if req.schedule_every_seconds:
        return {
            "type": "interval",
            "every_seconds": int(req.schedule_every_seconds),
            "enabled": True,
        }
    cron = req.schedule_cron or _default_cron(req.strategy_class)
    return {"type": "cron", "cron": cron, "enabled": True}


def _default_cron(strategy_class: str) -> str:
    return {
        "scalping": "*/1 * * * *",
        "trend": "*/15 * * * *",
        "news": "*/5 * * * *",
    }.get(strategy_class, "*/5 * * * *")


def _default_policy(strategy_class: str) -> dict[str, Any]:
    base = {
        "max_single_order_usd": 100.0,
        "max_daily_notional_usd": 1000.0,
        "max_open_positions": 1,
        "min_confidence": 0.55,
        "allow_direct_order": True,
        "require_subagent_before_order": False,
        "default_order_usd": 50.0,
        "max_run_seconds": 60,
        "max_sdk_calls_per_run": 64,
        "max_subagent_calls_per_run": 4,
    }
    if strategy_class == "trend":
        base["require_subagent_before_order"] = True
        base["min_confidence"] = 0.6
    elif strategy_class == "news":
        base["require_subagent_before_order"] = True
        base["min_confidence"] = 0.65
        base["max_subagent_calls_per_run"] = 6
    return base


def _default_llm_policy(strategy_class: str) -> dict[str, Any]:
    if strategy_class == "scalping":
        return {
            "default_tier": "light",
            "allowed_tiers": ["light"],
            "max_calls_per_run": 2,
        }
    if strategy_class == "trend":
        return {
            "default_tier": "medium",
            "allowed_tiers": ["light", "medium"],
            "max_calls_per_run": 4,
        }
    return {
        "default_tier": "light",
        "allowed_tiers": ["light", "medium"],
        "max_calls_per_run": 8,
    }


def _tuning_block(req: StrategyGenerationRequest) -> dict[str, Any]:
    return {
        "enabled": True,
        "schedule": {"type": "cron", "cron": req.tuning_cron, "enabled": True},
        "lookback": {"runs": 200, "min_closed_trades": 0, "max_age_hours": 168},
        "subagent": {
            "name": "strategy_tuner",
            "prompt_file": "subagents/strategy_tuner.agent.md",
            "tier": "high",
        },
        "objectives": list(req.tuning_objectives) or ["risk_adjusted_return"],
        "guardrails": {
            "max_patch_files": 5,
            "max_position_size_change_pct": 25.0,
            "require_backtest": True,
            "require_shadow_run": False,
            "require_operator_approval": True,
        },
        "proposal_policy": {
            "allowed_targets": ["strategy.yml", "main.py", "subagents/*.agent.md"],
            "forbidden_targets": [
                "accounts/*",
                "limits.yml",
                "secrets/*",
                "live_trading_enabled",
            ],
        },
        "tuning_prompt": req.tuning_prompt or "",
    }


# ----- main.py templates ---------------------------------------------------


def _scalping_template(req: StrategyGenerationRequest) -> str:
    market = req.markets[0]
    return (
        '"""Auto-generated scalping strategy.\n\n'
        f"Strategy id: {req.strategy_id}\n"
        f"Market: {market}\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "from nerya.strategies import StrategyContext, StrategyResult\n\n"
        "\n"
        "def run(ctx: StrategyContext) -> StrategyResult:\n"
        '    """Single tick entry point invoked by StrategyRunner."""\n'
        "    market = ctx.config.markets[0]\n"
        '    candles = ctx.market.candles(market, timeframe="1m", limit=80)\n'
        "    if len(candles) < 10:\n"
        '        return ctx.result.hold(reason="not enough candles")\n'
        "    last = candles[-1]\n"
        "    prev = candles[-2]\n"
        '    momentum = float(last["close"]) - float(prev["close"])\n'
        "    if momentum <= 0:\n"
        '        return ctx.result.hold(reason="no upward momentum")\n'
        "    return ctx.trading.submit_intent(\n"
        "        market=market,\n"
        '        side="buy",\n'
        "        size=ctx.policy.default_order_usd,\n"
        '        size_unit="usd",\n'
        '        order_type="market",\n'
        "        confidence=0.6,\n"
        '        reasoning=f"momentum={momentum:.4f}",\n'
        "    )\n"
    )


def _trend_template(req: StrategyGenerationRequest) -> str:
    market = req.markets[0]
    subagent_call = ""
    if req.subagents:
        sa = req.subagents[0]
        subagent_call = (
            f'    analysis = ctx.subagents.run("{sa}", payload={{"market": market}})\n'
            '    output = analysis.get("output") or {}\n'
            '    rec = output.get("recommendation")\n'
            '    if rec not in {"buy", "sell"}:\n'
            '        return ctx.result.hold(reason=output.get("thesis", "subagent declined"))\n'
            '    confidence = float(output.get("confidence", 0.0) or 0.0)\n'
            '    if confidence < ctx.policy.min_confidence:\n'
            '        return ctx.result.hold(reason="subagent confidence below policy")\n'
            "    return ctx.trading.submit_intent(\n"
            "        market=market,\n"
            "        side=rec,\n"
            "        size=ctx.policy.default_order_usd,\n"
            '        size_unit="usd",\n'
            '        order_type="market",\n'
            "        confidence=confidence,\n"
            '        reasoning=output.get("thesis", ""),\n'
            "    )\n"
        )
    else:
        subagent_call = (
            "    return ctx.trading.submit_intent(\n"
            "        market=market,\n"
            '        side="buy",\n'
            "        size=ctx.policy.default_order_usd,\n"
            '        size_unit="usd",\n'
            '        order_type="market",\n'
            "        confidence=0.6,\n"
            '        reasoning="trend trigger",\n'
            "    )\n"
        )
    return (
        '"""Auto-generated trend-following strategy.\n\n'
        f"Strategy id: {req.strategy_id}\n"
        f"Market: {market}\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "from nerya.strategies import StrategyContext, StrategyResult\n\n"
        "\n"
        "def run(ctx: StrategyContext) -> StrategyResult:\n"
        "    market = ctx.config.markets[0]\n"
        '    features = ctx.market.features(market, timeframe="15m", lookback=200)\n'
        '    if features.get("rows", 0) < 50:\n'
        '        return ctx.result.hold(reason="insufficient history")\n'
        f"{subagent_call}"
    )


def _news_template(req: StrategyGenerationRequest) -> str:
    sources = list(req.news_sources) or ["operator-configured"]
    subagent_line = (
        f'    analysis = ctx.subagents.run("{req.subagents[0]}", payload={{"item": item}})\n'
        if req.subagents else
        "    analysis = {}\n"
    )
    return (
        '"""Auto-generated news-following strategy.\n\n'
        f"Strategy id: {req.strategy_id}\n"
        f"News sources: {sources}\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "from nerya.strategies import StrategyContext, StrategyResult\n\n"
        "\n"
        "def run(ctx: StrategyContext) -> StrategyResult:\n"
        '    items = ctx.news.fetch(since=ctx.state.get("last_seen"))\n'
        "    if not items:\n"
        '        return ctx.result.hold(reason="no news this tick")\n'
        "    items = ctx.dedupe.news(items)\n"
        "    if not items:\n"
        '        return ctx.result.hold(reason="all duplicates")\n'
        "    item = items[0]\n"
        "    cls = ctx.llm.classify(\n"
        '        prompt=item.get("summary") or item.get("title") or "",\n'
        '        labels=["alpha", "noise", "risk"],\n'
        "    )\n"
        '    if cls.get("label") != "alpha":\n'
        '        ctx.messages.send(text=f"news classified as {cls.get(\'label\')}")\n'
        '        ctx.state.set("last_seen", ctx.clock.now_iso())\n'
        '        return ctx.result.ok(reason="non-actionable news")\n'
        f"{subagent_line}"
        '    out = analysis.get("output") if isinstance(analysis, dict) else {}\n'
        "    out = out or {}\n"
        '    rec = out.get("recommendation")\n'
        '    if rec not in {"buy", "sell"}:\n'
        '        ctx.messages.send(text=out.get("summary", "subagent declined"))\n'
        '        ctx.state.set("last_seen", ctx.clock.now_iso())\n'
        '        return ctx.result.ok(reason="subagent declined")\n'
        '    ctx.state.set("last_seen", ctx.clock.now_iso())\n'
        "    return ctx.trading.submit_intent(\n"
        "        market=ctx.config.markets[0],\n"
        "        side=rec,\n"
        "        size=ctx.policy.default_order_usd,\n"
        '        size_unit="usd",\n'
        '        order_type="market",\n'
        '        confidence=float(out.get("confidence", 0.0) or 0.0),\n'
        '        reasoning=out.get("thesis", ""),\n'
        "    )\n"
    )


_MAIN_TEMPLATES = {
    "scalping": _scalping_template,
    "trend": _trend_template,
    "news": _news_template,
}


_CONTRACT_TEST_TEMPLATE = (
    '"""Smoke test that the strategy package imports cleanly.\n\n'
    "The validator already runs this check; the file lives in the\n"
    "package so operators can run ``pytest`` against the package on\n"
    "their own.\n"
    '"""\n\n'
    "from __future__ import annotations\n\n"
    "\n"
    "def test_strategy_imports():\n"
    "    import importlib.util\n"
    "    from pathlib import Path\n"
    "\n"
    '    main_path = Path(__file__).resolve().parent.parent / "main.py"\n'
    '    spec = importlib.util.spec_from_file_location("_smoke_{strategy_id}", main_path)\n'
    "    assert spec is not None and spec.loader is not None\n"
    "    module = importlib.util.module_from_spec(spec)\n"
    "    spec.loader.exec_module(module)\n"
    '    assert callable(getattr(module, "run", None))\n'
)


def _default_subagent_prompt(
    *,
    name: str,
    strategy_id: str,
    strategy_class: str,
    markets: Iterable[str],
) -> str:
    market_list = ", ".join(markets) or "<markets>"
    return (
        f"# {name}\n\n"
        f"Strategy: `{strategy_id}` ({strategy_class}); markets: {market_list}.\n\n"
        "## Role\n\n"
        f"You are the `{name}` subagent. Return a structured trade recommendation;\n"
        "the strategy runner decides whether to execute it.\n\n"
        "## Output schema\n\n"
        "```json\n"
        "{\n"
        '  "recommendation": "buy|sell|hold|reduce|avoid",\n'
        '  "confidence": 0.0,\n'
        '  "time_horizon": "minutes|hours|days",\n'
        f'  "market": "{next(iter(markets), "<market>")}",\n'
        '  "thesis": "...",\n'
        '  "invalidation": "...",\n'
        '  "risk_flags": [],\n'
        '  "evidence": [{ "source": "...", "summary": "..." }]\n'
        "}\n"
        "```\n\n"
        "## Constraints\n\n"
        "* Never place orders.\n"
        "* Cite evidence sources you actually used.\n"
        "* Respect the strategy's `policy.min_confidence`.\n"
    )


def _tuning_subagent_prompt(req: StrategyGenerationRequest) -> str:
    objectives = ", ".join(req.tuning_objectives) or "risk_adjusted_return"
    custom = req.tuning_prompt.strip() or "Focus on improving risk-adjusted return."
    return (
        "# strategy_tuner\n\n"
        f"Strategy: `{req.strategy_id}` ({req.strategy_class}).\n"
        f"Objectives: {objectives}.\n\n"
        "## Role\n\n"
        "You are this strategy's tuning subagent. Review recent runs, propose\n"
        "code/config/prompt changes that improve the objective, and return a\n"
        "structured proposal. **Do not** mutate live files; the runner converts\n"
        "your output into a `PatchProposal`.\n\n"
        "## Operator brief\n\n"
        f"{custom}\n\n"
        "## Output schema\n\n"
        "```json\n"
        "{\n"
        '  "summary": "...",\n'
        '  "evidence": [{ "source": "strategy_runs", "finding": "..." }],\n'
        '  "proposed_changes": [\n'
        '    {"file": "main.py", "kind": "code_patch", "rationale": "..."},\n'
        '    {"file": "strategy.yml", "kind": "config_patch", "rationale": "..."}\n'
        "  ],\n"
        '  "expected_effect": {"return": "neutral_or_better", "drawdown": "lower"},\n'
        '  "validation_plan": ["unit", "fixture_replay", "backtest", "shadow_run"],\n'
        '  "risk_flags": []\n'
        "}\n"
        "```\n"
    )


# ---------------------------------------------------------------------------
# Proposal helpers
# ---------------------------------------------------------------------------


def _rationale(req: StrategyGenerationRequest) -> str:
    parts = [
        f"# Strategy package: {req.strategy_id}",
        "",
        f"Class: **{req.strategy_class}**, mode: **{req.mode}**.",
        f"Markets: {', '.join(req.markets)}.",
        f"Accounts: {', '.join(req.accounts)}.",
    ]
    if req.prompt.strip():
        parts.extend(["", "## Operator prompt", "", req.prompt.strip()])
    return "\n".join(parts) + "\n"


def _test_plan(
    req: StrategyGenerationRequest,
    validation: Optional[StrategyValidation],
) -> str:
    parts = [
        "# Test plan",
        "",
        "1. Validator passes (see `validation_report.json`).",
        "2. Run `nerya strategy validate {sid}` after promotion.".format(
            sid=req.strategy_id
        ),
        "3. Run one paper tick: `nerya strategy run {sid} --dry-run`.".format(
            sid=req.strategy_id
        ),
    ]
    if validation is not None and not validation.ok:
        parts.append("")
        parts.append("## Outstanding blockers")
        for issue in validation.blockers:
            parts.append(f"* `{issue.code}`: {issue.message} ({issue.where})")
    if validation is not None and validation.warnings:
        parts.append("")
        parts.append("## Warnings")
        for issue in validation.warnings:
            parts.append(f"* `{issue.code}`: {issue.message} ({issue.where})")
    return "\n".join(parts) + "\n"


def _rollback(req: StrategyGenerationRequest) -> str:
    return (
        "# Rollback\n\n"
        f"Disable the trading schedule for `{req.strategy_id}` "
        "(`nerya strategy pause`), then remove the strategy package "
        f"directory `workspace/strategies/{req.strategy_id}/` "
        "(or restore the previous version from `versions/`).\n"
    )


def _json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, indent=2, default=str)


__all__ = [
    "StrategyCodeGenerator",
    "StrategyGenerationRequest",
    "StrategyGenerationResult",
]
