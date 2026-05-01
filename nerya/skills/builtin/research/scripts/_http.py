"""Tiny stdlib HTTP helper shared by the research scripts.

Kept inside the skill folder (rather than reusing the legacy
``websearch_skill`` helper) so the new ``research`` skill stays
self-contained as the legacy bridge is dismantled. The implementation
mirrors the previous ``websearch_skill.scripts.handlers``
just without the ``ctx`` plumbing.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 15.0
HARD_FETCH_BYTES = 4_000_000


def http_get(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    method: str = "GET",
    form: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
    user_agent: str = DEFAULT_UA,
) -> tuple[int, dict[str, str], bytes]:
    """Issue a single HTTP request. Returns ``(status, headers, body)``.

    Body is capped at ``HARD_FETCH_BYTES + 1`` so an oversized response
    is still detectable (callers slice further) without blowing up
    memory.
    """

    headers = {
        "User-Agent": user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5"
        ),
        "Accept-Language": "en-US,en;q=0.8",
    }
    if extra_headers:
        headers.update(extra_headers)
    body_bytes: bytes | None = None
    if form is not None:
        body_bytes = urllib.parse.urlencode(form).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, headers=headers, data=body_bytes, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            headers_out = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read(HARD_FETCH_BYTES + 1)
            return status, headers_out, body
    except urllib.error.HTTPError as e:
        try:
            body = e.read(HARD_FETCH_BYTES + 1)
        except Exception:
            body = b""
        return e.code, dict(e.headers or {}), body


__all__ = [
    "DEFAULT_TIMEOUT",
    "DEFAULT_UA",
    "HARD_FETCH_BYTES",
    "http_get",
]
