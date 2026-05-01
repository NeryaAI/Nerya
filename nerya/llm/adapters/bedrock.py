"""AWS Bedrock adapter (Converse API).

Two invocation modes are supported:

1. **Native ``boto3``** — when ``boto3`` is installed, the adapter talks
   directly to ``bedrock-runtime.converse`` using the default AWS
   credential chain. ``api_key`` is ignored in that path; auth comes from
   the AWS environment.
2. **HTTP proxy** — when a ``base_url`` is provided, the adapter POSTs a
   Converse-shaped body to that URL with ``Authorization: Bearer
   {api_key}``. This is the shape used by internal AWS proxies (e.g.
   LiteLLM, a homegrown signed-gateway) so Nerya doesn't hard-couple to
   boto3.

The first mode is for AWS-native deployments; the second is what the
unit tests exercise because it is deterministic.

``list_models`` works in both modes: ``boto3`` uses the control-plane
``ListFoundationModels``, the proxy path calls ``GET {base_url}/models``.
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


def _try_import_boto3():
    try:
        import boto3  # type: ignore
        return boto3
    except Exception:
        return None


@dataclass
class BedrockAdapter:
    transport: Transport = field(default_factory=UrllibTransport)
    timeout: float = 120.0
    region: str = "us-east-1"

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
        provider_name: str = "bedrock",
        timeout: float | None = None,
    ) -> ProviderResult:
        if base_url:
            return self._via_http_proxy(
                tier=tier, task=task, model=model, prompt=prompt,
                schema=schema, api_key=api_key, base_url=base_url,
                max_tokens=max_tokens, price_overrides=price_overrides,
                temperature=temperature, provider_name=provider_name,
                timeout=timeout,
            )
        return self._via_boto3(
            tier=tier, task=task, model=model, prompt=prompt,
            schema=schema, max_tokens=max_tokens,
            price_overrides=price_overrides, temperature=temperature,
            provider_name=provider_name,
        )

    # ----------------------------------------------------------- http proxy

    def _via_http_proxy(self, *, tier: str, task: str, model: str,
                        prompt: str, schema: dict | None,
                        api_key: str, base_url: str, max_tokens: int,
                        price_overrides: dict | None,
                        temperature: float, provider_name: str,
                        timeout: float | None) -> ProviderResult:
        if not api_key:
            raise LLMError("bedrock http-proxy mode requires api_key")
        system, user = _split_prompt_for_chat(prompt, schema)
        body: dict[str, Any] = {
            "modelId": model,
            "messages": [
                {"role": "user", "content": [{"text": user}]},
            ],
            "inferenceConfig": {
                "maxTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system:
            body["system"] = [{"text": system}]

        url = base_url.rstrip("/") + "/converse"
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
        text, pt, ct = _parse_converse_response(doc, prompt)
        p_in, p_out = _price_for(provider_name, model, price_overrides)
        usd = pt * p_in / 1000.0 + ct * p_out / 1000.0
        return ProviderResult(
            text=text or "", prompt_tokens=pt, completion_tokens=ct,
            total_tokens=pt + ct, usd_cost=float(usd), model=model,
            provider=provider_name, latency_ms=latency_ms,
            finish_reason=str(doc.get("stopReason") or ""),
        )

    # -------------------------------------------------------------- boto3

    def _via_boto3(self, *, tier: str, task: str, model: str,
                   prompt: str, schema: dict | None,
                   max_tokens: int, price_overrides: dict | None,
                   temperature: float, provider_name: str) -> ProviderResult:
        boto3 = _try_import_boto3()
        if boto3 is None:
            raise LLMError(
                "bedrock adapter in native mode requires boto3. "
                "Install it with `pip install boto3` or configure a "
                "bedrock http-proxy base_url instead."
            )
        system, user = _split_prompt_for_chat(prompt, schema)
        client = boto3.client("bedrock-runtime", region_name=self.region)
        started = time.time()
        resp = client.converse(
            modelId=model,
            messages=[{"role": "user", "content": [{"text": user}]}],
            system=([{"text": system}] if system else []),
            inferenceConfig={"maxTokens": max_tokens,
                             "temperature": temperature},
        )
        latency_ms = int((time.time() - started) * 1000)
        text, pt, ct = _parse_converse_response(resp, prompt)
        p_in, p_out = _price_for(provider_name, model, price_overrides)
        usd = pt * p_in / 1000.0 + ct * p_out / 1000.0
        return ProviderResult(
            text=text or "", prompt_tokens=pt, completion_tokens=ct,
            total_tokens=pt + ct, usd_cost=float(usd), model=model,
            provider=provider_name, latency_ms=latency_ms,
            finish_reason=str(resp.get("stopReason") or ""),
        )

    # -------------------------------------------------------------- models

    def list_models(self, *, api_key: str,
                    base_url: str | None = None) -> list[ModelInfo]:
        if base_url:
            url = base_url.rstrip("/") + "/models"
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            status, doc = self.transport.get_json(
                url, headers=headers, timeout=self.timeout,
            )
            if status >= 400:
                err = (doc.get("error") or {}).get("message") or doc.get("raw") or f"http_{status}"
                raise LLMError(f"bedrock list_models failed ({status}): {err}")
            out: list[ModelInfo] = []
            for item in (doc.get("modelSummaries") or doc.get("data") or []):
                out.append(ModelInfo(
                    id=str(item.get("modelId") or item.get("id") or ""),
                    owned_by=str(item.get("providerName") or item.get("owned_by") or ""),
                    context_length=item.get("context_length")
                    or item.get("inputContextLength"),
                    raw=item,
                ))
            return out
        # native boto3 path
        boto3 = _try_import_boto3()
        if boto3 is None:
            raise LLMError(
                "bedrock list_models requires either a base_url (http proxy) or boto3"
            )
        client = boto3.client("bedrock", region_name=self.region)
        resp = client.list_foundation_models()
        out: list[ModelInfo] = []
        for s in resp.get("modelSummaries") or []:
            out.append(ModelInfo(
                id=str(s.get("modelId") or ""),
                owned_by=str(s.get("providerName") or ""),
                context_length=None,
                raw=dict(s),
            ))
        return out


def _parse_converse_response(doc: dict[str, Any],
                             prompt: str) -> tuple[str, int, int]:
    """Extract (text, prompt_tokens, completion_tokens) from a Converse
    response or a proxy that mirrors the same shape."""
    text = ""
    try:
        parts = (
            ((doc.get("output") or {}).get("message") or {}).get("content")
            or []
        )
        for p in parts:
            if isinstance(p, dict) and p.get("text"):
                text += str(p["text"])
    except Exception:
        text = ""
    usage = doc.get("usage") or {}
    pt = int(usage.get("inputTokens") or usage.get("prompt_tokens")
             or _estimate_tokens(prompt))
    ct = int(usage.get("outputTokens") or usage.get("completion_tokens")
             or _estimate_tokens(text))
    return text, pt, ct


__all__ = ["BedrockAdapter"]
