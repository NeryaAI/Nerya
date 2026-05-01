"""Provider-native ``messages + tools`` interface.

The workspace-native agent loop (:class:`nerya.agent.loop.WorkspaceNativeAgentLoop`)
calls into :meth:`nerya.llm.gateway.LLMGateway.call_messages`, which dispatches
to one of the backends defined here. Each backend speaks a different vendor
wire format but normalises everything to the **same Anthropic-shaped content
blocks** so the loop never has to branch on provider.

Wire formats accepted (input)
-----------------------------
The kernel always sends the canonical Anthropic shape::

    messages = [
        {"role": "user",      "content": "..."},
        {"role": "user",      "content": [
            {"type": "text", "text": "..."},
            {"type": "tool_result", "tool_use_id": "...", "content": [...]}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "..."},
            {"type": "tool_use", "id": "...", "name": "...", "input": {...}}]},
    ]

    tools = [
        {"name": "read_file", "description": "...",
         "input_schema": {"type": "object", "properties": {...}}},
        ...
    ]

Each backend re-encodes that into the format its vendor expects (OpenAI
chat-completions ``tool_calls`` / ``role:tool``, Gemini ``functionCall`` /
``functionResponse`` parts, Ollama ``tools`` mirror of OpenAI).

Wire formats produced (output)
------------------------------
Every backend returns :class:`MessagesResponse` with ``content`` blocks::

    {"type": "text", "text": "..."}
    {"type": "thinking", "thinking": "..."}
    {"type": "tool_use", "id": "toolu_...", "name": "read_file", "input": {...}}

``stop_reason`` follows the Anthropic vocabulary (``end_turn``, ``tool_use``,
``max_tokens``, ``stop_sequence``). ``usage`` carries
``input_tokens``/``output_tokens`` and (when the provider exposes it)
``cache_read_input_tokens``/``cache_creation_input_tokens``.

Backends in this module
-----------------------
* :class:`AnthropicMessagesBackend` — Anthropic Messages API.
* :class:`OpenAIMessagesBackend`    — Chat Completions with tools (used by
  ``openai`` and every OpenAI-compatible provider when given a base URL:
  DeepSeek, Moonshot, OpenRouter, xAI, Mistral, Together, Groq, Cerebras…).
* :class:`GeminiMessagesBackend`    — Google Gemini ``generateContent`` with
  ``functionDeclarations``.
* :class:`OllamaMessagesBackend`    — local Ollama ``/api/chat`` with tools.
* :class:`BedrockAnthropicMessagesBackend` — Anthropic via AWS Bedrock
  (``anthropic.claude-*``). Lazy-imports ``boto3`` so the rest of Nerya keeps
  working without the AWS SDK installed.
* :class:`MockMessagesBackend`      — deterministic offline backend.

Adding a new provider only requires implementing :class:`MessagesBackend`
(callable taking :class:`MessagesRequest`, returning :class:`MessagesResponse`)
and wiring it through :meth:`LLMGateway._resolve_messages_backend`.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from ..core.errors import LLMError
from .adapters._base import (
    Transport,
    UrllibTransport,
    _estimate_tokens,
    _post_with_retry,
)


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rich error helpers (Apr-30 2026 — surface request_id / raw body)
# ---------------------------------------------------------------------------
#
# Before this change, every backend below collapsed an HTTP-error response
# down to ``f"http_{status}"`` whenever the body wasn't a JSON object with
# an ``error.message`` field — which is exactly what happens on a
# Cloudflare / nginx 502 (the upstream gateway returns a tiny HTML page,
# not a JSON envelope). That meant the operator's "openai messages api
# error (502): http_502" log was completely opaque: no provider request
# id, no upstream body excerpt, no way to tell whether OpenAI's
# api.openai.com itself was bouncing or our request shape was poisoned
# (e.g. context-too-long).
#
# :func:`_provider_error_message` extracts everything the upstream gave us
# (request_id from any of the common header names, structured error
# message if present, otherwise the raw body excerpt) and folds it into
# the :class:`LLMError` message string. It also attaches the same fields
# as attributes on the exception so the agent loop's retry diagnostic
# can pull them out when surfacing the retry to the dashboard.

# Header names different upstreams use to publish their request id. We
# probe in order; the first match wins.
_REQUEST_ID_HEADERS: tuple[str, ...] = (
    "x-request-id",
    "openai-request-id",
    "anthropic-request-id",
    "x-amzn-requestid",
    "x-amzn-trace-id",
    "cf-ray",
    "x-cloud-trace-context",
)


def _extract_request_id(resp_headers: dict[str, str] | None) -> str:
    if not resp_headers:
        return ""
    for name in _REQUEST_ID_HEADERS:
        v = resp_headers.get(name) or resp_headers.get(name.lower())
        if v:
            return str(v)
    return ""


def _raw_body_excerpt(doc: dict[str, Any] | None, *, limit: int = 600) -> str:
    """Render the raw upstream body in a form a human can read.

    For JSON-shaped errors we try ``error.message`` first (OpenAI /
    Anthropic / Gemini all use this). When the response is HTML — typical
    of a Cloudflare 502 / nginx 5xx — :class:`UrllibTransport` stuffs the
    raw text into ``doc["raw"]``. Either way we truncate to ``limit``.
    """

    if not doc:
        return ""
    err_obj = doc.get("error")
    if isinstance(err_obj, dict):
        msg = err_obj.get("message")
        if msg:
            return str(msg)[:limit]
    raw = doc.get("raw")
    if raw:
        text = str(raw).strip()
        if len(text) > limit:
            text = text[:limit] + "…"
        return text
    return ""


def _http_post_capturing_headers(
    transport: Transport,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    """Single-shot POST that always returns ``(status, doc, headers)``.

    Used by backends that don't go through :func:`_post_with_retry`
    (Gemini, Ollama) so they can still surface the upstream
    ``request_id`` / raw body excerpt. Falls back to plain
    ``post_json`` if the transport doesn't expose the rich variant
    (``MockTransport`` in some tests).
    """

    if hasattr(transport, "post_json_with_headers"):
        return transport.post_json_with_headers(  # type: ignore[attr-defined]
            url, headers=headers, body=body, timeout=timeout,
        )
    status, doc = transport.post_json(
        url, headers=headers, body=body, timeout=timeout,
    )
    return status, doc, {}


def _make_llm_error(
    *,
    provider: str,
    status: int,
    doc: dict[str, Any] | None,
    resp_headers: dict[str, str] | None,
    operation: str = "messages",
) -> LLMError:
    """Construct an :class:`LLMError` carrying every diagnostic field
    the upstream gave us.

    The string form looks like ::

        openai messages api error (502): bad_gateway | request_id=req_… | body=<html>502 Bad Gateway…

    so the existing log-line / dashboard ThinkingBlock pipeline shows
    the actual provider response inline. Attributes:

    * ``status_code`` — int HTTP status
    * ``request_id`` — provider trace handle
    * ``response_headers`` — full lower-cased header dict
    * ``raw_body`` — body excerpt (≤600 chars)
    """

    body = _raw_body_excerpt(doc) or f"http_{status}"
    request_id = _extract_request_id(resp_headers)
    parts = [f"{provider} {operation} api error ({status}): {body}"]
    if request_id:
        parts.append(f"request_id={request_id}")
    err = LLMError(" | ".join(parts))
    setattr(err, "status_code", int(status))
    setattr(err, "request_id", request_id)
    setattr(err, "response_headers", dict(resp_headers or {}))
    setattr(err, "raw_body", _raw_body_excerpt(doc, limit=2000))
    return err


# ---------------------------------------------------------------------------
# Provider-shaped IO
# ---------------------------------------------------------------------------


@dataclass
class MessagesRequest:
    """Provider-agnostic request for the ``messages`` endpoint.

    ``messages`` carries the Anthropic shape (see module docstring). The
    backend is responsible for translating into its vendor format.
    """

    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: Optional[dict[str, Any]] = None
    max_tokens: int = 4096
    temperature: float = 0.2
    stream: bool = False
    reasoning_effort: Optional[str] = None
    reasoning_summary: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MessagesResponse:
    """Normalised response in Anthropic content-block shape."""

    content: list[dict[str, Any]]
    stop_reason: str = "end_turn"
    usage: dict[str, int] = field(default_factory=dict)
    provider: str = ""
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0

    def text(self) -> str:
        return "".join(b.get("text") or "" for b in self.content if b.get("type") == "text")

    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]


MessagesBackend = Callable[[MessagesRequest], MessagesResponse]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_tool_use_id() -> str:
    return f"toolu_{uuid.uuid4().hex[:24]}"


def _coerce_text(content: Any) -> str:
    """Pull a flat text string out of either a string or content-block list."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict):
                btype = b.get("type")
                if btype == "text":
                    parts.append(str(b.get("text") or ""))
                elif btype == "tool_result":
                    inner = b.get("content")
                    if isinstance(inner, str):
                        parts.append(inner)
                    elif isinstance(inner, list):
                        for p in inner:
                            if isinstance(p, dict) and p.get("type") == "text":
                                parts.append(str(p.get("text") or ""))
        return "\n".join(parts)
    return ""


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------


@dataclass
class AnthropicMessagesBackend:
    """Anthropic Messages API. Native shape — no translation needed."""

    api_key: str
    model: str
    transport: Transport = field(default_factory=UrllibTransport)
    base_url: str = "https://api.anthropic.com/v1"
    anthropic_version: str = "2023-06-01"
    timeout: float = 60.0
    provider_name: str = "anthropic"
    enable_prompt_cache: bool = True

    def __call__(self, request: MessagesRequest) -> MessagesResponse:
        if not self.api_key:
            raise LLMError("anthropic backend requires api_key")
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "system": request.system,
            "messages": request.messages,
        }
        if request.tools:
            body["tools"] = list(request.tools)
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice
        effort = (request.reasoning_effort or "").strip().lower()
        if effort and effort != "none":
            adaptive_effort = _anthropic_adaptive_effort_for(self.model, effort)
            thinking_budget = _anthropic_thinking_budget_for(self.model, effort)
            if adaptive_effort:
                body["thinking"] = {"type": "adaptive"}
                body["output_config"] = {"effort": adaptive_effort}
            elif thinking_budget:
                body["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": thinking_budget,
                }
        url = self.base_url.rstrip("/") + "/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
        }
        started = time.time()
        status, doc, resp_headers = _post_with_retry(
            self.transport,
            url,
            headers=headers,
            body=body,
            timeout=self.timeout,
            provider_name=self.provider_name,
            api_key=self.api_key,
        )
        latency_ms = int((time.time() - started) * 1000)
        if status >= 400:
            raise _make_llm_error(
                provider="anthropic", status=status,
                doc=doc, resp_headers=resp_headers,
            )
        content = doc.get("content") or []
        if not isinstance(content, list):
            raise LLMError("anthropic returned non-list content")
        usage = doc.get("usage") or {}
        return MessagesResponse(
            content=list(content),
            stop_reason=str(doc.get("stop_reason") or ""),
            usage={
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
                "cache_creation_input_tokens": int(
                    usage.get("cache_creation_input_tokens") or 0
                ),
            },
            provider=self.provider_name,
            model=self.model,
            raw=doc,
            latency_ms=latency_ms,
        )


_ANTHROPIC_LEGACY_THINKING_PREFIXES: tuple[str, ...] = (
    "claude-3-7-sonnet",
    "claude-3.7-sonnet",
)
_ANTHROPIC_ADAPTIVE_THINKING_PREFIXES: tuple[str, ...] = (
    "claude-opus-4",
    "claude-sonnet-4",
    "claude-4.6-",
    "claude-4-6-",
    "claude-4.7-",
    "claude-4-7-",
)
_ANTHROPIC_EFFORT_BUDGET: dict[str, int] = {
    "minimal": 512,
    "low": 2048,
    "medium": 8192,
    "high": 24576,
    "xhigh": 32000,
    "max": 32000,
}


def _anthropic_thinking_budget_for(model: str, effort: str) -> int:
    low = (model or "").lower()
    if not any(low.startswith(prefix) for prefix in _ANTHROPIC_LEGACY_THINKING_PREFIXES):
        return 0
    return _ANTHROPIC_EFFORT_BUDGET.get(effort, 0)


def _anthropic_adaptive_effort_for(model: str, effort: str) -> str:
    low = (model or "").lower()
    if not any(low.startswith(prefix) for prefix in _ANTHROPIC_ADAPTIVE_THINKING_PREFIXES):
        return ""
    if effort in {"low", "medium", "high", "xhigh", "max"}:
        return effort
    if effort == "minimal":
        return "low"
    return ""


# ---------------------------------------------------------------------------
# OpenAI backend (also drives every OpenAI-compatible provider)
# ---------------------------------------------------------------------------


def _openai_render_messages(
    *, system: str, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Translate Anthropic-shaped messages into OpenAI chat-completions shape.

    Rules:
    * ``user`` text or content list → ``{role: "user", content: <str>}`` (text-only
      blocks are concatenated; tool_result blocks are extracted into individual
      ``role: "tool"`` messages so we keep their tool_use_id correlation).
    * ``assistant`` mixed content → ``content`` keeps text, ``tool_calls`` keeps
      the tool_use blocks (renamed to function calls).
    * Anthropic ``thinking`` blocks are dropped (OpenAI exposes its own
      reasoning channel out-of-band).
    """

    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
                continue
            if not isinstance(content, list):
                continue
            user_text_parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    user_text_parts.append(str(block.get("text") or ""))
                elif btype == "tool_result":
                    flushed = "\n".join(p for p in user_text_parts if p)
                    if flushed:
                        out.append({"role": "user", "content": flushed})
                        user_text_parts = []
                    inner = block.get("content")
                    if isinstance(inner, list):
                        text_chunks: list[str] = []
                        for p in inner:
                            if isinstance(p, dict) and p.get("type") == "text":
                                text_chunks.append(str(p.get("text") or ""))
                        result_text = "\n".join(text_chunks)
                    elif isinstance(inner, str):
                        result_text = inner
                    else:
                        result_text = ""
                    out.append({
                        "role": "tool",
                        "tool_call_id": str(block.get("tool_use_id") or ""),
                        "content": result_text,
                    })
            if user_text_parts:
                out.append({"role": "user", "content": "\n".join(user_text_parts)})
        elif role == "assistant":
            text_chunks: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            if isinstance(content, str):
                text_chunks.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_chunks.append(str(block.get("text") or ""))
                    elif btype == "tool_use":
                        tool_calls.append({
                            "id": str(block.get("id") or _new_tool_use_id()),
                            "type": "function",
                            "function": {
                                "name": str(block.get("name") or ""),
                                "arguments": json.dumps(
                                    block.get("input") or {},
                                    ensure_ascii=False,
                                ),
                            },
                        })
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            text = "\n".join(t for t in text_chunks if t)
            if text:
                assistant_msg["content"] = text
            else:
                assistant_msg["content"] = None
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            out.append(assistant_msg)
        else:
            if isinstance(content, str):
                out.append({"role": role or "user", "content": content})
    return out


def _openai_render_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "")
        description = str(t.get("description") or "")
        schema = t.get("input_schema") or t.get("parameters") or {}
        rendered.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": schema if isinstance(schema, dict) else {},
            },
        })
    return rendered


def _openai_tool_choice(tool_choice: Optional[dict[str, Any]]) -> Any:
    if not tool_choice:
        return None
    kind = (tool_choice.get("type") or "").lower()
    if kind == "auto":
        return "auto"
    if kind == "none":
        return "none"
    if kind == "any" or kind == "required":
        return "required"
    if kind == "tool":
        return {
            "type": "function",
            "function": {"name": str(tool_choice.get("name") or "")},
        }
    return tool_choice


def _openai_parse_response(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Translate an OpenAI chat-completions response into Anthropic blocks."""

    try:
        choice = (doc.get("choices") or [{}])[0]
    except Exception as exc:
        raise LLMError(f"openai messages: malformed choices: {exc}") from exc
    msg = choice.get("message") or {}
    finish = str(choice.get("finish_reason") or "")
    content_blocks: list[dict[str, Any]] = []

    text_payload = msg.get("content")
    if isinstance(text_payload, str) and text_payload:
        content_blocks.append({"type": "text", "text": text_payload})
    elif isinstance(text_payload, list):
        for p in text_payload:
            if isinstance(p, dict):
                ptype = p.get("type")
                if ptype in {"text", "output_text"}:
                    content_blocks.append({
                        "type": "text",
                        "text": str(p.get("text") or ""),
                    })

    reasoning_blob = msg.get("reasoning") or msg.get("reasoning_content")
    if reasoning_blob:
        rtext = ""
        if isinstance(reasoning_blob, str):
            rtext = reasoning_blob
        elif isinstance(reasoning_blob, list):
            chunks: list[str] = []
            for r in reasoning_blob:
                if isinstance(r, dict):
                    chunks.append(str(r.get("text") or r.get("content") or ""))
                elif isinstance(r, str):
                    chunks.append(r)
            rtext = "\n".join(c for c in chunks if c)
        elif isinstance(reasoning_blob, dict):
            rtext = str(reasoning_blob.get("summary") or reasoning_blob.get("text") or "")
        if rtext:
            content_blocks.insert(0, {"type": "thinking", "thinking": rtext})

    tool_calls = msg.get("tool_calls") or []
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {"_raw": fn.get("arguments") or ""}
            content_blocks.append({
                "type": "tool_use",
                "id": str(tc.get("id") or _new_tool_use_id()),
                "name": str(fn.get("name") or ""),
                "input": args,
            })

    if finish == "tool_calls":
        stop_reason = "tool_use"
    elif finish == "stop":
        stop_reason = "end_turn"
    elif finish == "length":
        stop_reason = "max_tokens"
    elif finish == "content_filter":
        stop_reason = "content_filter"
    else:
        stop_reason = finish or "end_turn"

    return content_blocks, stop_reason


@dataclass
class OpenAIMessagesBackend:
    """OpenAI Chat Completions backend with native ``tools`` support.

    Used directly for ``openai`` and as the engine for every
    OpenAI-compatible provider (DeepSeek, OpenRouter, Moonshot, xAI,
    Mistral, Together, Groq, Cerebras, …) by overriding ``base_url`` and
    ``provider_name``.
    """

    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    transport: Transport = field(default_factory=UrllibTransport)
    timeout: float = 180.0
    provider_name: str = "openai"
    reasoning_effort: Optional[str] = None
    reasoning_summary: Optional[str] = None

    def __call__(self, request: MessagesRequest) -> MessagesResponse:
        if not self.api_key:
            raise LLMError(f"{self.provider_name} backend requires api_key")
        url = self.base_url.rstrip("/") + "/chat/completions"
        rendered_messages = _openai_render_messages(
            system=request.system, messages=request.messages,
        )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": rendered_messages,
        }
        if _is_reasoning_model_id(self.model):
            body["max_completion_tokens"] = request.max_tokens
            eff = (
                request.reasoning_effort
                or self.reasoning_effort
                or ""
            ).strip().lower()
            if eff and eff != "none":
                body["reasoning_effort"] = eff
            summ = (
                request.reasoning_summary
                or self.reasoning_summary
                or ""
            ).strip().lower()
            if summ in {"concise", "detailed", "auto"}:
                body["reasoning"] = {"summary": summ}
                if eff and eff != "none":
                    body["reasoning"]["effort"] = eff
        else:
            body["max_tokens"] = request.max_tokens
            body["temperature"] = request.temperature

        if request.tools:
            body["tools"] = _openai_render_tools(request.tools)
            choice = _openai_tool_choice(request.tool_choice) or "auto"
            body["tool_choice"] = choice

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        started = time.time()
        status, doc, resp_headers = _post_with_retry(
            self.transport,
            url,
            headers=headers,
            body=body,
            timeout=self.timeout,
            provider_name=self.provider_name,
            api_key=self.api_key,
        )
        latency_ms = int((time.time() - started) * 1000)
        if status >= 400:
            raise _make_llm_error(
                provider=self.provider_name, status=status,
                doc=doc, resp_headers=resp_headers,
            )
        content_blocks, stop_reason = _openai_parse_response(doc)
        usage = doc.get("usage") or {}
        return MessagesResponse(
            content=content_blocks,
            stop_reason=stop_reason,
            usage={
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
            },
            provider=self.provider_name,
            model=self.model,
            raw=doc,
            latency_ms=latency_ms,
        )


_REASONING_MODEL_PREFIXES: tuple[str, ...] = (
    "gpt-5", "o1", "o3", "o4", "deepseek-r1", "deepseek-reasoner",
    "qwen-qwq", "qwen3-think", "qwen3-thinking",
)


def _is_reasoning_model_id(model: str) -> bool:
    if not model:
        return False
    low = model.lower()
    return any(low.startswith(p) for p in _REASONING_MODEL_PREFIXES)


# ---------------------------------------------------------------------------
# Gemini backend
# ---------------------------------------------------------------------------


def _gemini_render_contents(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate Anthropic-shaped messages into Gemini ``contents`` parts."""

    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            parts: list[dict[str, Any]] = []
            if isinstance(content, str):
                parts.append({"text": content})
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        parts.append({"text": str(block.get("text") or "")})
                    elif btype == "tool_result":
                        inner = block.get("content")
                        if isinstance(inner, list):
                            text_chunks: list[str] = []
                            for p in inner:
                                if isinstance(p, dict) and p.get("type") == "text":
                                    text_chunks.append(str(p.get("text") or ""))
                            result_text = "\n".join(text_chunks)
                        elif isinstance(inner, str):
                            result_text = inner
                        else:
                            result_text = ""
                        parts.append({
                            "functionResponse": {
                                "name": str(block.get("name") or ""),
                                "response": {
                                    "tool_use_id": str(block.get("tool_use_id") or ""),
                                    "content": result_text,
                                },
                            },
                        })
            if parts:
                out.append({"role": "user", "parts": parts})
        elif role == "assistant":
            parts = []
            if isinstance(content, str):
                parts.append({"text": content})
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        parts.append({"text": str(block.get("text") or "")})
                    elif btype == "tool_use":
                        parts.append({
                            "functionCall": {
                                "name": str(block.get("name") or ""),
                                "args": dict(block.get("input") or {}),
                            },
                        })
            if parts:
                out.append({"role": "model", "parts": parts})
    return out


def _gemini_render_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wrap tool descriptors as a Gemini ``tools`` envelope."""

    decls: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "")
        description = str(t.get("description") or "")
        schema = t.get("input_schema") or t.get("parameters") or {}
        decls.append({
            "name": name,
            "description": description,
            "parameters": schema if isinstance(schema, dict) else {},
        })
    if not decls:
        return []
    return [{"functionDeclarations": decls}]


_GEMINI_THINKING_BUDGETS: dict[str, int] = {
    "minimal": 512,
    "low": 2048,
    "medium": 8192,
    "high": 24576,
    "xhigh": 32000,
    "max": 32000,
}


def _gemini_thinking_config(model: str, effort: str | None) -> dict[str, Any] | None:
    eff = (effort or "").strip().lower()
    if not eff or eff == "none":
        return None
    low = (model or "").lower()
    if not (
        low.startswith("gemini-2.5")
        or low.startswith("gemini-3")
        or low.startswith("gemini-")
    ):
        return None
    if low.startswith("gemini-3"):
        level = eff if eff in {"low", "medium", "high"} else "high"
        if eff == "minimal":
            level = "low"
        return {"thinkingLevel": level, "includeThoughts": True}
    budget = _GEMINI_THINKING_BUDGETS.get(eff)
    if not budget:
        return None
    return {"thinkingBudget": budget, "includeThoughts": True}


def _gemini_parse_response(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    candidates = doc.get("candidates") or []
    parts: list[dict[str, Any]] = []
    finish = ""
    if candidates:
        cand = candidates[0]
        finish = str(cand.get("finishReason") or "")
        content = (cand.get("content") or {})
        parts = list(content.get("parts") or [])

    blocks: list[dict[str, Any]] = []
    saw_tool = False
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("thought") and p.get("text"):
            blocks.append({"type": "thinking", "thinking": str(p.get("text") or "")})
            continue
        if p.get("text"):
            blocks.append({"type": "text", "text": str(p.get("text") or "")})
            continue
        fc = p.get("functionCall")
        if fc:
            saw_tool = True
            blocks.append({
                "type": "tool_use",
                "id": _new_tool_use_id(),
                "name": str(fc.get("name") or ""),
                "input": dict(fc.get("args") or {}),
            })

    if saw_tool or finish == "TOOL_USE":
        stop_reason = "tool_use"
    elif finish == "STOP":
        stop_reason = "end_turn"
    elif finish == "MAX_TOKENS":
        stop_reason = "max_tokens"
    elif finish in {"SAFETY", "RECITATION"}:
        stop_reason = "content_filter"
    else:
        stop_reason = finish.lower() or "end_turn"
    return blocks, stop_reason


@dataclass
class GeminiMessagesBackend:
    """Google Gemini ``generateContent`` backend with function calling."""

    api_key: str
    model: str
    base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    transport: Transport = field(default_factory=UrllibTransport)
    timeout: float = 60.0
    provider_name: str = "gemini"

    def __call__(self, request: MessagesRequest) -> MessagesResponse:
        if not self.api_key:
            raise LLMError("gemini backend requires api_key")
        base = self.base_url.rstrip("/")
        url = f"{base}/models/{self.model}:generateContent?key={self.api_key}"
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": request.system or ""}]},
            "contents": _gemini_render_contents(request.messages),
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_tokens,
            },
        }
        thinking_cfg = _gemini_thinking_config(self.model, request.reasoning_effort)
        if thinking_cfg:
            body["generationConfig"]["thinkingConfig"] = thinking_cfg
        rendered_tools = _gemini_render_tools(request.tools)
        if rendered_tools:
            body["tools"] = rendered_tools
            if request.tool_choice:
                kind = (request.tool_choice.get("type") or "").lower()
                if kind in {"auto", "any", "required", "none"}:
                    mode = {
                        "auto": "AUTO", "any": "ANY",
                        "required": "ANY", "none": "NONE",
                    }[kind]
                    body["toolConfig"] = {
                        "functionCallingConfig": {"mode": mode},
                    }
                elif kind == "tool":
                    body["toolConfig"] = {
                        "functionCallingConfig": {
                            "mode": "ANY",
                            "allowedFunctionNames": [
                                str(request.tool_choice.get("name") or ""),
                            ],
                        },
                    }

        headers = {"Content-Type": "application/json"}
        started = time.time()
        status, doc, resp_headers = _http_post_capturing_headers(
            self.transport, url, headers=headers, body=body,
            timeout=self.timeout,
        )
        latency_ms = int((time.time() - started) * 1000)
        if status >= 400:
            raise _make_llm_error(
                provider="gemini", status=status,
                doc=doc, resp_headers=resp_headers,
            )
        blocks, stop_reason = _gemini_parse_response(doc)
        usage = doc.get("usageMetadata") or {}
        return MessagesResponse(
            content=blocks,
            stop_reason=stop_reason,
            usage={
                "input_tokens": int(usage.get("promptTokenCount") or 0),
                "output_tokens": int(usage.get("candidatesTokenCount") or 0),
            },
            provider=self.provider_name,
            model=self.model,
            raw=doc,
            latency_ms=latency_ms,
        )


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------


@dataclass
class OllamaMessagesBackend:
    """Local Ollama ``/api/chat`` backend with ``tools`` support.

    Ollama since v0.4 speaks an OpenAI-flavoured chat API on its own
    endpoint. We translate the Anthropic shape with the same OpenAI
    helpers but post to ``/api/chat`` (no auth header expected).
    """

    model: str
    base_url: str = "http://127.0.0.1:11434"
    transport: Transport = field(default_factory=UrllibTransport)
    timeout: float = 180.0
    provider_name: str = "ollama"

    def __call__(self, request: MessagesRequest) -> MessagesResponse:
        url = self.base_url.rstrip("/") + "/api/chat"
        rendered_messages = _openai_render_messages(
            system=request.system, messages=request.messages,
        )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": rendered_messages,
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_tokens,
            },
        }
        if request.tools:
            body["tools"] = _openai_render_tools(request.tools)

        headers = {"Content-Type": "application/json"}
        started = time.time()
        status, doc, resp_headers = _http_post_capturing_headers(
            self.transport, url, headers=headers, body=body,
            timeout=self.timeout,
        )
        latency_ms = int((time.time() - started) * 1000)
        if status >= 400:
            raise _make_llm_error(
                provider="ollama", status=status,
                doc=doc, resp_headers=resp_headers,
            )

        msg = doc.get("message") or {}
        blocks: list[dict[str, Any]] = []
        text = msg.get("content")
        if isinstance(text, str) and text:
            blocks.append({"type": "text", "text": text})
        tool_calls = msg.get("tool_calls") or []
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {"_raw": args}
                blocks.append({
                    "type": "tool_use",
                    "id": str(tc.get("id") or _new_tool_use_id()),
                    "name": str(fn.get("name") or ""),
                    "input": dict(args or {}),
                })
        stop_reason = "tool_use" if tool_calls else (
            "end_turn" if doc.get("done") else "stop"
        )
        return MessagesResponse(
            content=blocks,
            stop_reason=stop_reason,
            usage={
                "input_tokens": int(doc.get("prompt_eval_count") or 0),
                "output_tokens": int(doc.get("eval_count") or 0),
            },
            provider=self.provider_name,
            model=self.model,
            raw=doc,
            latency_ms=latency_ms,
        )


# ---------------------------------------------------------------------------
# Bedrock (Anthropic on AWS) backend
# ---------------------------------------------------------------------------


@dataclass
class BedrockAnthropicMessagesBackend:
    """Anthropic Claude served through AWS Bedrock.

    Uses the Bedrock InvokeModel API with the Anthropic Messages payload
    shape (``anthropic_version`` instead of base URL). Auth is handled by
    boto3 (sigv4) so the user only sets ``AWS_*`` env vars or an IAM
    role on the host.

    Lazy-imports ``boto3`` so installing it remains optional. Failing to
    import surfaces as :class:`LLMError`, which the gateway already
    catches and falls back to Mock.
    """

    region: str
    model: str  # e.g. "anthropic.claude-3-5-sonnet-20241022-v2:0"
    anthropic_version: str = "bedrock-2023-05-31"
    timeout: float = 60.0
    provider_name: str = "bedrock"

    def __call__(self, request: MessagesRequest) -> MessagesResponse:
        try:
            import boto3  # type: ignore
            from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
        except Exception as exc:
            raise LLMError(
                "bedrock backend requires `boto3` to be installed"
            ) from exc

        client = boto3.client("bedrock-runtime", region_name=self.region)
        body: dict[str, Any] = {
            "anthropic_version": self.anthropic_version,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "system": request.system,
            "messages": request.messages,
        }
        if request.tools:
            body["tools"] = list(request.tools)
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice

        started = time.time()
        try:
            resp = client.invoke_model(
                modelId=self.model,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body).encode("utf-8"),
            )
        except (ClientError, BotoCoreError) as exc:
            raise LLMError(f"bedrock messages invoke failed: {exc}") from exc
        latency_ms = int((time.time() - started) * 1000)

        raw = resp.get("body")
        if hasattr(raw, "read"):
            raw_bytes = raw.read()
        elif isinstance(raw, (bytes, bytearray)):
            raw_bytes = bytes(raw)
        else:
            raw_bytes = b""
        try:
            doc = json.loads(raw_bytes.decode("utf-8") or "{}")
        except Exception as exc:
            raise LLMError(f"bedrock returned non-json body: {exc}") from exc

        content = doc.get("content") or []
        if not isinstance(content, list):
            raise LLMError("bedrock returned non-list content")
        usage = doc.get("usage") or {}
        return MessagesResponse(
            content=list(content),
            stop_reason=str(doc.get("stop_reason") or ""),
            usage={
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
            },
            provider=self.provider_name,
            model=self.model,
            raw=doc,
            latency_ms=latency_ms,
        )


# ---------------------------------------------------------------------------
# Mock backend (offline / paper mode)
# ---------------------------------------------------------------------------


@dataclass
class MockMessagesBackend:
    """Deterministic backend for offline / paper-mode runs.

    Behaviour:
    * When the latest user message contains the marker
      ``[[call_tool: name args={...}]]`` we emit a single ``tool_use``
      block. Otherwise we emit a single ``text`` block summarising the
      user message and listing visible tools.
    """

    model: str = "mock-messages"
    provider_name: str = "mock"

    def __call__(self, request: MessagesRequest) -> MessagesResponse:
        last_user = ""
        for msg in reversed(request.messages):
            if msg.get("role") == "user":
                last_user = _coerce_text(msg.get("content"))
                break

        marker = "[[call_tool:"
        if marker in last_user:
            head = last_user.split(marker, 1)[1]
            close = head.find("]]")
            spec = head[:close] if close >= 0 else head
            name = spec.split(" ", 1)[0].strip()
            args: dict[str, Any] = {}
            if " args=" in spec:
                tail = spec.split(" args=", 1)[1].strip()
                try:
                    args = json.loads(tail)
                except Exception:
                    args = {"raw": tail}
            return MessagesResponse(
                content=[
                    {"type": "text",
                     "text": f"[mock] dispatching {name} with {args}"},
                    {"type": "tool_use",
                     "id": _new_tool_use_id(),
                     "name": name,
                     "input": args},
                ],
                stop_reason="tool_use",
                usage={"input_tokens": _estimate_tokens(last_user), "output_tokens": 50},
                provider=self.provider_name,
                model=self.model,
                latency_ms=1,
            )
        tool_summary = (
            ", ".join(t.get("name") or "?" for t in request.tools[:6])
            if request.tools
            else "(none)"
        )
        text = (
            f"[mock] you said: {last_user[:280]}\n"
            f"[mock] tools available: {tool_summary}"
        )
        return MessagesResponse(
            content=[{"type": "text", "text": text}],
            stop_reason="end_turn",
            usage={"input_tokens": _estimate_tokens(last_user), "output_tokens": 12},
            provider=self.provider_name,
            model=self.model,
            latency_ms=1,
        )


# ---------------------------------------------------------------------------
# Helpers used by the agent loop
# ---------------------------------------------------------------------------


def descriptors_to_provider_tools(descriptors: Iterable[Any]) -> list[dict[str, Any]]:
    """Render ToolRegistry descriptors as Anthropic-shaped tool specs."""

    out: list[dict[str, Any]] = []
    for d in descriptors:
        if hasattr(d, "to_provider_tool"):
            out.append(d.to_provider_tool())
        elif isinstance(d, dict):
            out.append(d)
    return out


# ``base64`` is imported above so we can extend backends to support image
# input later (Gemini ``inlineData``, OpenAI vision, Bedrock vision). It is
# referenced from the public surface to keep linters happy on builds where
# no caller has touched it yet.
_ = base64  # silence unused-import linters; future-use anchor


__all__ = [
    "AnthropicMessagesBackend",
    "BedrockAnthropicMessagesBackend",
    "GeminiMessagesBackend",
    "MessagesBackend",
    "MessagesRequest",
    "MessagesResponse",
    "MockMessagesBackend",
    "OllamaMessagesBackend",
    "OpenAIMessagesBackend",
    "descriptors_to_provider_tools",
]
