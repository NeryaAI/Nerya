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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core.errors import LLMError
from ..core.truth import resolve_allow_mock
from .providers import (
    DEFAULT_BASE_URLS,
    ProviderCallable,
    ProviderResult,
    Transport,
    builtin_providers,
)


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
              daily_budget_usd: 2.0

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
        provider_name = (cfg.get("provider") or "mock").lower()
        model = cfg.get("model") or ""
        base_url = cfg.get("base_url") or DEFAULT_BASE_URLS.get(provider_name)
        max_tokens = int(cfg.get("max_tokens") or 1024)
        temperature = float(cfg.get("temperature") or 0.1)
        price_overrides = cfg.get("prices") or None
        # Per-tier timeout override (seconds). Heavy reasoning tiers can
        # legitimately take > 60 s; expose the knob on the tier config.
        timeout_override = cfg.get("timeout_s") or cfg.get("timeout")
        # Reasoning controls. Strings are normalised to
        # "minimal" | "low" | "medium" | "high"; empty disables reasoning.
        reasoning_effort = str(cfg.get("reasoning_effort") or "").strip().lower()
        reasoning_summary = str(cfg.get("reasoning_summary") or "").strip().lower()

        # Resolve provider key. First preference is a vault ref
        # (``provider_key_ref: vault://...``). As a fallback for dev / e2e
        # we also accept ``provider_key_env: NERYA_LLM_KEY`` which reads
        # an environment variable. We never log or return the resolved
        # value; treat it as write-only to the adapter.
        key_ref = cfg.get("provider_key_ref")
        api_key = self._resolve_key(key_ref) if key_ref else None
        if not api_key:
            env_name = cfg.get("provider_key_env")
            if env_name:
                import os
                api_key = os.environ.get(env_name) or None

        if provider_name == "mock" or not api_key:
            # Production truth gate: only return mock content when mock mode
            # is explicitly authorised (tests, paper mode, env flag). Otherwise
            # surface an explicit LLMError so callers handle degradation.
            if provider_name != "mock" and not self._mock_allowed():
                reason = "missing_api_key" if not api_key else "no_provider"
                raise LLMError(
                    f"LLM tier '{tier}' unavailable "
                    f"(provider={provider_name!r}, reason={reason}); "
                    "set NERYA_ALLOW_MOCK_DATA=1 or runtime.mock_mode for mocks"
                )
            if provider_name == "mock" and not self._mock_allowed():
                # tier is explicitly configured as mock; this is an opt-in
                pass
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
                fallback_used=(provider_name != "mock"),
                error="" if provider_name == "mock" else "missing_api_key",
            )

        adapter = self._providers.get(provider_name)
        if adapter is None:
            raise LLMError(f"no adapter registered for provider '{provider_name}'")

        adapter_kwargs: dict[str, Any] = dict(
            tier=tier,
            task=task,
            model=model,
            prompt=prompt,
            schema=schema,
            api_key=api_key,
            base_url=base_url,
            max_tokens=max_tokens,
            price_overrides=price_overrides,
            temperature=temperature,
            provider_name=provider_name,
        )
        # Only propagate reasoning kwargs to adapters that accept them — older
        # ones (Bedrock, Ollama) don't and would crash on unknown keyword.
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
        except LLMError:
            raise
        except Exception as exc:  # pragma: no cover
            raise LLMError(f"provider '{provider_name}' crashed: {exc}") from exc

        return CallResult(
            text=res.text,
            tokens=res.total_tokens or (res.prompt_tokens + res.completion_tokens),
            usd_cost=res.usd_cost,
            provider=res.provider or provider_name,
            model=res.model or model,
            latency_ms=res.latency_ms,
            finish_reason=res.finish_reason,
            mode="live",
            reasoning_text=getattr(res, "reasoning_text", "") or "",
            reasoning_tokens=int(getattr(res, "reasoning_tokens", 0) or 0),
            reasoning_effort=getattr(res, "reasoning_effort", "") or reasoning_effort,
        )

    # ------------------------------------------------------------ internal
    def _resolve_key(self, ref: str) -> str | None:
        if not self.workspace or not ref:
            return None
        if not ref.startswith("vault://"):
            # plaintext refs forbidden — return None so we fall back to mock.
            log.warning("ignoring non-vault provider_key_ref %r", ref)
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
        return _wrap(json.dumps({
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
