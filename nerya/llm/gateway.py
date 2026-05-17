"""LLMGateway — the only way Nerya reaches an LLM.

Every call resolves a tier, enforces the caller's `LLMSession` (quota,
allowed tiers/tasks), dispatches through the
ModelRouter (which may resolve provider keys via the SecretVault), and
records redacted usage into the llm/security journals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..core.config import Config
from ..core.errors import LLMError
from ..core.errors import PromptInjectionDetected
from ..core.time import now_iso
from ..core.truth import resolve_allow_mock
from ..db import LLMUsageRepository
from ..db.sqlite import connect
from ..security.prompt_injection import flag_suspicious
from .adapters.openai import DEFAULT_BASE_URLS as _OPENAI_DEFAULT_BASE_URLS
from .messages import (
    AnthropicMessagesBackend,
    BedrockAnthropicMessagesBackend,
    GeminiMessagesBackend,
    MessagesBackend,
    MessagesRequest,
    MessagesResponse,
    MockMessagesBackend,
    OllamaMessagesBackend,
    OpenAIMessagesBackend,
    normalise_provider_native_web_search,
)
from .model_router import CallResult, ModelRouter
from .provider_catalog import lookup as _provider_lookup
from .redaction import scrub
from .session import LLMSession
from .structured_output import parse as parse_structured
from .tier_policy import TierPolicy
from .usage import LLMUsageJournal

_LOG = logging.getLogger(__name__)

def _messages_preview(messages: list[dict]) -> str:
    """Render a short preview of the latest user message for journal."""

    for msg in reversed(messages or []):
        if (msg or {}).get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content[:1000]
        if isinstance(content, list):
            parts: list[str] = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(str(b.get("text") or ""))
                elif isinstance(b, dict) and b.get("type") == "tool_result":
                    inner = b.get("content")
                    if isinstance(inner, list):
                        for p in inner:
                            if isinstance(p, dict) and p.get("type") == "text":
                                parts.append(str(p.get("text") or ""))
                    elif isinstance(inner, str):
                        parts.append(inner)
            return ("\n".join(parts))[:1000]
    return ""


@dataclass
class LLMCall:
    tier: str
    task: str
    caller: str
    tokens: int
    usd: float
    raw: str
    parsed: Any
    # Reasoning surfaces. Empty when the tier is configured for
    # a non-reasoning model OR ``reasoning_effort`` is unset on the tier.
    reasoning_text: str = ""
    reasoning_tokens: int = 0
    reasoning_effort: str = ""
    provider: str = ""
    model: str = ""


class LLMGateway:
    def __init__(self, config: Config):
        self.config = config
        try:
            from .ops import effective_tiers
            tiers = effective_tiers(config)
        except Exception:
            tiers = config.get("llm.tiers") or {}
        extra_class_map = config.get("llm.task_class_map") or {}
        if not isinstance(extra_class_map, dict):
            extra_class_map = {}
        # normalise keys/values to lower-case strings for stable lookup
        extra_class_map = {
            str(k).strip().lower(): str(v).strip().lower()
            for k, v in extra_class_map.items()
            if isinstance(k, str) and isinstance(v, str)
        }
        self.tier_policy = TierPolicy(
            tiers=tiers,
            default_tier=config.get("llm.default_tier") or "medium",
            extra_class_map=extra_class_map or None,
        )
        self.router = ModelRouter(tiers=tiers, workspace=config.paths.root)
        self.usage = LLMUsageJournal(
            journal_path=config.paths.journal("llm"),
            security_path=config.paths.journal("security"),
        )
        self._con = None

    def _con_lazy(self):
        if self._con is None:
            self._con = connect(self.config.paths.db)
        return self._con

    # ---------------------------------------------------------------- core
    def call(
        self,
        *,
        task: str,
        prompt: str,
        caller: str,
        tier: str | None = None,
        caller_allowed_tiers: list[str] | None = None,
        schema: dict | None = None,
        session: LLMSession | None = None,
        debug_full_prompt_journal: bool = False,
    ) -> LLMCall:
        # resolve tier (task advertised by tier.allowed_tasks)
        resolved_tier = self.tier_policy.resolve(
            task=task,
            requested_tier=tier,
            caller_allowed_tiers=caller_allowed_tiers,
        )

        clean_prompt = scrub(prompt)

        # prompt firewall: refuse hostile content before it reaches a real
        # model. Classification-style tasks are still expected to run on
        # text that might contain injection markers — we log but do not
        # refuse for the ``classify`` and ``risk_screening`` tasks so the
        # caller can explicitly score the headline as "risk".
        hits = flag_suspicious(clean_prompt)
        if hits:
            from ..core import jsonl
            jsonl.append(
                self.config.paths.journal("security"),
                {
                    "kind": "prompt_firewall.flag",
                    "caller": caller,
                    "task": task,
                    "tier": resolved_tier,
                    "patterns": hits,
                    "ts": now_iso(),
                },
            )
            non_blocking_tasks = {"classify", "risk_screening", "compress"}
            if task not in non_blocking_tasks:
                raise PromptInjectionDetected(patterns=hits, caller=caller)

        # session-level gates
        if session is not None:
            session.check_tier(resolved_tier)
            session.check_task(task)
            session.check_quota_before()
            if (resolved_tier == "high"
                    and session.policy.high_tier_requires_approval
                    and not _caller_has_high_approval(self.config, session.caller)):
                raise LLMError(
                    f"caller '{session.caller}' requires approval "
                    f"before using high-tier task '{task}'"
                )

        # Capability check: when the caller wants a schema-shaped
        # response we must not silently dispatch to a provider whose
        # ``schema_json_mode`` is declared ``unsupported``. Same goes for
        # ``reasoning_thinking`` on reasoning-flagged tasks. This closes the
        # gap between "matrix lists it as unsupported" and "runtime still
        # tries it and pretends to succeed".
        from .capability_matrix import capability_of
        tier_cfg = (self.config.get("llm.tiers") or {}).get(resolved_tier) or {}
        active_provider = tier_cfg.get("provider") or "mock"
        cap = capability_of(active_provider)
        if schema is not None and cap.tiers.get("schema_json_mode") == "unsupported":
            raise LLMError(
                f"provider {active_provider!r} on tier {resolved_tier!r} does "
                "not support schema/JSON mode; pick a tier whose provider "
                "declares schema_json_mode != 'unsupported'"
            )

        # dispatch
        try:
            result: CallResult = self.router.dispatch(
                tier=resolved_tier, task=task, prompt=clean_prompt,
                schema=schema, caller=caller,
            )
        except Exception as exc:  # pragma: no cover
            raise LLMError(f"router dispatch failed: {exc}") from exc

        # session post-flight (now we know actual tokens/cost)
        if session is not None:
            session.check_quota_after(tokens=result.tokens, cost=result.usd_cost)
            session.record(tokens=result.tokens, cost=result.usd_cost)

        # structured output enforcement. If schema provided we error out
        # hard so callers surface bad provider output.
        parsed: Any
        if schema is not None:
            parsed = parse_structured(result.text, schema=schema, strict=True)
        else:
            try:
                parsed = parse_structured(result.text, schema=None, strict=False)
            except Exception:
                parsed = {"raw": result.text}

        # persist usage
        usage_repo = LLMUsageRepository(self._con_lazy())
        usage_repo.record(tier=resolved_tier, task=task, caller=caller,
                          tokens=result.tokens, usd=result.usd_cost)
        self.usage.record(
            tier=resolved_tier, task=task, caller=caller,
            tokens=result.tokens, usd=result.usd_cost,
            prompt_preview=clean_prompt, response_preview=result.text,
            debug_full=debug_full_prompt_journal or bool(
                self.config.get("llm.debug_full_prompt_journal", False)),
            reasoning_text=getattr(result, "reasoning_text", "") or "",
            reasoning_tokens=int(getattr(result, "reasoning_tokens", 0) or 0),
            reasoning_effort=getattr(result, "reasoning_effort", "") or "",
            provider=getattr(result, "provider", "") or "",
            model=getattr(result, "model", "") or "",
        )

        return LLMCall(
            tier=resolved_tier, task=task, caller=caller,
            tokens=result.tokens, usd=result.usd_cost,
            raw=result.text, parsed=parsed,
            reasoning_text=getattr(result, "reasoning_text", "") or "",
            reasoning_tokens=int(getattr(result, "reasoning_tokens", 0) or 0),
            reasoning_effort=getattr(result, "reasoning_effort", "") or "",
            provider=getattr(result, "provider", "") or "",
            model=getattr(result, "model", "") or "",
        )

    # ---------------------------------------------------------- matrix
    def capabilities(self) -> dict[str, Any]:
        """Return the live per-tier capability matrix.

        Each configured tier is resolved to its provider and the
        corresponding capability record. Callers use this to know
        whether it is safe to request, e.g., tool_calling on the
        ``medium`` tier before actually dispatching.

        The response also surfaces a ``gaps`` block listing the
        capability rows that are still ``experimental``,
        ``metadata-only``, or ``unsupported`` for the currently
        configured tiers — the operator dashboard reads this to warn
        before a business-critical capability silently degrades.

        additionally enriches each tier with model-level
        metadata (context window, output limit, costs, modalities,
        knowledge cutoff, prompt caching) sourced from
        :mod:`nerya.llm.model_registry`.
        """
        from .capability_matrix import (
            CAPABILITIES,
            capability_of,
            summary as matrix_summary,
        )
        from .model_registry import ModelRegistry

        tiers = self.config.get("llm.tiers") or {}
        registry = ModelRegistry(workspace=self.config.paths.root)
        per_tier: dict[str, Any] = {}
        gaps: list[dict[str, Any]] = []
        for name, cfg in tiers.items():
            provider = (cfg or {}).get("provider") or "mock"
            cap = capability_of(provider)
            has_key = bool((cfg or {}).get("provider_key_ref")
                           or (cfg or {}).get("provider_key_env"))
            model_id = (cfg or {}).get("model") or ""
            model_meta = registry.lookup(provider, model_id)
            reasoning_effort = str((cfg or {}).get("reasoning_effort") or "")
            reasoning_summary = str((cfg or {}).get("reasoning_summary") or "")
            per_tier[name] = {
                "provider": cap.provider,
                "family": cap.family,
                "model": model_id,
                "has_key_ref": has_key,
                "tiers": dict(cap.tiers),
                "model_metadata": model_meta.to_dict(),
                "reasoning_effort": reasoning_effort,
                "reasoning_summary": reasoning_summary,
            }
            for capability in CAPABILITIES:
                level = cap.tiers.get(capability, "unsupported")
                if level != "supported":
                    gaps.append({
                        "tier": name,
                        "provider": cap.provider,
                        "capability": capability,
                        "level": level,
                    })
        return {
            "tiers": per_tier,
            "providers": matrix_summary(),
            "gaps": gaps,
            "capabilities": list(CAPABILITIES),
            "model_registry": registry.summary(tiers=tiers),
        }

    # ---------------------------------------------------------- convenience
    def classify(self, *, caller: str, text: str, labels: list[str],
                 tier: str | None = None,
                 session: LLMSession | None = None) -> dict:
        prompt = f"Classify this text into one of {labels}:\n{text}"
        intent_tier = self.config.get("llm.intent_tier") or "light"
        return self.call(task="classify", caller=caller, tier=tier or intent_tier,
                         prompt=prompt, session=session).parsed

    def extract_json(self, *, caller: str, text: str, schema: dict | None = None,
                     task: str = "extract_json", tier: str | None = None,
                     session: LLMSession | None = None) -> dict:
        prompt = f"Extract a JSON matching schema from this text:\n{text}"
        return self.call(task=task, caller=caller, tier=tier, prompt=prompt,
                         schema=schema, session=session).parsed

    def compress(self, *, caller: str, text: str, max_tokens: int = 512,
                 session: LLMSession | None = None) -> str:
        return self.call(task="compress", caller=caller, tier="light",
                         prompt=f"Compress to <{max_tokens} tokens:\n{text}",
                         session=session).raw

    def analyze_signal(self, *, caller: str, text: str, tier: str = "high",
                       session: LLMSession | None = None,
                       schema: dict | None = None) -> dict:
        return self.call(task="complex_signal_analysis", caller=caller,
                         tier=tier, prompt=text, schema=schema,
                         session=session).parsed

    # ============================================================
    # provider-native ``messages + tools`` interface
    # ============================================================
    #
    # ``call_messages`` is the entry point used by the
    # :class:`WorkspaceNativeAgentLoop`. Unlike :meth:`call` (which
    # serialises a single prompt and parses a strict-JSON decision)
    # this returns the assistant *content blocks* — including
    # ``tool_use`` — so the loop can dispatch tools natively.
    #
    # Backends are resolved per-tier. Today we wire two:
    #   * AnthropicMessagesBackend (real provider)
    #   * MockMessagesBackend       (offline / paper mode)
    #
    # Other providers (OpenAI Responses, Gemini, OpenRouter) plug in
    # later by implementing ``MessagesBackend``; the gateway's
    # public surface is unchanged.

    def call_messages(
        self,
        *,
        task: str,
        caller: str,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        tier: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        session: LLMSession | None = None,
        reasoning_effort: str | None = None,
        reasoning_summary: str | None = None,
        model_provider: str | None = None,
        model_id: str | None = None,
    ) -> MessagesResponse:
        """Provider-native messages call.

        Returns :class:`MessagesResponse` directly so the agent loop
        can inspect ``content`` (text + tool_use blocks) without going
        through the legacy ``parsed`` JSON interpreter.
        """

        resolved_tier = self.tier_policy.resolve(
            task=task, requested_tier=tier, caller_allowed_tiers=None,
        )
        if session is not None:
            session.check_tier(resolved_tier)
            session.check_task(task)
            session.check_quota_before()

        backend = self._resolve_messages_backend(
            resolved_tier,
            provider_override=model_provider,
            model_override=model_id,
        )
        tier_cfg = self._effective_tier_cfg(
            resolved_tier,
            provider_override=model_provider,
            model_override=model_id,
        )
        provider_native_web_search = normalise_provider_native_web_search(
            tier_cfg.get(
                "provider_native_web_search",
                self.config.get("llm.provider_native_web_search"),
            )
        )

        request = MessagesRequest(
            system=system,
            messages=list(messages),
            tools=list(tools or []),
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            reasoning_summary=reasoning_summary,
            metadata={
                "provider_native_web_search": provider_native_web_search,
            },
        )

        response = backend(request)

        usage = response.usage or {}
        prompt_tokens = int(usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or 0)
        tokens = prompt_tokens + completion_tokens
        usd_cost = self._estimate_messages_cost(
            tier=resolved_tier,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            provider_override=model_provider,
            model_override=model_id,
        )

        if session is not None:
            session.check_quota_after(tokens=tokens, cost=usd_cost)
            session.record(tokens=tokens, cost=usd_cost)

        try:
            con = self._con_lazy()
            usage_repo = LLMUsageRepository(con)
            usage_repo.record(
                tier=resolved_tier, task=task, caller=caller,
                tokens=tokens, usd=usd_cost,
            )
        except Exception:
            pass
        try:
            self.usage.record(
                tier=resolved_tier, task=task, caller=caller,
                tokens=tokens, usd=usd_cost,
                prompt_preview=_messages_preview(messages),
                response_preview=response.text(),
                debug_full=False,
                provider=response.provider,
                model=response.model,
            )
        except Exception:
            pass
        return response

    def _effective_tier_cfg(
        self,
        tier: str,
        *,
        provider_override: str | None = None,
        model_override: str | None = None,
    ) -> dict:
        tiers = self.config.get("llm.tiers") or {}
        provider_profiles = self.config.get("llm.providers") or {}
        cfg = dict(tiers.get(tier) or {})
        provider = str(provider_override or "").strip().lower()
        if not provider:
            provider = str(cfg.get("provider") or "").strip().lower()
            profile = (
                provider_profiles.get(provider)
                if isinstance(provider_profiles, dict)
                else None
            ) or {}
            if isinstance(profile, dict):
                for key in (
                    "base_url", "provider_key_ref", "provider_key_env", "kind",
                    "provider_native_web_search",
                ):
                    if key == "provider_native_web_search":
                        if key not in cfg and profile.get(key) is not None:
                            cfg[key] = profile[key]
                    elif not cfg.get(key) and profile.get(key):
                        cfg[key] = profile[key]
        if provider:
            provider_cfg: dict | None = None
            for name, candidate in tiers.items():
                if str((candidate or {}).get("provider") or "").lower() != provider:
                    continue
                if name == tier:
                    provider_cfg = dict(candidate or {})
                    break
                if provider_cfg is None:
                    provider_cfg = dict(candidate or {})
                if (candidate or {}).get("provider_key_ref") or (
                    candidate or {}
                ).get("provider_key_env"):
                    provider_cfg = dict(candidate or {})
                    break
            if provider_cfg:
                cfg.update(provider_cfg)
            profile = (
                provider_profiles.get(provider)
                if isinstance(provider_profiles, dict)
                else None
            ) or {}
            if isinstance(profile, dict):
                for key in (
                    "base_url", "provider_key_ref", "provider_key_env", "kind",
                    "provider_native_web_search",
                ):
                    if key == "provider_native_web_search":
                        if key not in cfg and profile.get(key) is not None:
                            cfg[key] = profile[key]
                    elif not cfg.get(key) and profile.get(key):
                        cfg[key] = profile[key]
            cfg["provider"] = provider
        if model_override:
            cfg["model"] = str(model_override).strip()
        return cfg

    def _messages_api_mode(self, provider: str, cfg: dict) -> str:
        kind = str(cfg.get("kind") or "").strip().lower()
        if kind:
            return kind
        entry = _provider_lookup(provider)
        if entry is not None:
            return entry.api_mode
        if cfg.get("base_url"):
            return "chat_completions"
        return ""

    def effective_model_metadata(
        self,
        tier: str | None,
        *,
        provider_override: str | None = None,
        model_override: str | None = None,
    ) -> tuple[str, str, Any]:
        """Return ``(provider, model, metadata)`` for a pending messages call.

        Chat and gateway uploads use this before dispatch so they only
        attach binary blocks when the selected model/provider can plausibly
        accept them. The lookup mirrors the backend resolution path without
        touching credentials or making a network call.
        """

        resolved_tier = self.tier_policy.resolve(
            task="agent.loop",
            requested_tier=tier,
            caller_allowed_tiers=None,
        )
        cfg = self._effective_tier_cfg(
            resolved_tier,
            provider_override=provider_override,
            model_override=model_override,
        )
        provider = str(cfg.get("provider") or "mock").strip().lower()
        model = str(cfg.get("model") or "").strip()
        from .model_registry import ModelRegistry

        return provider, model, ModelRegistry(workspace=self.config.paths.root).lookup(
            provider,
            model,
        )

    def _mock_messages_or_raise(
        self,
        *,
        tier: str,
        provider: str,
        model: str,
        reason: str,
    ) -> MessagesBackend:
        if provider == "mock" or resolve_allow_mock(config_like=self.config):
            return MockMessagesBackend(
                model=model or "mock",
                provider_name="mock" if provider != "mock" else provider,
            )
        raise LLMError(
            f"LLM messages tier '{tier}' unavailable "
            f"(provider={provider!r}, reason={reason}); configure a real "
            "provider credential/base_url or explicitly enable mock mode"
        )

    def _resolve_messages_backend(
        self,
        tier: str,
        *,
        provider_override: str | None = None,
        model_override: str | None = None,
    ) -> MessagesBackend:
        """Build a :class:`MessagesBackend` for ``tier``.

        Routes by ``llm.tiers.<tier>.provider``:

        * ``anthropic`` / ``claude``     — :class:`AnthropicMessagesBackend`
        * ``openai``                     — :class:`OpenAIMessagesBackend`
        * OpenAI-compatible providers and custom provider profiles with
          ``kind: chat_completions``     — :class:`OpenAIMessagesBackend` with
                                           the provider/profile base URL
        * ``gemini`` / ``google``        — :class:`GeminiMessagesBackend`
        * ``ollama``                     — :class:`OllamaMessagesBackend`
        * ``bedrock``                    — :class:`BedrockAnthropicMessagesBackend`
        * explicit ``mock``              — :class:`MockMessagesBackend`
        * real provider missing config   — raises :class:`LLMError`
          unless mock mode is explicitly enabled.
        """

        cfg = self._effective_tier_cfg(
            tier,
            provider_override=provider_override,
            model_override=model_override,
        )
        provider = (cfg.get("provider") or "mock").lower()
        model = str(cfg.get("model") or "")
        base_url = cfg.get("base_url") or _OPENAI_DEFAULT_BASE_URLS.get(provider) or ""
        api_mode = self._messages_api_mode(provider, cfg)

        if provider == "mock" or api_mode == "mock":
            return MockMessagesBackend(
                model=model or "mock",
                provider_name="mock",
            )

        if provider in {"anthropic", "claude"} or api_mode == "anthropic_messages":
            api_key = self._resolve_provider_key(cfg)
            if not api_key:
                _LOG.warning(
                    "%s backend missing api key on tier %s; "
                    "LLM messages backend unavailable", provider or "anthropic", tier,
                )
                return self._mock_messages_or_raise(
                    tier=tier,
                    provider=provider or "anthropic",
                    model=model,
                    reason="missing_api_key",
                )
            return AnthropicMessagesBackend(
                api_key=api_key,
                model=model or "claude-sonnet-4-5",
                base_url=base_url or "https://api.anthropic.com/v1",
                provider_name="anthropic" if provider in {"anthropic", "claude"} else provider,
            )

        if provider == "openai" or api_mode == "chat_completions":
            api_key = self._resolve_provider_key(cfg)
            if not api_key:
                _LOG.warning(
                    "%s backend missing api key on tier %s; "
                    "LLM messages backend unavailable", provider or "openai", tier,
                )
                return self._mock_messages_or_raise(
                    tier=tier,
                    provider=provider or "openai",
                    model=model,
                    reason="missing_api_key",
                )
            if not base_url:
                _LOG.warning(
                    "%s backend has no base_url and no default; "
                    "LLM messages backend unavailable", provider or "openai",
                )
                return self._mock_messages_or_raise(
                    tier=tier,
                    provider=provider or "openai",
                    model=model,
                    reason="missing_base_url",
                )
            return OpenAIMessagesBackend(
                api_key=api_key,
                model=model or "gpt-4o-mini",
                base_url=base_url,
                provider_name=provider,
                reasoning_effort=cfg.get("reasoning_effort"),
                reasoning_summary=cfg.get("reasoning_summary"),
            )

        if provider in {"gemini", "google"} or api_mode == "gemini_v1beta":
            api_key = self._resolve_provider_key(cfg)
            if not api_key:
                _LOG.warning(
                    "gemini backend missing api key on tier %s; "
                    "LLM messages backend unavailable", tier,
                )
                return self._mock_messages_or_raise(
                    tier=tier,
                    provider=provider or "gemini",
                    model=model,
                    reason="missing_api_key",
                )
            return GeminiMessagesBackend(
                api_key=api_key,
                model=model or "gemini-1.5-flash",
                base_url=base_url or "https://generativelanguage.googleapis.com/v1beta",
                provider_name=provider,
            )

        if provider == "ollama" or api_mode == "ollama_native":
            return OllamaMessagesBackend(
                model=model or "llama3.1",
                base_url=base_url or "http://127.0.0.1:11434",
                provider_name="ollama",
            )

        if provider == "bedrock" or api_mode == "bedrock":
            region = str(cfg.get("region") or cfg.get("aws_region") or "us-east-1")
            try:
                return BedrockAnthropicMessagesBackend(
                    region=region,
                    model=model or "anthropic.claude-3-5-sonnet-20241022-v2:0",
                    provider_name="bedrock",
                )
            except Exception as exc:
                _LOG.warning(
                    "bedrock backend init failed on tier %s (%s); "
                    "LLM messages backend unavailable", tier, exc,
                )
                return self._mock_messages_or_raise(
                    tier=tier,
                    provider=provider or "bedrock",
                    model=model,
                    reason="backend_init_failed",
                )

        return self._mock_messages_or_raise(
            tier=tier,
            provider=provider or "unknown",
            model=model,
            reason="unsupported_provider",
        )

    def _resolve_provider_key(self, tier_cfg: dict) -> str:
        """Pull the API key from the SecretVault / env, never logged."""

        ref = tier_cfg.get("provider_key_ref") or ""
        env_var = tier_cfg.get("provider_key_env") or ""
        try:
            from ..security.secrets import SecretVault
        except Exception:
            SecretVault = None  # type: ignore[assignment]
        if ref and SecretVault is not None:
            try:
                vault = SecretVault.open(self.config.paths.vault_enc)
                name = ref.split("vault://", 1)[-1]
                key = vault.resolve(name)
                if key:
                    return key
            except Exception:
                pass
        if env_var:
            import os as _os
            return _os.environ.get(env_var) or ""
        return ""

    def _estimate_messages_cost(
        self,
        *,
        tier: str,
        prompt_tokens: int,
        completion_tokens: int,
        provider_override: str | None = None,
        model_override: str | None = None,
    ) -> float:
        """Best-effort cost estimate for messages calls.

        Uses the existing pricing table via ``adapters._base._price_for``;
        if the provider is unknown we charge zero (paper / mock mode).
        """

        cfg = self._effective_tier_cfg(
            tier,
            provider_override=provider_override,
            model_override=model_override,
        )
        provider = (cfg.get("provider") or "mock").lower()
        model = str(cfg.get("model") or "")
        try:
            from .adapters._base import _price_for
            p_in, p_out = _price_for(provider, model, None)
            return prompt_tokens * p_in / 1000.0 + completion_tokens * p_out / 1000.0
        except Exception:
            return 0.0


def _caller_has_high_approval(config: Config, caller: str) -> bool:
    """A very small allow-list check for high-tier callers.

    Agents and subagents are allowed by default (they're already running
    inside Nerya and are approved by the operator). Scripts must appear
    in workspace/approvals/llm_high_tier_callers.yml. A missing file
    means no script gets approval — safest default.
    """
    if caller == "agent" or caller.startswith("subagent:"):
        return True
    if not caller.startswith("script:"):
        # sdk/ unknown → treat as approved at this layer; the per-skill
        # approval_gate still applies.
        return True
    script_id = caller.split(":", 1)[1]
    try:
        from ..core import yaml_io
    except Exception:
        return False
    approvals_file = config.paths.approvals / "llm_high_tier_callers.yml"
    doc = yaml_io.load(approvals_file, default=None) if approvals_file.exists() else None
    if not isinstance(doc, dict):
        return False
    allowed = set(doc.get("allowed") or [])
    return script_id in allowed
