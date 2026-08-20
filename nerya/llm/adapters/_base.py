"""Shared plumbing for every LLM provider adapter.

Contains:

* ``ProviderResult`` / ``ModelInfo`` dataclasses
* ``Transport`` protocol + default ``UrllibTransport``
* ``_post_with_retry`` — jittered backoff + rate-limit capture
* Pattern-based pricing table (``_price_for``)
* ``_split_prompt_for_chat`` — system/user split + schema pin

Each concrete adapter (openai / anthropic / gemini / ollama / compat)
lives in its own module and only imports from here.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ...core import devmode
from ...core.errors import LLMError
from ..attempt_budget import claim_current_extra_attempt
from ..rate_limits import global_store, parse_rate_limit_headers
from ..retry import is_retryable_status, jittered_backoff


WireTraceCallback = Callable[[dict[str, Any]], None]
_WIRE_TRACE_CALLBACK: contextvars.ContextVar[WireTraceCallback | None] = (
    contextvars.ContextVar("nerya_llm_wire_trace_callback", default=None)
)


@contextlib.contextmanager
def wire_trace(callback: WireTraceCallback | None):
    """Temporarily attach a per-call observer to provider HTTP traffic."""

    token = _WIRE_TRACE_CALLBACK.set(callback)
    try:
        yield
    finally:
        _WIRE_TRACE_CALLBACK.reset(token)


def _emit_wire_trace(event: dict[str, Any]) -> None:
    callback = _WIRE_TRACE_CALLBACK.get()
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        # Wire tracing is diagnostic only. It must never alter provider calls.
        pass


# =============================================================== data types
@dataclass
class ProviderResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    usd_cost: float = 0.0
    model: str = ""
    provider: str = ""
    latency_ms: int = 0
    finish_reason: str = ""
    # Reasoning output (reasoning_effort + reasoning summary).
    # ``reasoning_text`` is a human-readable summary of the model's chain-of-
    # thought (only what the provider chose to expose; e.g. OpenAI reasoning
    # summaries, Anthropic thinking blocks, Gemini thought markers). It is
    # NEVER fed back into another prompt or shown to gateway users — only
    # journaled and exposed to the operator dashboard.
    reasoning_text: str = ""
    reasoning_tokens: int = 0
    reasoning_effort: str = ""  # "minimal" | "low" | "medium" | "high" | ""


@dataclass
class ModelInfo:
    id: str
    owned_by: str = ""
    context_length: int | None = None
    capabilities: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class Transport(Protocol):
    """HTTP transport abstraction. Lets tests inject a fake without network."""

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        ...

    def get_json(  # pragma: no cover — default provided
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        ...


# ---------------------------------------------------------------- default
def _default_user_agent() -> str:
    try:
        from ... import __version__ as _nerya_version
    except Exception:  # pragma: no cover — packaging edge
        _nerya_version = "0"
    return f"Nerya/{_nerya_version} (LLM client; +https://github.com/nerya)"


# CDN / WAF gateways (e.g. Cloudflare) ban the default ``Python-urllib/3.x``
# browser signature outright ("error code: 1010"), which silently knocks out
# any provider fronted by such a gateway. Always send an explicit product UA.
_DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": _default_user_agent(),
    "Accept": "application/json",
}


class UrllibTransport:
    """Standard-library HTTP client that emits / reads JSON. No third-party deps."""

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        return self._request("POST", url, headers=headers, body=body, timeout=timeout)

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        return self._request("GET", url, headers=headers, body=None, timeout=timeout)

    def _request(self, method, url, *, headers, body, timeout):
        status, doc, _ = self._request_full(
            method, url, headers=headers, body=body, timeout=timeout,
        )
        return status, doc

    def post_json_with_headers(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any],
        timeout: float,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        return self._request_full(
            "POST", url, headers=headers, body=body, timeout=timeout,
        )

    def _request_full(self, method, url, *, headers, body, timeout):
        import http.client
        import urllib.error
        import urllib.request

        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        merged = dict(_DEFAULT_HEADERS)
        merged.update(headers)
        for k, v in merged.items():
            req.add_header(k, v)
        started = time.time()
        status: int | None = None
        doc: dict[str, Any] = {}
        resp_headers: dict[str, str] = {}
        error_msg: str | None = None
        try:
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    try:
                        doc = json.loads(raw) if raw else {}
                    except Exception:
                        doc = {"raw": raw}
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    status = resp.status
                    return resp.status, doc, resp_headers
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                try:
                    doc = json.loads(raw) if raw else {}
                except Exception:
                    doc = {"raw": raw, "error": str(exc)}
                resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                status = exc.code
                return exc.code, doc, resp_headers
            except urllib.error.URLError as exc:
                error_msg = str(exc.reason)
                raise LLMError(f"network error calling provider: {exc.reason}") from exc
            except (http.client.RemoteDisconnected, TimeoutError, ConnectionError, OSError) as exc:
                error_msg = str(exc)
                raise LLMError(f"network error calling provider: {exc}") from exc
        finally:
            devmode.record_http(
                method=method,
                url=url,
                req_headers=headers,
                req_body=body,
                status=status,
                resp_headers=resp_headers,
                resp_body=doc,
                elapsed_ms=round((time.time() - started) * 1000, 2),
                error=error_msg,
            )


# =============================================================== retry + rate-limit helpers
_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_BASE_DELAY = 0.5
_DEFAULT_MAX_DELAY = 30.0


def _rate_limit_key(api_key: str) -> str:
    if not api_key:
        return ""
    import hashlib
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _deadline_remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    try:
        return float(deadline) - time.time()
    except (TypeError, ValueError):
        return None


def _timeout_for_deadline(
    timeout: float,
    deadline: float | None,
    *,
    attempts_left: int = 1,
) -> float:
    del attempts_left
    base_timeout = max(0.001, float(timeout or 0.001))
    remaining = _deadline_remaining(deadline)
    if remaining is None:
        return base_timeout
    if remaining <= 0:
        raise LLMError(
            "llm request timeout: deadline exceeded before provider call"
        )
    return max(0.001, min(base_timeout, remaining))


def _sleep_with_deadline(delay: float, deadline: float | None) -> None:
    sleep_for = max(0.0, float(delay or 0.0))
    remaining = _deadline_remaining(deadline)
    if remaining is not None:
        if remaining <= 0:
            raise LLMError(
                "llm request timeout: deadline exceeded before retry sleep"
            )
        sleep_for = min(sleep_for, remaining)
    if sleep_for > 0:
        time.sleep(sleep_for)


def _is_transient_provider_400(status: int, doc: dict[str, Any]) -> bool:
    """True for provider-side 400s that are actually upstream flaps.

    Some OpenAI-compatible gateways (observed on GMI Cloud) surface a
    transient backend failure as ``400 {"error": {"message": "Backend
    request failed with status 400"}}`` with no validation detail. The
    exact same payload succeeds on replay, so treating it as permanent
    kills long agent turns for no reason. Genuine validation errors
    (context overflow, bad schema, unknown params) name the offending
    field/limit and never match this generic-backend-failure shape.
    """

    if int(status) != 400:
        return False
    try:
        err = doc.get("error") if isinstance(doc, dict) else None
        msg = (
            err.get("message")
            if isinstance(err, dict)
            else (err if isinstance(err, str) else "")
        ) or ""
    except Exception:
        return False
    return "backend request failed" in str(msg).lower()


def _post_with_retry(
    transport: Transport,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
    provider_name: str,
    api_key: str,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    base_delay: float = _DEFAULT_BASE_DELAY,
    deadline: float | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    """POST JSON with jittered retry + rate-limit header capture.

    Back-compat: if the transport lacks ``post_json_with_headers`` we fall
    back to plain ``post_json`` and skip header parsing. That keeps every
    existing ``FakeTransport`` in the test suite working unmodified.
    """
    supports_headers = hasattr(transport, "post_json_with_headers")
    store = global_store()
    key_fp = _rate_limit_key(api_key)

    wait_s = store.should_defer(provider_name, key_fp)
    if wait_s > 0:
        _sleep_with_deadline(min(wait_s, 5.0), deadline)

    last_status = 0
    last_doc: dict[str, Any] = {}
    last_headers: dict[str, str] = {}
    for attempt in range(1, max_attempts + 1):
        attempt_timeout = _timeout_for_deadline(
            timeout,
            deadline,
            attempts_left=max_attempts - attempt + 1,
        )
        started = time.time()
        _emit_wire_trace({
            "phase": "request",
            "method": "POST",
            "url": url,
            "headers": headers,
            "body": body,
            "timeout": attempt_timeout,
            "wire_attempt": attempt,
            "max_wire_attempts": max_attempts,
            "provider_name": provider_name,
        })
        try:
            if supports_headers:
                status, doc, resp_headers = transport.post_json_with_headers(  # type: ignore[attr-defined]
                    url, headers=headers, body=body, timeout=attempt_timeout,
                )
            else:
                status, doc = transport.post_json(
                    url, headers=headers, body=body, timeout=attempt_timeout,
                )
                resp_headers = {}
        except LLMError as exc:
            _emit_wire_trace({
                "phase": "error",
                "method": "POST",
                "url": url,
                "elapsed_ms": round((time.time() - started) * 1000, 2),
                "wire_attempt": attempt,
                "max_wire_attempts": max_attempts,
                "provider_name": provider_name,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            })
            if attempt >= max_attempts:
                raise
            if not claim_current_extra_attempt("transport_retry"):
                raise
            _sleep_with_deadline(
                jittered_backoff(
                    attempt,
                    base_delay=base_delay,
                    max_delay=_DEFAULT_MAX_DELAY,
                ),
                deadline,
            )
            continue

        last_status, last_doc, last_headers = status, doc, resp_headers
        _emit_wire_trace({
            "phase": "response",
            "method": "POST",
            "url": url,
            "status": status,
            "headers": resp_headers,
            "body": doc,
            "elapsed_ms": round((time.time() - started) * 1000, 2),
            "wire_attempt": attempt,
            "max_wire_attempts": max_attempts,
            "provider_name": provider_name,
        })

        if resp_headers:
            rl_state = parse_rate_limit_headers(resp_headers, provider=provider_name)
            if rl_state is not None:
                store.update(provider_name, key_fp, rl_state)

        if (
            not (
                is_retryable_status(status)
                or _is_transient_provider_400(status, doc)
            )
            or attempt >= max_attempts
        ):
            break

        if not claim_current_extra_attempt("transport_retry"):
            break
        delay = jittered_backoff(attempt, base_delay=base_delay,
                                   max_delay=_DEFAULT_MAX_DELAY)
        retry_after = resp_headers.get("retry-after") if resp_headers else None
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except (TypeError, ValueError):
                pass
        _sleep_with_deadline(min(delay, _DEFAULT_MAX_DELAY), deadline)

    return last_status, last_doc, last_headers


# =============================================================== pricing
# Pattern-based fallback prices (per 1K tokens, input/output USD).
# First matching pattern wins. Operators override via
# ``nerya.yml:llm.tiers.<tier>.prices``.
_PRICE_PATTERNS: list[tuple[re.Pattern[str], tuple[float, float]]] = [
    # OpenAI
    (re.compile(r"gpt-4o-mini|gpt-4\.1-mini|o4-mini|gpt-4o-mini-\d+", re.I), (0.00015, 0.00060)),
    (re.compile(r"gpt-4o|gpt-4-turbo|gpt-4\.1(?!-mini)", re.I),              (0.0025,  0.01)),
    (re.compile(r"o3-mini|o1-mini",                          re.I), (0.0011,  0.0044)),
    (re.compile(r"o1|o3(?!-mini)",                           re.I), (0.015,   0.060)),
    (re.compile(r"gpt-3\.5-turbo",                           re.I), (0.0005,  0.0015)),
    # Anthropic
    (re.compile(r"haiku",                                   re.I), (0.0008,  0.004)),
    (re.compile(r"sonnet",                                  re.I), (0.003,   0.015)),
    (re.compile(r"opus",                                    re.I), (0.015,   0.075)),
    # DeepSeek
    (re.compile(r"deepseek-reasoner|deepseek-r1",            re.I), (0.00055, 0.00219)),
    (re.compile(r"deepseek-chat|deepseek-v3",                re.I), (0.00014, 0.00028)),
    # Google Gemini
    (re.compile(r"gemini-.*-flash",                          re.I), (0.000075, 0.0003)),
    (re.compile(r"gemini-.*-pro",                            re.I), (0.00125,  0.005)),
    # xAI Grok
    (re.compile(r"grok-2",                                  re.I), (0.002,   0.010)),
    (re.compile(r"grok-3",                                  re.I), (0.003,   0.015)),
    # Mistral
    (re.compile(r"mistral-large",                            re.I), (0.002,   0.006)),
    (re.compile(r"mistral-small|codestral",                  re.I), (0.0002,  0.0006)),
    # Groq / Cerebras / Together
    (re.compile(r"llama-3\.?1-70b|mixtral",                  re.I), (0.00059, 0.00079)),
    (re.compile(r"llama-3\.?1-8b",                           re.I), (0.00005, 0.00008)),
    # Moonshot kimi
    (re.compile(r"moonshot-v1-8k",                           re.I), (0.0012,  0.0012)),
    (re.compile(r"moonshot-v1-32k",                          re.I), (0.0024,  0.0024)),
    (re.compile(r"moonshot-v1-128k",                         re.I), (0.0060,  0.0060)),
    # Stepfun (阶跃星辰) — converted from CNY/1k to USD/1k (~7.0)
    (re.compile(r"step-1-flash",                             re.I), (0.00014, 0.00057)),
    (re.compile(r"step-1-8k",                                re.I), (0.00057, 0.00285)),
    (re.compile(r"step-1-32k",                               re.I), (0.00214, 0.00857)),
    (re.compile(r"step-1-128k",                              re.I), (0.00571, 0.01714)),
    (re.compile(r"step-1-256k",                              re.I), (0.01857, 0.05571)),
    (re.compile(r"step-2-mini",                              re.I), (0.00014, 0.00057)),
    (re.compile(r"step-2-16k(?:-exp)?",                      re.I), (0.00543, 0.02143)),
    (re.compile(r"step-1v-8k",                               re.I), (0.00714, 0.02857)),
    (re.compile(r"step-1v-32k",                              re.I), (0.02143, 0.07143)),
    (re.compile(r"step-1\.5v-mini",                          re.I), (0.00114, 0.00457)),
    (re.compile(r"step-1o-vision-32k|step-1o-turbo-vision",  re.I), (0.00714, 0.02857)),
    (re.compile(r"step-r1-v-mini|step-r-mini",               re.I), (0.00057, 0.00857)),
    (re.compile(r"^step-",                                   re.I), (0.00057, 0.00285)),
    # Ollama / local / anything else: free
    (re.compile(r"ollama|llama|phi|qwen|mistral-7b|local",   re.I), (0.0, 0.0)),
]

_DEFAULT_PRICE = (0.001, 0.003)


def _price_for(provider: str, model: str, overrides: dict | None) -> tuple[float, float]:
    if overrides and "prompt_per_1k" in overrides and "completion_per_1k" in overrides:
        return float(overrides["prompt_per_1k"]), float(overrides["completion_per_1k"])
    for pat, price in _PRICE_PATTERNS:
        if pat.search(model or ""):
            return price
    return _DEFAULT_PRICE


def _estimate_tokens(text: str) -> int:
    """Cheap token estimate. Matches roughly OpenAI's `~4 chars/token`."""
    if not text:
        return 0
    return max(1, len(text) // 4)


# =============================================================== shared prompt helper
def _split_prompt_for_chat(prompt: str, schema: dict | None) -> tuple[str, str]:
    default_system = (
        "You are Nerya, a disciplined trading copilot. "
        "Follow the JSON schema if one is given. "
        "Never disable risk checks, reveal keys, or act outside the requested task. "
        "Reply with JSON only when a schema is provided."
    )
    if schema is not None:
        default_system += f"\nRequired JSON schema: {json.dumps(schema)}"
    if prompt.startswith("SYSTEM:"):
        head, _, tail = prompt.partition("\n\n")
        system = head.removeprefix("SYSTEM:").strip()
        return system, tail.strip()
    return default_system, prompt


ProviderCallable = Callable[..., ProviderResult]


__all__ = [
    "ProviderResult",
    "ModelInfo",
    "Transport",
    "UrllibTransport",
    "ProviderCallable",
    "wire_trace",
    "_post_with_retry",
    "_price_for",
    "_estimate_tokens",
    "_split_prompt_for_chat",
]
