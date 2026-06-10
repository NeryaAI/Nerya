"""Anthropic Messages API adapter with prompt-cache support."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ...core.errors import LLMError
from ..prompt_caching import apply_anthropic_cache_control
from ._base import (
    ModelInfo,
    ProviderResult,
    Transport,
    UrllibTransport,
    _estimate_tokens,
    _post_with_retry,
    _price_for,
    _split_prompt_for_chat,
)


@dataclass
class AnthropicAdapter:
    transport: Transport = field(default_factory=UrllibTransport)
    base_url: str = "https://api.anthropic.com/v1"
    timeout: float = 30.0
    anthropic_version: str = "2023-06-01"
    enable_prompt_cache: bool = True
    cache_ttl: str = "5m"  # "5m" or "1h"

    def __call__(
        self,
        *,
        tier: str,
        task: str,
        model: str,
        prompt: str,
        schema: dict | None,
        api_key: str,
        base_url: str | None = None,
        max_tokens: int = 1024,
        price_overrides: dict | None = None,
        temperature: float = 0.1,
        provider_name: str = "anthropic",
        prompt_cache: bool | None = None,
        reasoning_effort: str | None = None,
        reasoning_summary: str | None = None,
    ) -> ProviderResult:
        if not api_key:
            raise LLMError("anthropic adapter requires api_key")

        url = (base_url or self.base_url).rstrip("/") + "/messages"
        system, user = _split_prompt_for_chat(prompt, schema)
        messages = [{"role": "user", "content": user}]
        system_msgs = [{"role": "system", "content": system}] if system else []

        use_cache = self.enable_prompt_cache if prompt_cache is None else bool(prompt_cache)
        if use_cache:
            cached = apply_anthropic_cache_control(
                system_msgs + messages, cache_ttl=self.cache_ttl,
            )
            system_out = cached[0]["content"] if cached and cached[0].get("role") == "system" else system
            messages_out = [m for m in cached if m.get("role") != "system"]
        else:
            system_out = system
            messages_out = messages

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_out,
            "messages": messages_out,
        }
        # Extended thinking — emitted only when the caller asks for it AND
        # the model id matches a thinking-capable Claude. Two API shapes:
        #   * Newer models (claude-opus-4-7+, claude-4.6-*) prefer adaptive
        #     thinking with ``output_config.effort`` (low/medium/high/xhigh/max).
        #   * Older models (claude-3-7-sonnet*, claude-opus-4 / sonnet-4 GA)
        #     keep the legacy ``thinking.type=enabled`` + ``budget_tokens``.
        # Setting reasoning_effort="" disables thinking even on supported models.
        adaptive_effort = _adaptive_effort_for(model, reasoning_effort)
        used_thinking = False
        if adaptive_effort:
            body["thinking"] = {"type": "adaptive"}
            body["output_config"] = {"effort": adaptive_effort}
            body["temperature"] = 1.0
            used_thinking = True
        else:
            thinking_budget = _thinking_budget_for(model, reasoning_effort)
            if thinking_budget:
                body["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": thinking_budget,
                }
                body["temperature"] = 1.0
                used_thinking = True
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": self.anthropic_version,
        }
        started = time.time()
        status, doc, _ = _post_with_retry(
            self.transport, url, headers=headers, body=body, timeout=self.timeout,
            provider_name=provider_name, api_key=api_key,
        )
        latency_ms = int((time.time() - started) * 1000)

        if status >= 400:
            err = _provider_error_text(doc, status=status)
            raise LLMError(f"anthropic api error ({status}): {err}")

        try:
            blocks = doc["content"] or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            # ``thinking`` blocks carry the visible-to-operator chain of
            # thought. We surface them as ``reasoning_text`` but do NOT
            # feed them back into prompts.
            reasoning_text = "\n".join(
                b.get("thinking", "") for b in blocks
                if b.get("type") == "thinking" and b.get("thinking")
            )
            finish = doc.get("stop_reason") or ""
        except Exception as exc:
            raise LLMError(f"anthropic returned malformed body: {exc}") from exc

        usage = doc.get("usage") or {}
        pt = int(usage.get("input_tokens") or _estimate_tokens(prompt))
        ct = int(usage.get("output_tokens") or _estimate_tokens(text))
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        tt = pt + ct + cache_read + cache_write

        p_in, p_out = _price_for(provider_name, model, price_overrides)
        # Anthropic cache pricing: cache_read = 10% of input, cache_write = 125%.
        usd = pt * p_in / 1000.0 + ct * p_out / 1000.0
        if cache_read:
            usd += cache_read * (p_in * 0.10) / 1000.0
        if cache_write:
            usd += cache_write * (p_in * 1.25) / 1000.0

        return ProviderResult(
            text=text or "", prompt_tokens=pt + cache_read + cache_write,
            completion_tokens=ct, total_tokens=tt,
            usd_cost=float(usd), model=model, provider=provider_name,
            latency_ms=latency_ms, finish_reason=finish,
            reasoning_text=reasoning_text,
            reasoning_tokens=0,  # Anthropic does not separate thinking tokens
            reasoning_effort=str(reasoning_effort or "") if used_thinking else "",
        )

    def list_models(self, *, api_key: str, base_url: str | None = None) -> list[ModelInfo]:
        if not api_key:
            raise LLMError("list_models requires api_key")
        url = (base_url or self.base_url).rstrip("/") + "/models"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": self.anthropic_version,
        }
        status, doc = self.transport.get_json(url, headers=headers, timeout=self.timeout)
        if status >= 400:
            err = _provider_error_text(doc, status=status)
            raise LLMError(f"list_models failed ({status}): {err}")
        out: list[ModelInfo] = []
        for item in doc.get("data") or []:
            out.append(ModelInfo(
                id=str(item.get("id") or ""),
                owned_by="anthropic",
                raw=item,
            ))
        return out


_LEGACY_THINKING_PREFIXES: tuple[str, ...] = (
    "claude-3-7-sonnet", "claude-3.7-sonnet",
)

_ADAPTIVE_THINKING_PREFIXES: tuple[str, ...] = (
    "claude-opus-4", "claude-sonnet-4",
    "claude-4.6-", "claude-4-6-",
    "claude-4.7-", "claude-4-7-",
    "claude-opus-4-7", "claude-sonnet-4-7",
)

_EFFORT_TO_BUDGET: dict[str, int] = {
    "minimal": 512,
    "low": 2048,
    "medium": 8192,
    "high": 24576,
    "xhigh": 32000,
    "max": 32000,
}

_ADAPTIVE_EFFORTS: frozenset[str] = frozenset(
    {"low", "medium", "high", "xhigh", "max"}
)


def _thinking_budget_for(model: str, effort: str | None) -> int:
    """Return the Anthropic ``budget_tokens`` for the legacy thinking API.

    Returns 0 unless the model is a legacy thinking-capable Claude
    (3.7 sonnet) and the caller provided an effort string.
    """
    if not effort or not model:
        return 0
    low = model.lower()
    if not any(low.startswith(p) for p in _LEGACY_THINKING_PREFIXES):
        return 0
    return _EFFORT_TO_BUDGET.get(str(effort).lower(), 0)


def _adaptive_effort_for(model: str, effort: str | None) -> str:
    """Map ``reasoning_effort`` to the Anthropic adaptive ``output_config.effort``.

    Returns "" unless the model id matches an adaptive-thinking Claude
    (claude-opus-4*, claude-sonnet-4*, claude-4.6-*, claude-4.7-*) and the
    caller passed a recognised effort tier.
    """
    if not effort or not model:
        return ""
    low = model.lower()
    if not any(low.startswith(p) for p in _ADAPTIVE_THINKING_PREFIXES):
        return ""
    eff = str(effort).lower()
    if eff in _ADAPTIVE_EFFORTS:
        return eff
    if eff == "minimal":
        return "low"
    return ""


def _provider_error_text(doc: dict[str, Any], *, status: int) -> str:
    err = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(err, dict):
        msg = err.get("message")
        if msg:
            return str(msg)
    if err:
        return str(err)
    raw = doc.get("raw") if isinstance(doc, dict) else None
    if raw:
        return str(raw)
    return f"http_{status}"


__all__ = ["AnthropicAdapter"]
