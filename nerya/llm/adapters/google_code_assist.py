"""Google Code Assist (Cloud Code / OAuth-backed Gemini) adapter.

Google Code Assist is the Gemini flavor you get when authenticating via
Google Cloud OAuth instead of a Generative Language API key. The wire
format is a thin superset of the Gemini ``generateContent`` body; the
differences that matter to us are:

- auth is ``Authorization: Bearer <oauth_token>`` instead of an API key
  query parameter
- the endpoint is under ``cloudcode-pa.googleapis.com`` / a user-supplied
  Vertex base URL rather than ``generativelanguage.googleapis.com``
- model identifiers look like ``projects/{project}/locations/{location}/publishers/google/models/gemini-*``

The adapter accepts the OAuth access token through ``api_key`` (the
field already means "bearer credential" everywhere else in our adapter
family) and resolves the request URL using ``base_url`` + ``model``.
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


@dataclass
class GoogleCodeAssistAdapter:
    transport: Transport = field(default_factory=UrllibTransport)
    base_url: str = "https://cloudcode-pa.googleapis.com/v1internal"
    timeout: float = 120.0

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
        provider_name: str = "google_code_assist",
        timeout: float | None = None,
    ) -> ProviderResult:
        if not api_key:
            raise LLMError(
                f"{provider_name} adapter requires an OAuth access token "
                "(passed through api_key)."
            )
        system, user = _split_prompt_for_chat(prompt, schema)

        contents: list[dict[str, Any]] = []
        if system:
            contents.append({
                "role": "user",
                "parts": [{"text": f"System instructions:\n{system}"}],
            })
        contents.append({"role": "user", "parts": [{"text": user}]})

        body: dict[str, Any] = {
            "model": model,
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if schema is not None:
            body["generationConfig"]["responseMimeType"] = "application/json"

        url = (base_url or self.base_url).rstrip("/") + f"/models/{model}:generateContent"
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
            err = (doc.get("error") or {}).get("message") or doc.get("raw") or f"http_{status}"
            raise LLMError(f"{provider_name} api error ({status}): {err}")

        text = ""
        finish = ""
        for cand in (doc.get("candidates") or []):
            finish = cand.get("finishReason") or finish
            for part in ((cand.get("content") or {}).get("parts") or []):
                if isinstance(part, dict) and part.get("text"):
                    text += str(part["text"])

        usage = doc.get("usageMetadata") or {}
        pt = int(usage.get("promptTokenCount") or _estimate_tokens(prompt))
        ct = int(usage.get("candidatesTokenCount") or _estimate_tokens(text))
        tt = int(usage.get("totalTokenCount") or (pt + ct))

        p_in, p_out = _price_for(provider_name, model, price_overrides)
        usd = pt * p_in / 1000.0 + ct * p_out / 1000.0

        return ProviderResult(
            text=text or "", prompt_tokens=pt, completion_tokens=ct,
            total_tokens=tt, usd_cost=float(usd), model=model,
            provider=provider_name, latency_ms=latency_ms,
            finish_reason=finish,
        )

    def list_models(self, *, api_key: str,
                    base_url: str | None = None) -> list[ModelInfo]:
        if not api_key:
            raise LLMError("list_models requires an OAuth access token")
        url = (base_url or self.base_url).rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        status, doc = self.transport.get_json(
            url, headers=headers, timeout=self.timeout,
        )
        if status >= 400:
            err = (doc.get("error") or {}).get("message") or doc.get("raw") or f"http_{status}"
            raise LLMError(f"google_code_assist list_models failed ({status}): {err}")
        out: list[ModelInfo] = []
        for item in (doc.get("models") or doc.get("data") or []):
            out.append(ModelInfo(
                id=str(item.get("name") or item.get("id") or ""),
                owned_by=str(item.get("displayName") or "google"),
                context_length=item.get("inputTokenLimit"),
                raw=item,
            ))
        return out


__all__ = ["GoogleCodeAssistAdapter"]
