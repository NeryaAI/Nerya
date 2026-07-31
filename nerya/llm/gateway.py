"""LLMGateway — the only way Nerya reaches an LLM.

Every call resolves a tier, enforces the caller's `LLMSession` (quota,
allowed tiers/tasks), dispatches through the
ModelRouter (which may resolve provider keys via the SecretVault), and
records redacted usage into the llm/security journals.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid
from dataclasses import dataclass
from typing import Any

from ..core import jsonl
from ..core.config import Config
from ..core.errors import LLMError
from ..core.errors import PromptInjectionDetected
from ..core.redaction import redact_display_dict
from ..core.time import now_iso
from ..core.truth import resolve_allow_mock
from ..db import LLMUsageRepository
from ..db.sqlite import connect
from ..security.prompt_injection import flag_suspicious
from .adapters.openai import DEFAULT_BASE_URLS as _OPENAI_DEFAULT_BASE_URLS
from .adapters._base import wire_trace
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
from .route_candidates import (
    RESOLVED_PROVIDER_KEY,
    configured_routes,
    expand_tier_route_cfgs,
    first_configured_route,
    split_csv_values,
)
from .session import LLMSession
from .structured_output import parse as parse_structured
from .tier_policy import TierPolicy
from .usage import LLMUsageJournal

_LOG = logging.getLogger(__name__)

_CONTEXT_FULL_VALUES = {"1", "true", "yes", "on", "full", "context_full", "context-full"}
_CONTEXT_FULL_CORRELATION_KEYS = (
    "session_id",
    "turn_id",
    "iteration",
    "max_iterations",
    "llm_attempt",
    "tool_calls_completed",
    "completed_tool_names",
    "successful_tool_names",
    "required_next_tool_names",
    "text_only_final_attempt",
    "safety_retry_active",
    "context_scope",
    "messages_sent_count",
    "tools_sent_count",
    "remaining_wall_seconds",
    "subagent",
    "team_run_id",
    "strategy_id",
    "trigger_event_id",
    "parent_call_id",
)

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


def _context_full_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _CONTEXT_FULL_VALUES


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
        self.router = ModelRouter(
            tiers=tiers,
            workspace=config.paths.root,
            config_like=config,
        )
        self.usage = LLMUsageJournal(
            journal_path=config.paths.journal("llm"),
            security_path=config.paths.journal("security"),
        )
        self._con = None

    def _con_lazy(self):
        if self._con is None:
            self._con = connect(self.config.paths.db)
        return self._con

    def _context_full_logging_enabled(self) -> bool:
        for env_name in ("NERYA_CONTEXT_FULL_LOG", "NERYA_LLM_CONTEXT_LOG"):
            value = os.environ.get(env_name)
            if value is not None and value != "":
                return _context_full_value(value)
        mode = self.config.get("llm.context_log_mode")
        if mode is not None:
            return _context_full_value(mode)
        return _context_full_value(
            self.config.get("llm.debug_full_context_journal", False)
        )

    def _record_context_full(
        self,
        *,
        call_id: str,
        api: str,
        phase: str,
        task: str,
        caller: str,
        tier: str,
        provider: str = "",
        model: str = "",
        request: dict[str, Any] | None = None,
        response: dict[str, Any] | None = None,
        error: BaseException | None = None,
        tier_config: dict[str, Any] | None = None,
        correlation: dict[str, Any] | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "kind": "llm.context_full",
            "api": api,
            "phase": phase,
            "call_id": call_id,
            "ts": now_iso(),
            "caller": caller,
            "task": task,
            "tier": tier,
        }
        if provider:
            record["provider"] = provider
        if model:
            record["model"] = model
        if correlation:
            record.update(correlation)
        if tier_config is not None:
            record["tier_config"] = self._safe_context_tier_config(tier_config)
        if request is not None:
            record["request"] = request
        if response is not None:
            record["response"] = response
        if error is not None:
            record["error"] = self._context_error_payload(error)
        try:
            jsonl.append(
                self.config.paths.dev_log("llm_context_full"),
                redact_display_dict(record),
            )
        except Exception:
            pass

    def _record_context_wire_event(
        self,
        *,
        call_id: str,
        api: str,
        task: str,
        caller: str,
        tier: str,
        provider: str,
        model: str,
        correlation: dict[str, Any],
        event: dict[str, Any],
    ) -> None:
        phase = str(event.get("phase") or "").strip().lower()
        if phase not in {"request", "response", "error"}:
            return
        method = str(event.get("method") or "POST")
        url = self._safe_context_wire_url(str(event.get("url") or ""))
        body = event.get("body") if isinstance(event.get("body"), dict) else None
        event_provider = str(event.get("provider_name") or provider or "")
        event_model = model
        if isinstance(body, dict) and body.get("model"):
            event_model = str(body.get("model") or event_model)
        base: dict[str, Any] = {
            "method": method,
            "url": url,
            "wire_attempt": int(event.get("wire_attempt") or 1),
            "max_wire_attempts": int(event.get("max_wire_attempts") or 1),
        }
        timeout = event.get("timeout")
        if timeout is not None:
            base["timeout"] = timeout
        if event.get("elapsed_ms") is not None:
            base["elapsed_ms"] = event.get("elapsed_ms")

        request: dict[str, Any] | None = None
        response: dict[str, Any] | None = None
        if phase == "request":
            request = {
                **base,
                "headers": event.get("headers") or {},
                "body": body if body is not None else event.get("body"),
            }
        elif phase == "response":
            response = {
                **base,
                "status": int(event.get("status") or 0),
                "headers": event.get("headers") or {},
                "body": body if body is not None else event.get("body"),
            }
        else:
            response = {
                **base,
                "error": event.get("error") or {},
            }
        self._record_context_full(
            call_id=call_id,
            api=api,
            phase=f"wire_{phase}",
            caller=caller,
            task=task,
            tier=tier,
            provider=event_provider,
            model=event_model,
            correlation={
                **correlation,
                "wire_attempt": base["wire_attempt"],
            },
            request=request,
            response=response,
        )

    @staticmethod
    def _safe_context_wire_url(url: str) -> str:
        if not url:
            return ""
        try:
            parts = urlsplit(url)
        except Exception:
            return url
        if not parts.query:
            return url
        safe_pairs: list[tuple[str, str]] = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            key_norm = key.lower().replace("-", "_")
            if (
                key_norm in {"key", "api_key", "apikey", "token", "access_token"}
                or key_norm.endswith("_key")
                or key_norm.endswith("_token")
                or "secret" in key_norm
            ):
                safe_pairs.append((key, "***REDACTED***" if value else value))
            else:
                safe_pairs.append((key, value))
        return urlunsplit((
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(safe_pairs, doseq=True),
            parts.fragment,
        ))

    @staticmethod
    def _safe_context_tier_config(cfg: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in (
            "provider",
            "model",
            "base_url",
            "kind",
            "timeout_s",
            "timeout",
            "max_tokens",
            "temperature",
            "reasoning_effort",
            "reasoning_summary",
        ):
            if key in cfg:
                out[key] = cfg.get(key)
        if "provider_key_env" in cfg:
            out["has_provider_key_env"] = bool(cfg.get("provider_key_env"))
        if "provider_key_ref" in cfg:
            out["has_provider_key_ref"] = bool(cfg.get("provider_key_ref"))
        if isinstance(cfg.get("routes"), list):
            safe_routes: list[dict[str, Any]] = []
            for route in cfg.get("routes") or []:
                if isinstance(route, dict):
                    safe_routes.append(LLMGateway._safe_context_tier_config(route))
            out["routes"] = safe_routes
        return out

    @staticmethod
    def _safe_context_correlation(metadata: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(metadata, dict):
            return {}
        out: dict[str, Any] = {}
        for key in _CONTEXT_FULL_CORRELATION_KEYS:
            if key not in metadata:
                continue
            value = metadata.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                out[key] = value
            elif isinstance(value, list):
                safe_items: list[Any] = []
                for item in value:
                    if isinstance(item, (str, int, float, bool)) or item is None:
                        safe_items.append(item)
                out[key] = safe_items
        return out

    @staticmethod
    def _context_error_payload(error: BaseException) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": type(error).__name__,
            "message": str(error),
        }
        for attr in ("status_code", "request_id", "raw_body", "response_headers"):
            if hasattr(error, attr):
                payload[attr] = getattr(error, attr)
        return payload

    @staticmethod
    def _provider_profiles(config: Config) -> dict[str, dict[str, Any]]:
        raw = config.get("llm.providers") or {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for provider, profile in raw.items():
            provider_id = str(provider or "").strip().lower()
            if provider_id and isinstance(profile, dict):
                out[provider_id] = dict(profile)
        return out

    @staticmethod
    def _apply_provider_profile(
        cfg: dict[str, Any],
        provider_profiles: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        out = dict(cfg or {})
        provider = str(out.get("provider") or "").strip().lower()
        profile = provider_profiles.get(provider) or {}
        for key in (
            "base_url", "provider_key_ref", "provider_key_env", "kind",
            "provider_native_web_search",
        ):
            if key == "provider_native_web_search":
                if key not in out and profile.get(key) is not None:
                    out[key] = profile[key]
            elif not out.get(key) and profile.get(key):
                out[key] = profile[key]
        return out

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
        metadata: dict[str, Any] | None = None,
        model_provider: str | None = None,
        model_id: str | None = None,
    ) -> LLMCall:
        # resolve tier (task advertised by tier.allowed_tasks)
        resolved_tier = self.tier_policy.resolve(
            task=task,
            requested_tier=tier,
            caller_allowed_tiers=caller_allowed_tiers,
        )

        # Optional per-call provider/model override (used by subagents /
        # team members whose role pins a custom model). The tier still
        # controls task gating, budgets, and journals; only the routed
        # provider+model change.
        override_provider = str(model_provider or "").strip() or None
        override_model = str(model_id or "").strip() or None
        cfg_override: dict[str, Any] | None = None
        if override_provider or override_model:
            cfg_override = self._effective_tier_cfg(
                resolved_tier,
                provider_override=override_provider,
                model_override=override_model,
            )

        clean_prompt = scrub(prompt)
        request_metadata = dict(metadata or {})

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
        tier_cfg = (
            cfg_override
            if cfg_override is not None
            else (self.config.get("llm.tiers") or {}).get(resolved_tier) or {}
        )
        active_provider = tier_cfg.get("provider") or "mock"
        cap = capability_of(active_provider)
        if schema is not None and cap.tiers.get("schema_json_mode") == "unsupported":
            raise LLMError(
                f"provider {active_provider!r} on tier {resolved_tier!r} does "
                "not support schema/JSON mode; pick a tier whose provider "
                "declares schema_json_mode != 'unsupported'"
            )

        # dispatch
        context_full = self._context_full_logging_enabled()
        context_call_id = uuid.uuid4().hex if context_full else ""
        context_correlation = (
            self._safe_context_correlation(request_metadata) if context_full else {}
        )
        if context_full:
            self._record_context_full(
                call_id=context_call_id,
                api="prompt",
                phase="request",
                caller=caller,
                task=task,
                tier=resolved_tier,
                provider=str(active_provider or ""),
                model=str(tier_cfg.get("model") or ""),
                tier_config=dict(tier_cfg),
                correlation=context_correlation,
                request={
                    "prompt": clean_prompt,
                    "schema": schema,
                    "metadata": request_metadata,
                },
            )
        wire_callback = None
        if context_full:
            wire_callback = lambda event: self._record_context_wire_event(
                call_id=context_call_id,
                api="prompt",
                caller=caller,
                task=task,
                tier=resolved_tier,
                provider=str(active_provider or ""),
                model=str(tier_cfg.get("model") or ""),
                correlation=context_correlation,
                event=event,
            )
        try:
            with wire_trace(wire_callback):
                result: CallResult = self.router.dispatch(
                    tier=resolved_tier, task=task, prompt=clean_prompt,
                    schema=schema, caller=caller,
                    cfg_override=cfg_override,
                )
        except Exception as exc:  # pragma: no cover
            err = LLMError(f"router dispatch failed: {exc}")
            if context_full:
                self._record_context_full(
                    call_id=context_call_id,
                    api="prompt",
                    phase="error",
                    caller=caller,
                    task=task,
                    tier=resolved_tier,
                    provider=str(active_provider or ""),
                    model=str(tier_cfg.get("model") or ""),
                    correlation=context_correlation,
                    error=err,
                )
            raise err from exc

        # session post-flight (now we know actual tokens/cost)
        if session is not None:
            session.check_quota_after(tokens=result.tokens, cost=result.usd_cost)
            session.record(tokens=result.tokens, cost=result.usd_cost)

        if context_full:
            self._record_context_full(
                call_id=context_call_id,
                api="prompt",
                phase="response",
                caller=caller,
                task=task,
                tier=resolved_tier,
                provider=getattr(result, "provider", "") or str(active_provider or ""),
                model=getattr(result, "model", "") or str(tier_cfg.get("model") or ""),
                correlation=context_correlation,
                response={
                    "text": result.text,
                    "tokens": result.tokens,
                    "usd_cost": result.usd_cost,
                    "reasoning_text": getattr(result, "reasoning_text", "") or "",
                    "reasoning_tokens": int(getattr(result, "reasoning_tokens", 0) or 0),
                    "reasoning_effort": getattr(result, "reasoning_effort", "") or "",
                },
            )

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
                 session: LLMSession | None = None,
                 metadata: dict[str, Any] | None = None) -> dict:
        prompt = f"Classify this text into one of {labels}:\n{text}"
        intent_tier = self.config.get("llm.intent_tier") or "light"
        return self.call(task="classify", caller=caller, tier=tier or intent_tier,
                         prompt=prompt, session=session, metadata=metadata).parsed

    def extract_json(self, *, caller: str, text: str, schema: dict | None = None,
                      task: str = "extract_json", tier: str | None = None,
                      session: LLMSession | None = None,
                      metadata: dict[str, Any] | None = None) -> dict:
        prompt = f"Extract a JSON matching schema from this text:\n{text}"
        return self.call(task=task, caller=caller, tier=tier, prompt=prompt,
                         schema=schema, session=session, metadata=metadata).parsed

    def compress(self, *, caller: str, text: str, max_tokens: int = 512,
                 session: LLMSession | None = None,
                 metadata: dict[str, Any] | None = None) -> str:
        return self.call(task="compress", caller=caller, tier="light",
                          prompt=f"Compress to <{max_tokens} tokens:\n{text}",
                          session=session, metadata=metadata).raw

    def analyze_signal(self, *, caller: str, text: str, tier: str = "high",
                        session: LLMSession | None = None,
                        schema: dict | None = None,
                        metadata: dict[str, Any] | None = None) -> dict:
        return self.call(task="complex_signal_analysis", caller=caller,
                          tier=tier, prompt=text, schema=schema,
                          session=session, metadata=metadata).parsed

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
        deadline: float | None = None,
        metadata: dict[str, Any] | None = None,
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

        route_cfgs = self._messages_route_cfgs(
            resolved_tier,
            provider_override=model_provider,
            model_override=model_id,
        )
        tier_cfg = route_cfgs[0] if route_cfgs else self._effective_tier_cfg(
            resolved_tier,
            provider_override=model_provider,
            model_override=model_id,
        )
        active_route_cfg = tier_cfg

        request_metadata = dict(metadata or {})
        first_web_search = normalise_provider_native_web_search(
            tier_cfg.get(
                "provider_native_web_search",
                self.config.get("llm.provider_native_web_search"),
            )
        )
        request_metadata["provider_native_web_search"] = first_web_search

        request = MessagesRequest(
            system=system,
            messages=list(messages),
            tools=list(tools or []),
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            reasoning_summary=reasoning_summary,
            deadline=deadline,
            metadata=request_metadata,
        )

        context_full = self._context_full_logging_enabled()
        context_call_id = uuid.uuid4().hex if context_full else ""
        context_provider = str(tier_cfg.get("provider") or "")
        context_model = str(tier_cfg.get("model") or "")
        context_correlation = (
            self._safe_context_correlation(request.metadata) if context_full else {}
        )
        if context_full:
            self._record_context_full(
                call_id=context_call_id,
                api="messages",
                phase="request",
                caller=caller,
                task=task,
                tier=resolved_tier,
                provider=context_provider,
                model=context_model,
                tier_config=dict(tier_cfg),
                correlation=context_correlation,
                request={
                    "system": request.system,
                    "messages": request.messages,
                    "tools": request.tools,
                    "tool_choice": request.tool_choice,
                    "max_tokens": request.max_tokens,
                    "temperature": request.temperature,
                    "stream": request.stream,
                    "reasoning_effort": request.reasoning_effort,
                    "reasoning_summary": request.reasoning_summary,
                    "deadline": request.deadline,
                    "metadata": request.metadata,
                },
            )

        response: MessagesResponse | None = None
        last_exc: Exception | None = None
        for index, route_cfg in enumerate(route_cfgs or [tier_cfg]):
            try:
                route_metadata = dict(request_metadata)
                route_metadata["provider_native_web_search"] = (
                    normalise_provider_native_web_search(
                        route_cfg.get(
                            "provider_native_web_search",
                            self.config.get("llm.provider_native_web_search"),
                        )
                    )
                )
                route_request = MessagesRequest(
                    system=request.system,
                    messages=list(request.messages),
                    tools=list(request.tools or []),
                    tool_choice=request.tool_choice,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                    stream=request.stream,
                    reasoning_effort=request.reasoning_effort,
                    reasoning_summary=request.reasoning_summary,
                    deadline=request.deadline,
                    metadata=route_metadata,
                )
                backend = self._resolve_messages_backend(
                    resolved_tier,
                    provider_override=model_provider,
                    model_override=model_id,
                    route_cfg=route_cfg,
                )
                wire_callback = None
                if context_full:
                    route_provider = str(route_cfg.get("provider") or context_provider)
                    route_model = str(route_cfg.get("model") or context_model)
                    wire_callback = lambda event, rp=route_provider, rm=route_model: self._record_context_wire_event(
                        call_id=context_call_id,
                        api="messages",
                        caller=caller,
                        task=task,
                        tier=resolved_tier,
                        provider=rp,
                        model=rm,
                        correlation=context_correlation,
                        event=event,
                    )
                with wire_trace(wire_callback):
                    response = backend(route_request)
                active_route_cfg = route_cfg
                break
            except Exception as exc:
                last_exc = exc
                if index < len(route_cfgs) - 1:
                    continue
        if response is None:
            exc = last_exc or LLMError("messages backend returned no response")
            if context_full:
                self._record_context_full(
                    call_id=context_call_id,
                    api="messages",
                    phase="error",
                    caller=caller,
                    task=task,
                    tier=resolved_tier,
                    provider=context_provider,
                    model=context_model,
                    correlation=context_correlation,
                    error=exc,
                )
            raise exc

        usage = response.usage or {}
        prompt_tokens = int(usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or 0)
        tokens = prompt_tokens + completion_tokens
        usd_cost = self._estimate_messages_cost(
            tier=resolved_tier,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            provider_override=response.provider or active_route_cfg.get("provider"),
            model_override=response.model or active_route_cfg.get("model"),
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
        if context_full:
            self._record_context_full(
                call_id=context_call_id,
                api="messages",
                phase="response",
                caller=caller,
                task=task,
                tier=resolved_tier,
                provider=response.provider or context_provider,
                model=response.model or context_model,
                correlation=context_correlation,
                response={
                    "content": response.content,
                    "stop_reason": response.stop_reason,
                    "usage": response.usage,
                    "provider": response.provider,
                    "model": response.model,
                },
            )
        return response

    def _effective_tier_cfg(
        self,
        tier: str,
        *,
        provider_override: str | None = None,
        model_override: str | None = None,
    ) -> dict:
        tiers = self.config.get("llm.tiers") or {}
        provider_profiles = self._provider_profiles(self.config)
        base_cfg = dict(tiers.get(tier) or {})
        provider = str(provider_override or "").strip().lower()

        if provider:
            provider_cfg: dict | None = None
            # Check the requested tier's own routes first. Plain dict order
            # used to win here: with provider_override=stepfun and tier=medium,
            # the scan hit the *light* tier's stepfun fallback route first and
            # adopted its inherited policy (reasoning_effort: none, light
            # max_tokens), so reasoning models ran unbounded thinking and
            # returned empty content on the messages path.
            ordered_tiers = [(tier, tiers.get(tier))] + [
                (name, candidate)
                for name, candidate in tiers.items()
                if name != tier
            ]
            for name, candidate in ordered_tiers:
                candidate_cfg = dict(candidate or {})
                candidate_routes = (
                    configured_routes(candidate_cfg)
                    if isinstance(candidate_cfg, dict)
                    else []
                )
                for route in candidate_routes:
                    if str((route or {}).get("provider") or "").lower() != provider:
                        continue
                    if name == tier:
                        provider_cfg = dict(route or {})
                        break
                    if provider_cfg is None:
                        provider_cfg = dict(route or {})
                    if (route or {}).get("provider_key_ref") or (
                        route or {}
                    ).get("provider_key_env"):
                        provider_cfg = dict(route or {})
                        break
                if provider_cfg and (
                    name == tier
                    or provider_cfg.get("provider_key_ref")
                    or provider_cfg.get("provider_key_env")
                ):
                    break
            cfg = {key: value for key, value in base_cfg.items() if key != "routes"}
            if provider_cfg:
                cfg.update(provider_cfg)
            cfg["provider"] = provider
            if model_override:
                cfg["model"] = str(model_override).strip()
            return self._apply_provider_profile(cfg, provider_profiles)

        cfg = self._apply_provider_profile(base_cfg, provider_profiles)
        if cfg.get("routes"):
            routes = [
                self._apply_provider_profile(route, provider_profiles)
                for route in configured_routes(cfg)
            ]
            if model_override:
                for route in routes:
                    route["model"] = str(model_override).strip()
            cfg["routes"] = routes
            if routes:
                first_route = routes[0]
                for key in (
                    "provider", "model", "base_url", "provider_key_ref",
                    "provider_key_env", "kind", "provider_native_web_search",
                ):
                    if key in first_route:
                        cfg[key] = first_route[key]
            return cfg

        if model_override:
            cfg["model"] = str(model_override).strip()
        return cfg

    def _messages_route_cfgs(
        self,
        tier: str,
        *,
        provider_override: str | None = None,
        model_override: str | None = None,
    ) -> list[dict[str, Any]]:
        cfg = self._effective_tier_cfg(
            tier,
            provider_override=provider_override,
            model_override=model_override,
        )
        return expand_tier_route_cfgs(
            cfg,
            keys_for_route=self._resolve_provider_keys,
            model_override=model_override,
        )

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
        route = first_configured_route(cfg)
        provider = str(route.get("provider") or cfg.get("provider") or "mock").strip().lower()
        model = str(route.get("model") or cfg.get("model") or "").strip()
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
        route_cfg: dict[str, Any] | None = None,
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

        cfg = dict(route_cfg) if route_cfg is not None else self._effective_tier_cfg(
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
                timeout=float(cfg.get("timeout_s") or cfg.get("timeout") or 180.0),
                max_attempts=int(
                    cfg.get("http_max_attempts")
                    or cfg.get("max_attempts")
                    or 5
                ),
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

        keys = self._resolve_provider_keys(tier_cfg)
        return keys[0] if keys else ""

    def _resolve_provider_keys(self, tier_cfg: dict) -> list[str]:
        if tier_cfg.get(RESOLVED_PROVIDER_KEY):
            return split_csv_values(tier_cfg.get(RESOLVED_PROVIDER_KEY))
        refs = split_csv_values(tier_cfg.get("provider_key_ref"))
        env_vars = split_csv_values(tier_cfg.get("provider_key_env"))
        keys: list[str] = []

        def add_values(value: Any) -> None:
            for key in split_csv_values(value):
                if key not in keys:
                    keys.append(key)

        try:
            from ..security.secrets import SecretVault
        except Exception:
            SecretVault = None  # type: ignore[assignment]
        if refs and SecretVault is not None:
            for ref in refs:
                if not ref.startswith("vault://"):
                    continue
                try:
                    vault = SecretVault.open(self.config.paths.vault_enc)
                    name = ref.split("vault://", 1)[-1]
                    add_values(vault.resolve(name))
                except Exception:
                    pass
        if env_vars:
            import os as _os
            for env_var in env_vars:
                add_values(_os.environ.get(env_var) or "")
        return keys

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
