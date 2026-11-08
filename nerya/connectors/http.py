"""HTTP transport for CEX / DEX connectors.

Same Transport protocol as nerya.llm.providers but used for REST calls.
Supports GET and POST with arbitrary params/headers/body. A lightweight
rate-limiter is included for polite public-endpoint usage.
"""

from __future__ import annotations

import json
import logging
import math
import time
from io import BytesIO
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from ..core import devmode
from ..core.errors import TradingError
from ..harness.cancellation import raise_if_cancelled


log = logging.getLogger(__name__)


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        body: Any = None,
        timeout: float = 15.0,
    ) -> tuple[int, dict[str, Any]]:
        ...


_DEFAULT_USER_AGENT = (
    # Many public feeds (e.g. coindesk, cointelegraph, bitcoinmagazine)
    # block the bare "Python-urllib/3.x" User-Agent with 403s, which
    # forces the news pipeline into degraded mode even when the
    # network is fine. We advertise a real-ish UA so those feeds let
    # us in, while still identifying ourselves as Nerya so operators
    # of those feeds can reach us if needed.
    "Mozilla/5.0 (compatible; Nerya/1.0; "
    "+https://github.com/nerya-labs/nerya)"
)


@dataclass
class UrllibHttp:
    """Standard-library HTTP client. No third-party deps required."""
    rate_limit_per_sec: float = 8.0
    user_agent: str = _DEFAULT_USER_AGENT
    max_response_bytes: int | None = None
    allowed_origins: tuple[str, ...] | None = None
    strict_json: bool = False
    _last_ts: float = field(default=0.0, init=False)

    def _check_origin(self, url: str) -> None:
        if self.allowed_origins is None:
            return
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.username or parts.password
                or f"{parts.scheme}://{parts.netloc}" not in self.allowed_origins):
            raise ValueError("HTTP origin is not allowlisted")

    def _read_json(self, response: Any) -> Any:
        raw = (response.read() if self.max_response_bytes is None
               else response.read(self.max_response_bytes + 1))
        if self.max_response_bytes is not None and len(raw) > self.max_response_bytes:
            raise ValueError("HTTP response exceeds byte limit")
        text = raw.decode("utf-8", errors="strict" if self.strict_json else "replace")
        if self.strict_json:
            def finite_number(value: str) -> float:
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError("Non-finite JSON number")
                return number

            def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate JSON key")
                    result[key] = value
                return result

            return json.loads(text, parse_constant=finite_number,
                              parse_float=finite_number, object_pairs_hook=unique_keys)
        try:
            return json.loads(text) if text else {}
        except Exception:
            return {"raw": text}

    def _wait_rate(self) -> None:
        if self.rate_limit_per_sec <= 0:
            return
        min_gap = 1.0 / self.rate_limit_per_sec
        dt = time.time() - self._last_ts
        if dt < min_gap:
            time.sleep(min_gap - dt)
        self._last_ts = time.time()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        body: Any = None,
        timeout: float = 15.0,
    ) -> tuple[int, dict[str, Any]]:
        import urllib.request
        import urllib.error

        self._wait_rate()
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urlencode(params)
        self._check_origin(url)
        open_url = urllib.request.urlopen
        if self.allowed_origins is not None:
            check_origin = self._check_origin

            class CheckedRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    check_origin(newurl)
                    return super().redirect_request(req, fp, code, msg, headers, newurl)

            open_url = urllib.request.build_opener(CheckedRedirect()).open
        data: bytes | None = None
        h = dict(headers or {})
        # Only advertise our UA when the caller did not already set one.
        # Exchange/market adapters often pin their own UA for audit.
        if self.user_agent and not any(k.lower() == "user-agent" for k in h):
            h["User-Agent"] = self.user_agent
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                data = bytes(body)
            else:
                data = json.dumps(body).encode("utf-8")
                h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, method=method.upper())
        for k, v in h.items():
            req.add_header(k, v)
        started = time.time()
        status: int | None = None
        doc: dict[str, Any] = {}
        resp_headers: dict[str, str] = {}
        error: str | None = None
        try:
            try:
                with open_url(req, timeout=timeout) as resp:
                    doc = self._read_json(resp)
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    status = resp.status
                    return resp.status, doc
            except urllib.error.HTTPError as exc:
                try:
                    doc = self._read_json(exc)
                finally:
                    exc.close()
                resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                status = exc.code
                return exc.code, doc
            except urllib.error.URLError as exc:
                error = str(exc.reason)
                raise TradingError(f"network error: {exc.reason}") from exc
        finally:
            self._record_http(
                method=method.upper(),
                url=url,
                req_headers=h,
                req_body=body,
                status=status,
                resp_headers=resp_headers,
                resp_body=doc,
                elapsed_ms=round((time.time() - started) * 1000, 2),
                error=error,
            )

    @staticmethod
    def _record_http(**record):
        devmode.record_http(**record)


class KeepAliveHttp(UrllibHttp):
    """Run-local sequential public RPC connection pool; no cookie/session state."""

    def __init__(self, *, deadline, cancel_token=None, **kwargs):
        super().__init__(**kwargs)
        import httpx
        self.deadline, self.cancel_token = deadline, cancel_token
        self.client = httpx.Client(trust_env=False, follow_redirects=False,
            transport=httpx.HTTPTransport(retries=0, trust_env=False,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)),
            event_hooks={'request': [lambda request: request.headers.pop('cookie', None)]})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def request(self, method, url, *, headers=None, params=None, body=None, timeout=15.0):
        from urllib.parse import urljoin
        self._wait_rate()
        if params:
            url += ('&' if '?' in url else '?') + urlencode(params)
        h = dict(headers or {})
        if self.user_agent and not any(k.lower() == 'user-agent' for k in h):
            h['User-Agent'] = self.user_agent
        data = bytes(body) if isinstance(body, (bytes, bytearray)) else (
            json.dumps(body).encode('utf-8') if body is not None else None)
        if body is not None and not isinstance(body, (bytes, bytearray)):
            h.setdefault('Content-Type', 'application/json')
        started = time.monotonic()
        end = min(self.deadline, started + timeout)
        status, doc, response_headers, error = None, {}, {}, None
        try:
            for _ in range(10):
                raise_if_cancelled(self.cancel_token)
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('HTTP deadline exhausted')
                self._check_origin(url)
                self.client.cookies.clear()
                try:
                    with self.client.stream(method, url, headers=h, content=data, timeout=remaining) as response:
                        status, response_headers = response.status_code, dict(response.headers)
                        self.client.cookies.clear()
                        if status in (301, 302, 303, 307, 308) and 'location' in response.headers:
                            url = urljoin(url, response.headers['location'])
                            self._check_origin(url)
                            if status == 303 and method.upper() != 'HEAD':
                                method, data = 'GET', None
                            continue
                        raw = bytearray()
                        for chunk in response.iter_bytes():
                            raise_if_cancelled(self.cancel_token)
                            if time.monotonic() >= end:
                                raise TimeoutError('HTTP deadline exhausted')
                            if self.max_response_bytes is not None and len(raw) + len(chunk) > self.max_response_bytes:
                                raise ValueError('HTTP response exceeds byte limit')
                            raw.extend(chunk)
                        raise_if_cancelled(self.cancel_token)
                        if time.monotonic() >= end:
                            raise TimeoutError('HTTP deadline exhausted')
                        doc = self._read_json(BytesIO(raw))
                        return status, doc
                finally:
                    self.client.cookies.clear()
            raise ValueError('HTTP redirect limit exceeded')
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            self._record_http(method=method.upper(), url=url, req_headers=h, req_body=body,
                status=status, resp_headers=response_headers, resp_body=doc,
                elapsed_ms=round((time.monotonic() - started) * 1000, 2), error=error)


__all__ = ["HttpTransport", "UrllibHttp", "KeepAliveHttp"]
