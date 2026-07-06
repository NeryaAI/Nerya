"""OpenAI Chat Completions adapter + OpenAI-compatible family.

``OpenAIAdapter`` drives the canonical OpenAI API (``/chat/completions``,
``/models``). ``OpenAICompatAdapter`` subclasses it to route requests
through any OpenAI-compatible base URL (DeepSeek, OpenRouter, xAI,
Moonshot, Mistral, Together, Groq, Cerebras, Nvidia NIM, …).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ...core.errors import LLMError
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


# =============================================================== default urls
# The canonical map lives in :mod:`nerya.llm.provider_catalog`. We re-export
# it here so legacy callers (tests, ops surfaces) that imported
# ``DEFAULT_BASE_URLS`` directly from this module keep working without
# triggering a circular import (the catalog file does not import this
# module).
def _load_default_base_urls() -> dict[str, str]:
    from ..provider_catalog import DEFAULT_BASE_URLS as _cat
    return dict(_cat)


DEFAULT_BASE_URLS: dict[str, str] = _load_default_base_urls()


@dataclass
class OpenAIAdapter:
    """Adapter for OpenAI Chat Completions + ``/models`` endpoints.

    Used directly for ``openai`` and as the base class for every
    OpenAI-compatible provider.
    """
    transport: Transport = field(default_factory=UrllibTransport)
    base_url: str = "https://api.openai.com/v1"
    # Heavy reasoning models stream for 60-90 s on a single turn; 30 s is too
    # aggressive and causes spurious 500s on long user prompts.
    timeout: float = 180.0

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
        provider_name: str = "openai",
        timeout: float | None = None,
        reasoning_effort: str | None = None,
        reasoning_summary: str | None = None,
    ) -> ProviderResult:
        if not api_key:
            raise LLMError(f"{provider_name} adapter requires api_key")

        url = (base_url or self.base_url).rstrip("/") + "/chat/completions"
        system, user = _split_prompt_for_chat(prompt, schema)
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }

        # Reasoning models (gpt-5*, gpt-5.x*, o1*, o3*, o4*) refuse a non-
        # default ``temperature`` and accept ``reasoning_effort``. Detect
        # by model id and route accordingly. The Chat Completions API
        # accepts ``reasoning_effort`` directly (str) — valid values per
        # OpenAI's 2026 docs are "none" | "minimal" | "low" | "medium" |
        # "high" | "xhigh". Unknown values are passed through verbatim
        # (useful for compat servers); legacy models silently ignore.
        is_reasoning_model = _is_reasoning_model(model)
        minimax_compat = _is_minimax_openai_compat(
            provider_name=provider_name,
            base_url=base_url or self.base_url,
            model=model,
        )
        if is_reasoning_model:
            # gpt-5/o-series ignore ``temperature`` and require
            # ``max_completion_tokens`` instead of ``max_tokens``.
            body.pop("max_tokens", None)
            body["max_completion_tokens"] = max_tokens
            eff = (reasoning_effort or "").strip().lower()
            if eff and eff != "none":
                body["reasoning_effort"] = eff
            # ``reasoning_summary`` is a Responses-API field; some compat
            # servers (e.g. OpenRouter, vLLM, Together) honour it via the
            # ``reasoning`` envelope on chat-completions. Pass it through
            # only when the caller supplies a non-empty value.
            summ = (reasoning_summary or "").strip().lower()
            if summ in {"concise", "detailed", "auto"}:
                body["reasoning"] = {"summary": summ}
                if eff and eff != "none":
                    body["reasoning"]["effort"] = eff
        elif minimax_compat:
            # MiniMax's OpenAI-compatible endpoint uses completion-token
            # naming and explicit thinking controls for MiniMax-M3.
            body.pop("max_tokens", None)
            body["max_completion_tokens"] = max_tokens
            body["temperature"] = temperature
            if _minimax_thinking_enabled(reasoning_effort):
                body["thinking"] = {"type": "adaptive"}
                body["reasoning_split"] = True
            else:
                body["thinking"] = {"type": "disabled"}
        else:
            body["temperature"] = temperature
        if schema is not None:
            body["response_format"] = {"type": "json_object"}

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        started = time.time()
        status, doc, _ = _post_with_retry(
            self.transport, url, headers=headers, body=body,
            timeout=timeout if timeout is not None else self.timeout,
            provider_name=provider_name, api_key=api_key,
        )
        latency_ms = int((time.time() - started) * 1000)

        if status >= 400:
            err = _provider_error_text(doc, status=status)
            raise LLMError(f"{provider_name} api error ({status}): {err}")

        try:
            choice = doc["choices"][0]
            msg = choice.get("message") or {}
            text = _message_text(msg)
            finish = choice.get("finish_reason", "")
            # Reasoning summary surfaces. OpenAI gpt-5 + o-series return it
            # under ``message.reasoning`` (string OR list of {type,text});
            # OpenRouter mirrors the same shape for reasoning-capable models.
            if isinstance(msg, dict):
                reasoning_blob = (
                    msg.get("reasoning")
                    or msg.get("reasoning_content")
                    or msg.get("reasoning_details")
                )
            else:
                reasoning_blob = None
            reasoning_text = _extract_reasoning_text(reasoning_blob)
        except Exception as exc:
            raise LLMError(f"{provider_name} returned malformed body: {exc}") from exc

        usage = doc.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or _estimate_tokens(prompt))
        ct = int(usage.get("completion_tokens") or _estimate_tokens(text))
        tt = int(usage.get("total_tokens") or (pt + ct))
        # Reasoning tokens are reported under ``completion_tokens_details``.
        details = usage.get("completion_tokens_details") or {}
        reasoning_tokens = int(details.get("reasoning_tokens") or 0)

        p_in, p_out = _price_for(provider_name, model, price_overrides)
        usd = pt * p_in / 1000.0 + ct * p_out / 1000.0

        return ProviderResult(
            text=text or "", prompt_tokens=pt, completion_tokens=ct, total_tokens=tt,
            usd_cost=float(usd), model=model, provider=provider_name,
            latency_ms=latency_ms, finish_reason=finish,
            reasoning_text=reasoning_text,
            reasoning_tokens=reasoning_tokens,
            reasoning_effort=str(reasoning_effort or "") if is_reasoning_model else "",
        )

    def list_models(self, *, api_key: str, base_url: str | None = None) -> list[ModelInfo]:
        if not api_key:
            raise LLMError("list_models requires api_key")
        url = (base_url or self.base_url).rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        status, doc = self.transport.get_json(url, headers=headers, timeout=self.timeout)
        if status >= 400:
            err = (doc.get("error") or {}).get("message") or doc.get("raw") or f"http_{status}"
            raise LLMError(f"list_models failed ({status}): {err}")
        out: list[ModelInfo] = []
        for item in doc.get("data") or []:
            out.append(ModelInfo(
                id=str(item.get("id") or ""),
                owned_by=str(item.get("owned_by") or ""),
                context_length=item.get("context_length") or item.get("context_window"),
                raw=item,
            ))
        return out


@dataclass
class OpenAICompatAdapter(OpenAIAdapter):
    """Routes requests through any OpenAI-compatible ``base_url``.

    Falls back to :data:`DEFAULT_BASE_URLS` when ``base_url`` is blank —
    callers can omit it and rely on the per-provider default.
    """
    def __call__(
        self,
        *,
        tier: str,
        task: str,
        model: str,
        prompt: str,
        schema: dict | None,
        api_key: str,
        base_url: str,
        max_tokens: int = 1024,
        price_overrides: dict | None = None,
        temperature: float = 0.1,
        provider_name: str = "compat",
        timeout: float | None = None,
        reasoning_effort: str | None = None,
        reasoning_summary: str | None = None,
    ) -> ProviderResult:
        if not base_url:
            base_url = DEFAULT_BASE_URLS.get(provider_name, "")
        return super().__call__(
            tier=tier, task=task, model=model, prompt=prompt, schema=schema,
            api_key=api_key, base_url=base_url, max_tokens=max_tokens,
            price_overrides=price_overrides, temperature=temperature,
            provider_name=provider_name, timeout=timeout,
            reasoning_effort=reasoning_effort,
            reasoning_summary=reasoning_summary,
        )

    def list_models(self, *, api_key: str, base_url: str | None = None,
                    provider_name: str = "compat") -> list[ModelInfo]:
        target = base_url or DEFAULT_BASE_URLS.get(provider_name)
        if not target:
            raise LLMError(f"list_models for {provider_name!r} needs base_url")
        return super().list_models(api_key=api_key, base_url=target)


# =============================================================== helpers
_REASONING_MODEL_PREFIXES: tuple[str, ...] = (
    "gpt-5", "o1", "o3", "o4",
    "deepseek-r1", "deepseek-reasoner",
    "qwen-qwq", "qwen3-think", "qwen3-thinking",
    # Stepfun (阶跃星辰) — step-3 / step-3.x / step-r1-* are reasoning models
    # whose API counts internal reasoning against the completion budget. They
    # MUST be called with ``max_completion_tokens`` and ``reasoning_effort``
    # plumbed through so the model can pace itself; otherwise large prompts
    # exhaust ``max_tokens`` on reasoning and emit zero visible content
    # (observed empirically with step-3.6 + reasoning_effort=high on 44k
    # char subagent prompts).
    "step-3", "step-r1",
)


def _is_reasoning_model(model: str) -> bool:
    """Return True if the model id implies a reasoning/thinking model.

    Reasoning models reject ``temperature`` and accept ``reasoning_effort``;
    they also tend to require ``max_completion_tokens`` instead of
    ``max_tokens``. Detection is conservative — unknown models default to
    the legacy chat-completions shape.
    """
    if not model:
        return False
    low = model.lower()
    # Gateways (GMI Cloud, OpenRouter, ...) namespace passthrough models as
    # "vendor/model" (e.g. "openai/gpt-5.5"). Match on the bare model name,
    # otherwise reasoning models behind a gateway silently fall into the
    # legacy branch and get max_tokens/temperature — which the strict
    # upstream rejects with 400 unsupported_parameter.
    bare = low.rsplit("/", 1)[-1]
    return any(
        low.startswith(p) or bare.startswith(p)
        for p in _REASONING_MODEL_PREFIXES
    )


_MINIMAX_THINKING_ON_VALUES: frozenset[str] = frozenset({
    "1", "true", "on", "enabled", "enable", "adaptive",
})


def _is_minimax_openai_compat(
    *, provider_name: str, base_url: str, model: str,
) -> bool:
    haystack = " ".join(
        str(part or "").lower()
        for part in (provider_name, base_url, model)
    )
    return "minimax" in haystack


def _minimax_thinking_enabled(effort: str | None) -> bool:
    return str(effort or "").strip().lower() in _MINIMAX_THINKING_ON_VALUES


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


def _message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if text:
                    chunks.append(str(text))
        return "\n".join(chunks)
    if isinstance(content, dict):
        text = content.get("text") or content.get("content")
        return str(text or "")
    return ""


def _extract_reasoning_text(blob: Any) -> str:
    """Normalise the various provider shapes for reasoning summaries.

    Accepts:
      * ``None`` — returns "".
      * ``str``   — returned as-is.
      * ``list[{type, text}]`` — concatenated text fields.
      * ``dict`` with ``summary`` / ``text`` keys.
    """
    if not blob:
        return ""
    if isinstance(blob, str):
        return blob
    if isinstance(blob, dict):
        for key in ("summary", "text", "content"):
            v = blob.get(key)
            if isinstance(v, str) and v:
                return v
        return ""
    if isinstance(blob, list):
        out: list[str] = []
        for item in blob:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                t = item.get("text") or item.get("content") or ""
                if isinstance(t, str) and t:
                    out.append(t)
        return "\n".join(out)
    return ""


__all__ = ["OpenAIAdapter", "OpenAICompatAdapter", "DEFAULT_BASE_URLS"]
