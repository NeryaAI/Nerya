"""backend browser / web-safety stack.

This module is the safety layer that *every* outbound URL in Nerya
should pass through before we fetch it (news, social, browser tools,
trigger payloads, dashboard previews, MCP servers, etc.).

It is intentionally browser-driver-agnostic — no Selenium, CDP,
playwright, or HTTP client lives here.  The module returns structured
decisions; the caller decides whether to fetch.

Key decisions
-------------

* **Block private/loopback/link-local hosts by default.** Outbound
  agent traffic must not pull from `127.0.0.1`, `10.*`, `192.168.*`,
  IPv6 link-local, or `*.local` unless the operator has explicitly
  added the host to the workspace allowlist.
* **Strip credentials in URLs.** ``https://user:pass@example.com``
  is a textbook prompt-injection / credential-exfil vector.  We refuse
  to fetch URLs that carry userinfo.
* **Disallow non-http(s) schemes.** ``file://``, ``ftp://``, ``data:``,
  ``javascript:`` etc. are blocked.
* **Optional allow/deny lists.** Operators can pin policy via
  ``workspace/security/web_policy.yml`` (or by passing a
  :class:`WebPolicy` object directly).
* **Citation hygiene.** A single helper trims/escapes a remote
  document down to a short, attribution-bearing snippet so we never
  paste an entire page into context.

The module only depends on stdlib (``urllib.parse``, ``ipaddress``,
``re``); no third-party browser libraries are pulled in.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional
from urllib.parse import unquote, urlparse, urlunparse

from ..core import yaml_io
from ..core.errors import SecurityError


# --------------------------------------------------------------------- #
# Data shapes
# --------------------------------------------------------------------- #


class WebSafetyError(SecurityError):
    """A URL/website was rejected by the web-safety layer."""


VERDICT_ALLOW = "allow"
VERDICT_DENY = "deny"
VERDICT_WARN = "warn"


# Reasons returned in :class:`Decision.reason` so dashboard / gateway
# can localise / theme the rejection without parsing the message.
REASON_OK = "ok"
REASON_INVALID_URL = "invalid_url"
REASON_BAD_SCHEME = "bad_scheme"
REASON_USERINFO = "userinfo_in_url"
REASON_PRIVATE_HOST = "private_host"
REASON_LOOPBACK = "loopback"
REASON_LINK_LOCAL = "link_local"
REASON_DENY_LIST = "deny_list"
REASON_NOT_ALLOWED = "not_allowed"
REASON_CREDENTIAL_KEYWORD = "credential_keyword"
REASON_TOO_LARGE = "too_large"


# Default scheme allowlist — only http(s) by default.  An operator can
# extend this via :class:`WebPolicy.allowed_schemes`.
DEFAULT_SCHEMES = ("http", "https")


# Common substrings that look like an embedded secret in a URL query.
# The runtime's `web_tools.py` blocks the same family of substrings before
# fetching; we keep the same safeguard.
_SECRET_QUERY_KEYS = (
    "access_token", "id_token", "refresh_token", "api_key",
    "apikey", "secret", "password", "client_secret",
    "authorization", "auth_token",
)


@dataclass
class Decision:
    """Verdict for a single URL.

    The verdict is one of :data:`VERDICT_ALLOW`, :data:`VERDICT_DENY`,
    :data:`VERDICT_WARN`.  ``url`` is the canonicalised URL (after we
    stripped userinfo if any).  ``reason`` matches one of the
    ``REASON_*`` constants for machine-readable handling.
    """

    url: str
    verdict: str
    reason: str = REASON_OK
    note: str = ""
    notes: List[str] = field(default_factory=list)
    host: str = ""

    def is_allowed(self) -> bool:
        return self.verdict == VERDICT_ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "verdict": self.verdict,
            "reason": self.reason,
            "note": self.note,
            "notes": list(self.notes),
            "host": self.host,
            "allowed": self.is_allowed(),
        }


@dataclass
class WebPolicy:
    """Operator-controlled allow/deny configuration.

    The defaults are deliberately conservative; loading is opt-in via
    :meth:`load_from_file` so test/CI environments don't accidentally
    inherit a developer's local allowlist.
    """

    allow_hosts: List[str] = field(default_factory=list)
    deny_hosts: List[str] = field(default_factory=list)
    allowed_schemes: List[str] = field(default_factory=lambda: list(DEFAULT_SCHEMES))
    allow_private_hosts: bool = False
    max_content_bytes: int = 5 * 1024 * 1024  # 5 MiB hard cap by default

    @classmethod
    def load_from_file(cls, path: Path | str) -> "WebPolicy":
        p = Path(path)
        if not p.exists():
            return cls()
        doc = yaml_io.load(p) or {}
        if not isinstance(doc, dict):
            return cls()
        return cls(
            allow_hosts=[str(h).strip().lower() for h in (doc.get("allow_hosts") or []) if h],
            deny_hosts=[str(h).strip().lower() for h in (doc.get("deny_hosts") or []) if h],
            allowed_schemes=[str(s).strip().lower() for s in (doc.get("allowed_schemes")
                                                              or list(DEFAULT_SCHEMES)) if s],
            allow_private_hosts=bool(doc.get("allow_private_hosts", False)),
            max_content_bytes=int(doc.get("max_content_bytes")
                                  or 5 * 1024 * 1024),
        )


# --------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------- #


def _normalize_host(raw_host: str) -> str:
    h = (raw_host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):  # IPv6 literal
        return h[1:-1]
    return h


def _is_private_host(host: str) -> tuple[bool, str]:
    """Return (is_private, reason).

    Detects loopback, link-local, private (RFC1918 / RFC4193), and the
    ``*.local`` mDNS suffix that some OSes expose.
    """
    h = _normalize_host(host)
    if not h:
        return True, REASON_INVALID_URL
    if h in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return True, REASON_LOOPBACK
    if h.endswith(".local"):
        return True, REASON_LINK_LOCAL
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False, ""
    if ip.is_loopback:
        return True, REASON_LOOPBACK
    if ip.is_link_local:
        return True, REASON_LINK_LOCAL
    if ip.is_private or ip.is_reserved or ip.is_multicast:
        return True, REASON_PRIVATE_HOST
    return False, ""


def _has_credential_keyword(url: str) -> Optional[str]:
    """Return the offending key if a URL carries a credential keyword.

    the runtime blocks anything that looks like ``?api_key=...``.  We do the
    same — we never want a model-emitted URL to drop a token into the
    network.
    """
    decoded = unquote(url).lower()
    for k in _SECRET_QUERY_KEYS:
        # Match ``?key=`` or ``&key=`` to avoid false positives on a
        # legit path component like ``/api_keys/list`` (no "=").
        if re.search(rf"[?&]{re.escape(k)}=", decoded):
            return k
    return None


def _host_in_list(host: str, items: Iterable[str]) -> bool:
    """Match host against a list with optional wildcard prefix.

    ``"example.com"`` matches ``example.com`` exactly; an entry
    ``"*.example.com"`` matches any subdomain of example.com.
    """
    h = _normalize_host(host)
    for raw in items:
        rule = (raw or "").strip().lower()
        if not rule:
            continue
        if rule.startswith("*."):
            suffix = rule[1:]  # ".example.com"
            if h == rule[2:] or h.endswith(suffix):
                return True
        else:
            if h == rule:
                return True
    return False


# --------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------- #


def evaluate_url(url: str, *, policy: Optional[WebPolicy] = None) -> Decision:
    """Return a :class:`Decision` for ``url`` under ``policy``."""
    policy = policy or WebPolicy()
    raw = (url or "").strip()
    if not raw:
        return Decision(url="", verdict=VERDICT_DENY,
                        reason=REASON_INVALID_URL,
                        note="empty url")
    try:
        parsed = urlparse(raw)
    except Exception:
        return Decision(url=raw, verdict=VERDICT_DENY,
                        reason=REASON_INVALID_URL,
                        note="urlparse failed")
    scheme = (parsed.scheme or "").lower()
    if not scheme or not parsed.netloc:
        return Decision(url=raw, verdict=VERDICT_DENY,
                        reason=REASON_INVALID_URL,
                        note="missing scheme or netloc")
    if scheme not in [s.lower() for s in policy.allowed_schemes]:
        return Decision(url=raw, verdict=VERDICT_DENY,
                        reason=REASON_BAD_SCHEME,
                        note=f"scheme {scheme!r} not allowed",
                        host=_normalize_host(parsed.hostname or ""))
    if parsed.username or parsed.password:
        return Decision(url=raw, verdict=VERDICT_DENY,
                        reason=REASON_USERINFO,
                        note="userinfo (user:pass@) not allowed",
                        host=_normalize_host(parsed.hostname or ""))
    host = _normalize_host(parsed.hostname or "")
    if not host:
        return Decision(url=raw, verdict=VERDICT_DENY,
                        reason=REASON_INVALID_URL,
                        note="missing host")

    # Deny list always wins.
    if _host_in_list(host, policy.deny_hosts):
        return Decision(url=raw, verdict=VERDICT_DENY,
                        reason=REASON_DENY_LIST,
                        note=f"host {host!r} on deny list",
                        host=host)

    notes: List[str] = []

    private, private_reason = _is_private_host(host)
    if private and not policy.allow_private_hosts:
        # Allow-listing wins over default block, so check the allowlist
        # first.
        if not _host_in_list(host, policy.allow_hosts):
            return Decision(url=raw, verdict=VERDICT_DENY,
                            reason=private_reason or REASON_PRIVATE_HOST,
                            note=f"host {host!r} is private/loopback",
                            host=host)
        notes.append(f"private host {host!r} permitted by allowlist")
    elif private and policy.allow_private_hosts:
        notes.append(f"private host {host!r} permitted by policy")

    cred_key = _has_credential_keyword(raw)
    if cred_key:
        return Decision(url=raw, verdict=VERDICT_DENY,
                        reason=REASON_CREDENTIAL_KEYWORD,
                        note=f"url carries credential keyword {cred_key!r}",
                        host=host)

    # Allowlist behaviour: if the operator pinned an allowlist, only
    # those hosts (or wildcard-matched hosts) may pass.  Empty
    # allowlist == open.
    if policy.allow_hosts and not _host_in_list(host, policy.allow_hosts):
        return Decision(url=raw, verdict=VERDICT_DENY,
                        reason=REASON_NOT_ALLOWED,
                        note=f"host {host!r} is not in allow_hosts",
                        host=host)

    # Strip empty userinfo if any leftover.
    cleaned = urlunparse((parsed.scheme, parsed.netloc, parsed.path,
                          parsed.params, parsed.query, parsed.fragment))
    return Decision(url=cleaned, verdict=VERDICT_ALLOW,
                    reason=REASON_OK,
                    note="ok",
                    notes=notes,
                    host=host)


def evaluate_urls(urls: Iterable[str], *, policy: Optional[WebPolicy] = None) -> List[Decision]:
    return [evaluate_url(u, policy=policy) for u in urls]


def require_safe_url(url: str, *, policy: Optional[WebPolicy] = None) -> str:
    """Return the canonical url or raise :class:`WebSafetyError`."""
    decision = evaluate_url(url, policy=policy)
    if not decision.is_allowed():
        raise WebSafetyError(f"{decision.reason}: {decision.note} ({decision.url})")
    return decision.url


# --------------------------------------------------------------------- #
# Citation hygiene
# --------------------------------------------------------------------- #


@dataclass
class Citation:
    """A short, attribution-bearing snippet for model context.

    ``snippet`` is bounded by character count so we don't accidentally
    paste an entire page in.  ``source`` keeps the canonical URL the
    snippet was drawn from.
    """

    source: str
    snippet: str
    title: str = ""
    fetched_at: str = ""
    char_count: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "snippet": self.snippet,
            "title": self.title,
            "fetched_at": self.fetched_at,
            "char_count": self.char_count,
            "truncated": self.truncated,
        }


def make_citation(*, source: str, body: str, title: str = "",
                  max_chars: int = 800, fetched_at: str = "",
                  policy: Optional[WebPolicy] = None) -> Citation:
    """Trim ``body`` to ``max_chars`` characters and bind to ``source``.

    The source is sanitised through :func:`require_safe_url` first so
    a citation never points at a private network or credential URL.
    """
    safe_source = require_safe_url(source, policy=policy)
    text = (body or "").strip()
    truncated = False
    if len(text) > max_chars:
        text = text[: max_chars].rstrip() + "…"
        truncated = True
    return Citation(
        source=safe_source,
        snippet=text,
        title=(title or "").strip(),
        fetched_at=fetched_at,
        char_count=len(text),
        truncated=truncated,
    )


__all__ = [
    "Citation",
    "Decision",
    "DEFAULT_SCHEMES",
    "REASON_BAD_SCHEME",
    "REASON_CREDENTIAL_KEYWORD",
    "REASON_DENY_LIST",
    "REASON_INVALID_URL",
    "REASON_LINK_LOCAL",
    "REASON_LOOPBACK",
    "REASON_NOT_ALLOWED",
    "REASON_OK",
    "REASON_PRIVATE_HOST",
    "REASON_TOO_LARGE",
    "REASON_USERINFO",
    "VERDICT_ALLOW",
    "VERDICT_DENY",
    "VERDICT_WARN",
    "WebPolicy",
    "WebSafetyError",
    "evaluate_url",
    "evaluate_urls",
    "make_citation",
    "require_safe_url",
]
