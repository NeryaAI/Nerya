"""Local Ollama HTTP adapter (``/api/chat`` + ``/api/tags``)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ...core.errors import LLMError
from ._base import (
    ModelInfo,
    ProviderResult,
    Transport,
    UrllibTransport,
    _estimate_tokens,
    _split_prompt_for_chat,
)


@dataclass
class OllamaAdapter:
    """Talks to a local Ollama daemon. No ``api_key`` required.

    ``list_models`` enumerates what the daemon has actually *pulled* — so
    the router only routes to models that exist locally.
    """
    transport: Transport = field(default_factory=UrllibTransport)
    base_url: str = "http://127.0.0.1:11434"
    timeout: float = 120.0

    def __call__(
        self,
        *,
        tier: str,
        task: str,
        model: str,
        prompt: str,
        schema: dict | None,
        api_key: str = "",
        base_url: str | None = None,
        max_tokens: int = 1024,
        price_overrides: dict | None = None,
        temperature: float = 0.1,
        provider_name: str = "ollama",
    ) -> ProviderResult:
        url = (base_url or self.base_url).rstrip("/") + "/api/chat"
        system, user = _split_prompt_for_chat(prompt, schema)
        body = {
            "model": model,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if schema is not None:
            body["format"] = "json"
        started = time.time()
        status, doc = self.transport.post_json(
            url, headers={"Content-Type": "application/json"},
            body=body, timeout=self.timeout,
        )
        latency_ms = int((time.time() - started) * 1000)

        if status >= 400:
            raise LLMError(
                f"ollama api error ({status}): {doc.get('error') or doc.get('raw')}",
            )

        try:
            text = (doc.get("message") or {}).get("content") or ""
            finish = doc.get("done_reason") or ""
        except Exception as exc:
            raise LLMError(f"ollama returned malformed body: {exc}") from exc

        pt = int(doc.get("prompt_eval_count") or _estimate_tokens(prompt))
        ct = int(doc.get("eval_count") or _estimate_tokens(text))
        tt = pt + ct
        return ProviderResult(
            text=text, prompt_tokens=pt, completion_tokens=ct, total_tokens=tt,
            usd_cost=0.0, model=model, provider=provider_name,
            latency_ms=latency_ms, finish_reason=finish,
        )

    def list_models(self, *, api_key: str = "", base_url: str | None = None) -> list[ModelInfo]:
        url = (base_url or self.base_url).rstrip("/") + "/api/tags"
        status, doc = self.transport.get_json(url, headers={}, timeout=self.timeout)
        if status >= 400:
            raise LLMError(
                f"list_models failed ({status}): {doc.get('error') or doc.get('raw')}",
            )
        return [
            ModelInfo(id=m.get("name", ""), owned_by="local", raw=m)
            for m in (doc.get("models") or [])
        ]


__all__ = ["OllamaAdapter"]
