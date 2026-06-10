"""Model router. Routes an LLM call to a provider adapter (openai,
anthropic, deepseek, openrouter, …) using the SecretVault for keys,
with a deterministic mock provider available only as an explicitly
authorised fallback for tests / paper mode.

Real provider keys are resolved at dispatch time and NEVER returned to
the caller. Scripts and the agent context only ever see a ``vault://``
reference.

Truth-gate semantics
--------------------
If the configured provider is missing its API key, or the tier is
wired to the ``mock`` provider, the router does not silently fall back
to mock content. It only returns mock output when mock mode has been
explicitly authorised via ``NERYA_ALLOW_MOCK_DATA=1``,
``runtime.mock_mode`` in ``nerya.yml``, or ``runtime.mock_when_paper``
combined with paper-mode execution. Otherwise the router raises
:class:`nerya.core.errors.LLMError` so the caller can surface the
degradation honestly.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import LLMError
from ..core.truth import resolve_allow_mock
from .provider_catalog import (
    default_base_url as _catalog_default_base_url,
    default_env_keys_for as _catalog_default_env_keys,
    lookup as _catalog_lookup,
    resolve_alias as _catalog_resolve_alias,
)
from .route_candidates import (
    RESOLVED_PROVIDER_KEY,
    expand_tier_route_cfgs,
    split_csv_values,
)
from .providers import (
    DEFAULT_BASE_URLS,
    ProviderCallable,
    ProviderResult,
    Transport,
    builtin_providers,
)


# Map operator-facing effort labels to the wire-format the adapters expect.
# * ``extra_high`` → ``xhigh`` (OpenAI Responses/Chat reasoning_effort + Anthropic adaptive).
# * ``none``       → empty string so the adapter skips reasoning entirely.
# Other levels pass through unchanged.
_REASONING_EFFORT_WIRE_MAP: dict[str, str] = {
    "none": "",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra_high": "xhigh",
    "xhigh": "xhigh",
    "max": "xhigh",
}


def _normalise_reasoning_effort(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip().lower().replace("-", "_")
    if not s:
        return ""
    return _REASONING_EFFORT_WIRE_MAP.get(s, s)


log = logging.getLogger(__name__)


@dataclass
class CallResult:
    text: str
    tokens: int
    usd_cost: float
    provider: str = "mock"
    model: str = ""
    latency_ms: int = 0
    finish_reason: str = ""
    # Truth-gate fields: every LLM result must declare its mode.
    mode: str = "live"          # one of: live, mock, degraded, unavailable
    degraded: bool = False
    fallback_used: bool = False
    error: str = ""
    # Reasoning surfaces. ``reasoning_text`` is the model's
    # exposed chain-of-thought summary (NEVER fed back into prompts);
    # ``reasoning_tokens`` and ``reasoning_effort`` are journaled for
    # the operator dashboard / cost-attribution.
    reasoning_text: str = ""
    reasoning_tokens: int = 0
    reasoning_effort: str = ""


class ModelRouter:
    """Tier-aware dispatch.

    Tier config shape (from nerya.yml):
        llm:
          default_tier: medium
          tiers:
            light:
              provider: openai
              model: gpt-4o-mini
              provider_key_ref: vault://openai_key
              base_url: https://api.openai.com/v1   # optional override
              max_tokens: 1024
              temperature: 0.1
              prices: {prompt_per_1k: 0.00015, completion_per_1k: 0.0006}
              allowed_tasks: [classify, extract_json, ...]

    If no provider / no key / or provider = ``"mock"``, the router only
    returns mock content when mock mode is explicitly authorised
    (``NERYA_ALLOW_MOCK_DATA=1``, ``runtime.mock_mode`` in ``nerya.yml``,
    or ``runtime.mock_when_paper`` + paper execution). Otherwise it
    raises :class:`nerya.core.errors.LLMError` so the caller can record
    an honest ``mode="unavailable"`` degradation instead of silently
    presenting mock output as live evidence.
    """

    def __init__(
        self,
        tiers: dict[str, dict[str, Any]],
        workspace: Path | None = None,
        *,
        providers: dict[str, ProviderCallable] | None = None,
        transport: Transport | None = None,
        allow_mock: bool | None = None,
        config_like: Any | None = None,
    ):
        self.tiers = tiers
        self.workspace = workspace
        self._providers: dict[str, ProviderCallable] = providers or builtin_providers(transport)
        self._allow_mock = allow_mock
        self._config_like = config_like

    def _mock_allowed(self) -> bool:
        return resolve_allow_mock(self._allow_mock, self._config_like)

    # ------------------------------------------------------------ public
    def register_provider(self, name: str, adapter: ProviderCallable) -> None:
        self._providers[name.lower()] = adapter

    def dispatch(
        self,
        *,
        tier: str,
        task: str,
        prompt: str,
        schema: dict | None = None,
        caller: str | None = None,
    ) -> CallResult:
        cfg = self.tiers.get(tier) or {}
        max_tokens = int(cfg.get("max_tokens") or 1024)
        temperature = float(cfg.get("temperature") or 0.1)
        price_overrides = cfg.get("prices") or None
        # Per-tier timeout override (seconds). Heavy reasoning tiers can
        # legitimately take > 60 s; expose the knob on the tier config.
        timeout_override = cfg.get("timeout_s") or cfg.get("timeout")
        # Reasoning controls. Operator-facing levels normalise via the
        # catalogue (none / minimal / low / medium / high / extra_high)
        # to the wire-format each adapter expects.
        reasoning_effort = _normalise_reasoning_effort(cfg.get("reasoning_effort"))
        reasoning_summary = str(cfg.get("reasoning_summary") or "").strip().lower()

        routes = expand_tier_route_cfgs(
            cfg,
            keys_for_route=lambda route: self._resolve_route_api_keys(route),
            model_override=None,
        )
        last_error: LLMError | None = None
        for route_index, route_cfg in enumerate(routes):
            raw_route_provider = (route_cfg.get("provider") or "mock").lower()
            route_provider = _catalog_resolve_alias(raw_route_provider) or raw_route_provider
            route_keys = self._resolve_route_api_keys(route_cfg)
            route_catalog_entry = _catalog_lookup(route_provider)
            route_key_optional = bool(
                route_catalog_entry and route_catalog_entry.extra.get("key_optional")
            )
            if route_provider != "mock" and not route_keys and not route_key_optional:
                last_error = LLMError(
                    f"LLM tier '{tier}' route unavailable "
                    f"(provider={route_provider!r}, reason=missing_api_key)"
                )
                if route_index < len(routes) - 1:
                    continue
                raise last_error
            if route_provider == "mock":
                res = _mock_call(tier=tier, task=task, prompt=prompt, schema=schema)
                return CallResult(
                    text=res.text,
                    tokens=res.total_tokens,
                    usd_cost=res.usd_cost,
                    provider="mock",
                    model="mock",
                    latency_ms=0,
                    finish_reason="mock",
                    mode="mock",
                    degraded=False,
                    fallback_used=route_index > 0,
                    error="",
                )
            adapter = self._providers.get(route_provider)
            if adapter is None:
                route_kind = str(route_cfg.get("kind") or "").strip().lower()
                route_api_mode = (
                    route_kind
                    or (route_catalog_entry.api_mode if route_catalog_entry else "")
                )
                if route_api_mode == "chat_completions" or route_cfg.get("base_url"):
                    adapter = self._providers.get("compat")
                elif route_api_mode == "anthropic_messages":
                    adapter = (
                        self._providers.get("anthropic-compat")
                        or self._providers.get("anthropic")
                    )
            if adapter is None:
                last_error = LLMError(f"no adapter registered for provider '{route_provider}'")
                if route_index < len(routes) - 1:
                    continue
                raise last_error
            route_base_url = (
                route_cfg.get("base_url")
                or _catalog_default_base_url(route_provider)
                or DEFAULT_BASE_URLS.get(route_provider)
            )
            model = str(route_cfg.get("model") or "")
            api_key = str(route_cfg.get(RESOLVED_PROVIDER_KEY) or "")
            adapter_kwargs: dict[str, Any] = dict(
                tier=tier,
                task=task,
                model=model,
                prompt=prompt,
                schema=schema,
                api_key=api_key,
                base_url=route_base_url,
                max_tokens=int(route_cfg.get("max_tokens") or max_tokens),
                price_overrides=route_cfg.get("prices") or price_overrides,
                temperature=float(route_cfg.get("temperature") or temperature),
                provider_name=route_provider,
            )
            # Only propagate reasoning kwargs to adapters that accept them.
            try:
                import inspect
                sig = inspect.signature(adapter.__call__) if callable(adapter) else None
            except (TypeError, ValueError):
                sig = None
            if sig is not None:
                params = sig.parameters
                if timeout_override is not None and "timeout" in params:
                    adapter_kwargs["timeout"] = float(timeout_override)
                if reasoning_effort and "reasoning_effort" in params:
                    adapter_kwargs["reasoning_effort"] = reasoning_effort
                if reasoning_summary and "reasoning_summary" in params:
                    adapter_kwargs["reasoning_summary"] = reasoning_summary
            try:
                res: ProviderResult = adapter(**adapter_kwargs)
            except LLMError as exc:
                last_error = exc
                if route_index < len(routes) - 1:
                    continue
                raise
            except Exception as exc:  # pragma: no cover
                crash = LLMError(f"provider '{route_provider}' crashed: {exc}")
                if route_index < len(routes) - 1:
                    last_error = crash
                    continue
                raise crash from exc

            return CallResult(
                text=res.text,
                tokens=res.total_tokens or (res.prompt_tokens + res.completion_tokens),
                usd_cost=res.usd_cost,
                provider=res.provider or route_provider,
                model=res.model or model,
                latency_ms=res.latency_ms,
                finish_reason=res.finish_reason,
                mode="live",
                fallback_used=route_index > 0,
                reasoning_text=getattr(res, "reasoning_text", "") or "",
                reasoning_tokens=int(getattr(res, "reasoning_tokens", 0) or 0),
                reasoning_effort=(
                    getattr(res, "reasoning_effort", "") or reasoning_effort
                ),
            )
        if last_error is not None:
            raise last_error
        raise LLMError(f"LLM tier '{tier}' has no route candidates")

    # ------------------------------------------------------------ internal
    def _resolve_api_keys(
        self,
        cfg: dict[str, Any],
        *,
        provider_name: str,
    ) -> list[str]:
        keys: list[str] = []

        def add_values(value: Any) -> None:
            for key in split_csv_values(value):
                if key not in keys:
                    keys.append(key)

        for ref in split_csv_values(cfg.get("provider_key_ref")):
            add_values(self._resolve_key(ref))

        env_names = split_csv_values(cfg.get("provider_key_env"))
        if env_names:
            import os
            for env_name in env_names:
                add_values(os.environ.get(env_name) or "")

        # Catalog-driven env-var fallback (e.g. ``ANTHROPIC_API_KEY`` →
        # ``ANTHROPIC_TOKEN`` → ``CLAUDE_CODE_OAUTH_TOKEN`` for the
        # ``anthropic`` provider, ``CLAUDE_CODE_OAUTH_TOKEN`` for
        # ``claude-code``). Tier-level overrides above still win.
        if not keys:
            import os
            for env_name in _catalog_default_env_keys(provider_name):
                add_values(os.environ.get(env_name) or "")
                if keys:
                    break
        return keys

    def _resolve_route_api_keys(self, cfg: dict[str, Any]) -> list[str]:
        provider_name = _catalog_resolve_alias(
            str(cfg.get("provider") or "mock").strip().lower()
        ) or str(cfg.get("provider") or "mock").strip().lower()
        api_keys = self._resolve_api_keys(cfg, provider_name=provider_name)
        if not api_keys and self._config_like is not None:
            try:
                from .oauth_login import OAUTH_PROVIDERS, resolve_oauth_token
                if provider_name in OAUTH_PROVIDERS:
                    api_keys = split_csv_values(
                        resolve_oauth_token(
                            self._config_like, provider=provider_name,
                        )
                    )
            except Exception:
                pass
        return api_keys

    def _resolve_key(self, ref: str) -> str | None:
        if not self.workspace or not ref:
            return None
        if not ref.startswith("vault://"):
            # plaintext refs forbidden — return None so we fall back to mock.
            log.warning("ignoring non-vault provider_key_ref")
            return None
        try:
            from ..security.secrets import SecretVault
            vault_path = self.workspace / "vault" / "secrets.enc"
            if not vault_path.exists():
                return None
            vault = SecretVault.open(vault_path)
            name = ref.split("vault://", 1)[-1]
            return vault.resolve(name, required_scope="llm")
        except Exception:
            return None


# ---------------------------------------------------------------- mock
_MOCK_HINT_RE = re.compile(
    r"<<MOCK_DECISION:(\{.*?\})>>", re.DOTALL,
)


def _extract_action_hint(prompt: str) -> dict | None:
    """Return a parsed JSON hint from a ``<<MOCK_DECISION:{...}>>`` marker."""
    m = _MOCK_HINT_RE.search(prompt)
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _mock_call(*, tier: str, task: str, prompt: str, schema: dict | None) -> ProviderResult:
    """Deterministic mock responses for every task Nerya actually uses.

    These mirror schema-valid structures so tests don't need a live model.
    """
    low = prompt.lower()
    tokens = max(1, min(4000, len(prompt.split())))
    usd = tokens * {"light": 0.00002, "medium": 0.00005, "high": 0.00020}.get(tier, 0.00005)

    def _wrap(text: str) -> ProviderResult:
        return ProviderResult(
            text=text,
            prompt_tokens=tokens,
            completion_tokens=max(1, tokens // 4),
            total_tokens=tokens + max(1, tokens // 4),
            usd_cost=float(usd),
            model="mock",
            provider="mock",
            latency_ms=0,
            finish_reason="stop",
        )

    if task == "classify":
        label = (
            "alpha" if any(w in low for w in ("surge", "breakout", "rally", "pump")) else
            "risk" if any(w in low for w in ("crash", "hack", "ban")) else
            "noise"
        )
        return _wrap(json.dumps({"label": label, "confidence": 0.7}))

    if task == "news_filtering":
        keep = any(w in low for w in ("etf", "hack", "listing", "rate cut", "fomc"))
        return _wrap(json.dumps({"keep": keep, "reason": "keyword"}))

    if task == "trigger_triage":
        return _wrap(json.dumps({"priority": "normal"}))

    if task == "auto_session_title":
        title = "Nerya session"
        for marker in ("User:", "user:", "Message:", "message:"):
            if marker in prompt:
                candidate = prompt.split(marker, 1)[1].strip().splitlines()[0]
                candidate = re.sub(r"\s+", " ", candidate).strip(" -#`\"'")
                if candidate:
                    title = candidate[:48].rstrip()
                    break
        return _wrap(json.dumps({"title": title or "Nerya session"}))

    if task == "compress":
        head = prompt[:400]
        return _wrap(head)

    if task == "subagent_analysis":
        if "risk" in low:
            signal = "neutral"
            risks = [
                {"summary": "macro event risk can invalidate a BTC long-cycle entry"},
                {"summary": "stale data or wide spread should block execution"},
            ]
        elif "sentiment" in low or "news" in low:
            signal = "neutral"
            risks = [{"summary": "headline risk remains unresolved"}]
        elif "technical" in low or "breakout" in low:
            signal = "bullish"
            risks = [{"summary": "breakout failure below support invalidates the signal"}]
        else:
            signal = "neutral"
            risks = [{"summary": "confidence is not high enough for live execution"}]
        return _wrap(json.dumps({
            "summary": (
                "Mock structured research memo for Agent Team validation. "
                "BTC view is based on the assignment payload and remains paper-only."
            ),
            "signal": signal,
            "confidence": 0.68,
            "evidence": [
                {
                    "summary": "BTC demo research considered technical, sentiment, and risk inputs.",
                    "source": "mock_subagent_payload",
                },
                {
                    "summary": "No live order is allowed; output is decision input only.",
                    "source": "nerya_demo_policy",
                },
            ],
            "risks": risks,
            "output": {
                "rating": "Hold" if signal == "neutral" else "Overweight",
                "data_freshness": "demo_offline",
                "key_risks": [r["summary"] for r in risks],
            },
            "done": True,
            "bias": "bullish" if "breakout" in low else "neutral",
            "strength": 0.6,
            "support": 78000,
            "resistance": 82000,
            "notes": ("mock market_analyst — breakout detected"
                       if "breakout" in low else "mock neutral"),
        }))

    if task == "normal_agent_loop":
        # Test hook: callers can steer the mock by inlining a marker in the
        # prompt. This lets unit tests drive the kernel through specific action
        # branches without patching providers.
        hint = _extract_action_hint(prompt)
        if hint is not None:
            return _wrap(json.dumps(hint))
        return _wrap(json.dumps({
            "action": "submit_trade_intent",
            "reasoning": "Breakout + volume confirm. Small position.",
            "intent": {
                "side": "buy",
                "market": "PAPER:BTCUSDT",
                "size_usd": 1000,
                "order_type": "market",
                "confidence": 0.62,
            },
        }))

    if task in ("analyze_signal", "complex_signal_analysis"):
        return _wrap(json.dumps({
            "signal": "alpha",
            "score": 0.7,
            "rationale": "mock high-tier analysis",
            "recommended_action": "wake main agent",
        }))

    if task in ("strategy_review", "trade_explanation"):
        return _wrap(json.dumps({
            "verdict": "ok",
            "strengths": ["followed plan", "within limits"],
            "weaknesses": [],
            "learning": "keep size conservative on first breakout leg",
        }))

    if task in ("script_generation", "skill_generation", "strategy_evolution"):
        return _wrap(json.dumps({
            "proposal_kind": task,
            "rationale": "mock proposal",
            "body": "# mock\n",
        }))

    if task == "large_loss_postmortem":
        return _wrap(json.dumps({
            "root_cause": "slippage + regime shift",
            "remediation": "tighten stop_loss_pct",
        }))

    return _wrap("{}")


__all__ = ["CallResult", "ModelRouter"]
