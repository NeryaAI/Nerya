"""Google Gemini REST adapter (generativelanguage.googleapis.com)."""

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
    _price_for,
    _split_prompt_for_chat,
)


@dataclass
class GeminiAdapter:
    """Gemini ``/v1beta/models/<model>:generateContent`` adapter.

    Auth is via ``?key=<api_key>`` query param by default; some deployments
    front it with a ``Authorization: Bearer`` header — switch by overriding
    ``auth_mode`` in the tier config (handled at the router level).
    """
    transport: Transport = field(default_factory=UrllibTransport)
    base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    timeout: float = 30.0

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
        provider_name: str = "gemini",
        reasoning_effort: str | None = None,
        reasoning_summary: str | None = None,
    ) -> ProviderResult:
        if not api_key:
            raise LLMError("gemini adapter requires api_key")

        system, user = _split_prompt_for_chat(prompt, schema)
        base = (base_url or self.base_url).rstrip("/")
        url = f"{base}/models/{model}:generateContent?key={api_key}"
        gen_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            **({"responseMimeType": "application/json"} if schema else {}),
        }
        # Gemini thinking config:
        #   * 2.5 series: ``thinkingBudget`` (tokens) + ``includeThoughts``.
        #   * 3.x+ series: ``thinkingLevel`` (low/medium/high) +
        #     ``includeThoughts``. Setting both ``thinkingBudget`` and
        #     ``thinkingLevel`` errors out.
        # We opt-in only for thinking-capable models; older models silently
        # ignore the field.
        thinking_cfg = _gemini_thinking_config(model, reasoning_effort)
        if thinking_cfg:
            gen_config["thinkingConfig"] = thinking_cfg
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen_config,
        }
        headers = {"Content-Type": "application/json"}
        started = time.time()
        status, doc = self.transport.post_json(
            url, headers=headers, body=body, timeout=self.timeout,
        )
        latency_ms = int((time.time() - started) * 1000)

        if status >= 400:
            err = (doc.get("error") or {}).get("message") or doc.get("raw") or f"http_{status}"
            raise LLMError(f"gemini api error ({status}): {err}")

        try:
            cands = doc.get("candidates") or []
            parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
            # Thought parts are tagged ``thought: true`` in 2.5+. Separate
            # them from the visible answer.
            text_chunks: list[str] = []
            thought_chunks: list[str] = []
            for p in parts:
                t = p.get("text", "") or ""
                if not t:
                    continue
                if p.get("thought"):
                    thought_chunks.append(t)
                else:
                    text_chunks.append(t)
            text = "".join(text_chunks)
            reasoning_text = "\n".join(thought_chunks)
            finish = cands[0].get("finishReason", "") if cands else ""
        except Exception as exc:
            raise LLMError(f"gemini returned malformed body: {exc}") from exc

        usage = doc.get("usageMetadata") or {}
        pt = int(usage.get("promptTokenCount") or _estimate_tokens(prompt))
        ct = int(usage.get("candidatesTokenCount") or _estimate_tokens(text))
        tt = int(usage.get("totalTokenCount") or (pt + ct))
        reasoning_tokens = int(usage.get("thoughtsTokenCount") or 0)
        p_in, p_out = _price_for(provider_name, model, price_overrides)
        usd = pt * p_in / 1000.0 + ct * p_out / 1000.0

        return ProviderResult(
            text=text or "", prompt_tokens=pt, completion_tokens=ct, total_tokens=tt,
            usd_cost=float(usd), model=model, provider=provider_name,
            latency_ms=latency_ms, finish_reason=finish,
            reasoning_text=reasoning_text,
            reasoning_tokens=reasoning_tokens,
            reasoning_effort=str(reasoning_effort or "") if thinking_cfg else "",
        )

    def list_models(self, *, api_key: str, base_url: str | None = None) -> list[ModelInfo]:
        if not api_key:
            raise LLMError("list_models requires api_key")
        url = (base_url or self.base_url).rstrip("/") + f"/models?key={api_key}"
        status, doc = self.transport.get_json(url, headers={}, timeout=self.timeout)
        if status >= 400:
            err = (doc.get("error") or {}).get("message") or doc.get("raw") or f"http_{status}"
            raise LLMError(f"list_models failed ({status}): {err}")
        out: list[ModelInfo] = []
        for item in doc.get("models") or []:
            name = item.get("name", "")
            mid = name.split("/", 1)[-1] if "/" in name else name
            out.append(ModelInfo(
                id=mid, owned_by="google",
                context_length=item.get("inputTokenLimit"),
                capabilities=list(item.get("supportedGenerationMethods") or []),
                raw=item,
            ))
        return out


_GEMINI_25_PREFIXES: tuple[str, ...] = (
    "gemini-2.5", "gemini-2-5",
)

_GEMINI_3_PREFIXES: tuple[str, ...] = (
    "gemini-3", "gemini-3.0", "gemini-3.1",
)

# ``thinkingBudget`` ranges per official docs (2026-04):
#   * 2.5 Pro:        128 .. 32768
#   * 2.5 Flash:        0 .. 24576
#   * 2.5 Flash-Lite: 512 .. 24576
# We use a Pro-leaning ladder and clamp later if necessary.
_GEMINI_EFFORT_TO_BUDGET: dict[str, int] = {
    "minimal": 512,
    "low": 2048,
    "medium": 8192,
    "high": 24576,
    "xhigh": 32768,
    "max": 32768,
}

# Gemini 3+ uses ``thinkingLevel`` (low|medium|high). Map our effort tiers
# onto those buckets so callers can keep using a single vocabulary.
_GEMINI_EFFORT_TO_LEVEL: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


def _gemini_thinking_config(model: str, effort: str | None) -> dict[str, Any] | None:
    """Build ``thinkingConfig`` for a Gemini model and effort tier.

    Returns ``None`` (caller skips the field) when the model is not
    thinking-capable or no effort was provided. Picks ``thinkingLevel``
    for Gemini 3+ and ``thinkingBudget`` for the 2.5 family.
    """
    if not effort or not model:
        return None
    low = model.lower()
    eff = str(effort).lower()
    if any(low.startswith(p) for p in _GEMINI_3_PREFIXES):
        level = _GEMINI_EFFORT_TO_LEVEL.get(eff)
        if not level:
            return None
        return {"thinkingLevel": level, "includeThoughts": True}
    if any(low.startswith(p) for p in _GEMINI_25_PREFIXES):
        budget = _GEMINI_EFFORT_TO_BUDGET.get(eff)
        if not budget:
            return None
        # Clamp to Flash range when the id mentions ``flash``.
        if "flash" in low and "lite" not in low:
            budget = min(budget, 24576)
        if "flash-lite" in low or "flash_lite" in low:
            budget = min(max(budget, 512), 24576)
        return {"thinkingBudget": budget, "includeThoughts": True}
    return None


__all__ = ["GeminiAdapter"]
