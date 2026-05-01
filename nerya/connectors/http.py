"""HTTP transport for CEX / DEX connectors.

Same Transport protocol as nerya.llm.providers but used for REST calls.
Supports GET and POST with arbitrary params/headers/body. A lightweight
rate-limiter is included for polite public-endpoint usage.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

from ..core import devmode
from ..core.errors import TradingError


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
    _last_ts: float = field(default=0.0, init=False)

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
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    try:
                        doc = json.loads(raw) if raw else {}
                    except Exception:
                        doc = {"raw": raw}
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    status = resp.status
                    return resp.status, doc
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                try:
                    doc = json.loads(raw) if raw else {}
                except Exception:
                    doc = {"raw": raw}
                resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                status = exc.code
                return exc.code, doc
            except urllib.error.URLError as exc:
                error = str(exc.reason)
                raise TradingError(f"network error: {exc.reason}") from exc
        finally:
            devmode.record_http(
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


__all__ = ["HttpTransport", "UrllibHttp"]
