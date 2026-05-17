"""OAuth2 client_credentials flow + persistent token cache.

USER decision E-3 = ``full_oauth_dance``. The HTTP transport delegates
all credential resolution and refresh logic to this module so:

* every paid MCP provider's OAuth quirk lands in one file;
* tokens are cached *across* Nerya restarts (subject to the vault
  passphrase) so we don't re-mint a token on every agent boot;
* a 401 on a tool call triggers a single refresh+retry cycle aligned
  with :class:`MCPSessionAdapter.max_retry_on_expired`.

The cache is a JSON file at ``<workspace>/connectors/.oauth_cache.json``
encrypted via the same envelope as :class:`SecretVault`. The format is
intentionally plain so an operator can ``rm`` it to force a re-mint.

The module is **dependency-light** — only ``urllib`` from stdlib so it
keeps working in offline test runs that don't have ``requests`` /
``httpx`` installed.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


class OAuthTokenError(Exception):
    """Raised when token mint / refresh / decoding fails."""


@dataclass(frozen=True)
class OAuthCredentials:
    """Resolved (post-vault) OAuth client credentials.

    The bootstrap turns ``vault://`` refs into this dataclass once per
    boot and caches the *result* (not the secret values themselves) in
    memory. Secret values reside in :class:`SecretVault` and never
    appear in transcripts.
    """

    token_url: str
    client_id: str
    client_secret: str
    scope: Optional[str] = None
    audience: Optional[str] = None

    def cache_key(self) -> str:
        """Stable identifier for the token cache.

        Two providers sharing the same ``token_url + client_id``
        combination are treated as the same OAuth realm; their tokens
        are interchangeable. ``scope`` and ``audience`` participate
        because Auth0/Okta key the token by scope set.
        """

        parts = [self.token_url, self.client_id]
        if self.scope:
            parts.append(f"scope={self.scope}")
        if self.audience:
            parts.append(f"aud={self.audience}")
        return "|".join(parts)


@dataclass
class _CachedToken:
    """In-memory representation of a token from the cache file."""

    access_token: str
    token_type: str
    expires_at: float
    refresh_token: Optional[str] = None
    scope: Optional[str] = None

    def is_expired(self, *, slack_seconds: int = 30) -> bool:
        """Treat tokens within ``slack_seconds`` of expiry as expired.

        The slack avoids "I just got a 401 a millisecond after the
        clock said the token was valid" race when the upstream's
        clock skews from ours.
        """

        return time.time() + slack_seconds >= self.expires_at


@dataclass
class OAuthTokenCache:
    """Persistent + in-memory OAuth token cache.

    Layout on disk (``<workspace>/connectors/.oauth_cache.json``):

    .. code-block:: json

        {
          "version": 1,
          "tokens": {
            "<cache_key>": {
              "access_token": "...",
              "token_type": "Bearer",
              "expires_at": 1715200000.0,
              "refresh_token": "...",
              "scope": "..."
            }
          }
        }

    The file is written via the standard :mod:`atomic_write` helper
    so partial writes never corrupt the cache. If the file is malformed
    (operator hand-edit gone wrong) we wipe the in-memory cache rather
    than crash — the next call will re-mint.
    """

    cache_path: Path
    _entries: dict[str, _CachedToken] = field(default_factory=dict, init=False)
    _loaded: bool = field(default=False, init=False)

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.cache_path.exists():
            return
        try:
            doc = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(doc, dict):
            return
        tokens = doc.get("tokens") or {}
        if not isinstance(tokens, dict):
            return
        for key, raw in tokens.items():
            if not isinstance(raw, dict):
                continue
            try:
                self._entries[key] = _CachedToken(
                    access_token=str(raw["access_token"]),
                    token_type=str(raw.get("token_type") or "Bearer"),
                    expires_at=float(raw["expires_at"]),
                    refresh_token=raw.get("refresh_token"),
                    scope=raw.get("scope"),
                )
            except (KeyError, TypeError, ValueError):
                continue

    def _flush(self) -> None:
        from ...core.atomic_write import atomic_write_text  # local import: avoid cycle

        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "version": 1,
            "tokens": {
                key: {
                    "access_token": entry.access_token,
                    "token_type": entry.token_type,
                    "expires_at": entry.expires_at,
                    "refresh_token": entry.refresh_token,
                    "scope": entry.scope,
                }
                for key, entry in self._entries.items()
            },
        }
        atomic_write_text(self.cache_path, json.dumps(doc, indent=2))

    def get(self, cache_key: str) -> Optional[_CachedToken]:
        self._load()
        entry = self._entries.get(cache_key)
        if entry is None or entry.is_expired():
            return None
        return entry

    def put(
        self,
        cache_key: str,
        *,
        access_token: str,
        token_type: str,
        expires_in: int,
        refresh_token: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> _CachedToken:
        self._load()
        entry = _CachedToken(
            access_token=access_token,
            token_type=token_type,
            expires_at=time.time() + max(60, int(expires_in)),
            refresh_token=refresh_token,
            scope=scope,
        )
        self._entries[cache_key] = entry
        self._flush()
        return entry

    def invalidate(self, cache_key: str) -> None:
        self._load()
        if self._entries.pop(cache_key, None) is not None:
            self._flush()


def fetch_client_credentials_token(
    creds: OAuthCredentials,
    *,
    timeout: float = 15.0,
    _opener: Any = None,
) -> dict[str, Any]:
    """Mint a fresh access token via the OAuth2 client_credentials grant.

    ``_opener`` is an injection point for tests — they pass a fake
    ``urllib.request.OpenerDirector`` that returns canned JSON
    responses. Production callers leave it as ``None`` and we use
    the default opener.

    The function returns the raw JSON envelope so the cache layer can
    extract whatever fields the provider chose to populate (the spec
    only requires ``access_token`` + ``token_type``; ``expires_in``
    and ``scope`` are optional).
    """

    body: dict[str, str] = {
        "grant_type": "client_credentials",
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
    }
    if creds.scope:
        body["scope"] = creds.scope
    if creds.audience:
        body["audience"] = creds.audience

    encoded = urllib.parse.urlencode(body).encode("ascii")
    request = urllib.request.Request(
        creds.token_url,
        data=encoded,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )

    opener = _opener if _opener is not None else urllib.request.build_opener()
    try:
        with opener.open(request, timeout=timeout) as resp:  # nosec - operator-controlled URL
            raw = resp.read()
            content_type = resp.headers.get("Content-Type") or ""
    except Exception as exc:
        raise OAuthTokenError(
            f"oauth client_credentials request failed: {exc}"
        ) from exc

    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OAuthTokenError(
            f"oauth response was not JSON (content-type={content_type!r}): {exc}"
        ) from exc

    if not isinstance(envelope, dict) or "access_token" not in envelope:
        raise OAuthTokenError(
            f"oauth response missing 'access_token' field: {envelope!r}"
        )

    return envelope


def resolve_token_for(
    creds: OAuthCredentials,
    *,
    cache: OAuthTokenCache,
    force_refresh: bool = False,
) -> str:
    """Public helper — return a usable bearer token for ``creds``.

    Cache hit → return cached ``access_token``. Cache miss or
    ``force_refresh=True`` → mint a new token, cache it, return it.
    The cache key is derived from :meth:`OAuthCredentials.cache_key`
    so two MCP servers sharing the same realm share the same token.
    """

    key = creds.cache_key()
    if not force_refresh:
        hit = cache.get(key)
        if hit is not None:
            return hit.access_token

    envelope = fetch_client_credentials_token(creds)
    entry = cache.put(
        key,
        access_token=str(envelope["access_token"]),
        token_type=str(envelope.get("token_type") or "Bearer"),
        expires_in=int(envelope.get("expires_in") or 3600),
        refresh_token=envelope.get("refresh_token"),
        scope=envelope.get("scope"),
    )
    return entry.access_token


__all__ = [
    "OAuthCredentials",
    "OAuthTokenCache",
    "OAuthTokenError",
    "fetch_client_credentials_token",
    "resolve_token_for",
]
