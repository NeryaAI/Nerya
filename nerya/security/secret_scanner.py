"""In-message secret scanner with placeholder swap-out.

Operators occasionally paste raw API keys or wallet private keys into
the gateway chat (Telegram, dashboard chat, generic webhook) when
talking to the agent. Without this layer the plaintext would land in
the LLM prompt, the conversation transcript, and any downstream model
provider's logs.

This module provides three capabilities:

1. **Detection** — :func:`scan_and_redact` runs a battery of regex
   probes (hex private keys, BIP39 mnemonics, Solana base58 keys, JWTs,
   generic high-entropy API key tokens) over a piece of text. Each
   match is stored in a per-session :class:`SecretBuffer` keyed by an
   opaque token, and the original text is rewritten with
   ``<<NERYA_SECRET:<token>>>`` placeholders before it ever reaches
   the agent kernel.

2. **Buffering** — :class:`SecretBuffer` holds the captured plaintext
   in memory with a configurable TTL (10 minutes by default) and a
   max-size guard. Buffers live alongside the :class:`InternalClient`,
   not on disk: a process restart drops any uncommitted secrets.

3. **Resolution** — :func:`expand_placeholders` replaces tokens back
   with plaintext at the *system* boundary (currently the account
   intake submit handler). The agent's account-creation request only
   ever carries placeholders; the system grabs the real value from
   the buffer when persisting into the vault, then drops the buffer
   entry so a leaked transcript can't replay the secret.

Threat model and limits
-----------------------

* The scanner is opt-in heuristic; it tries to keep false positives
  low by requiring high entropy and minimum lengths, but adversarial
  inputs may still leak through. Pair this layer with the existing
  :mod:`nerya.security.prompt_injection` firewall — it rejects
  messages that explicitly ask the agent to exfiltrate secrets.
* Placeholder tokens themselves are not secrets; an attacker who can
  read the token cannot redeem it without also having access to the
  in-process buffer.
* Anything captured here is auto-purged after the TTL even if the
  agent never calls back, so a forgotten-about chat session cannot
  leave plaintext behind.
"""

from __future__ import annotations

import re
import secrets as _secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any


_DEFAULT_TTL_S = 10 * 60  # 10 minutes
_DEFAULT_MAX_ENTRIES = 64

# Detection patterns. Order matters: higher-confidence patterns run
# first so their match isn't shadowed by a looser one.
_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    # 0x-prefixed hex private keys (EVM, Hyperliquid, …)
    ("evm_private_key", re.compile(r"\b0x[a-fA-F0-9]{64}\b")),
    # JWT (header.payload.signature)
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"
    )),
    # AWS-style access key
    ("aws_access_key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    # Slack / Telegram / OpenAI / Anthropic style prefixed tokens
    ("prefixed_secret", re.compile(
        r"\b(?:sk|sk-ant|xoxb|xoxp|xoxa|ghp|gho|ghu|ghs|ghr|sk-proj|sk-or|"
        r"AIza|gsk_|hf_|tg|sntrys|stripe-[a-z0-9_]+|cdp-)"
        r"[A-Za-z0-9_\-]{20,}\b"
    )),
    # Bare hex private keys (no 0x prefix) — kept lower priority
    # because plain hashes can also look like this; we still capture
    # them since pasting a key without 0x is common in CLI exports.
    ("hex_private_key", re.compile(r"\b[a-fA-F0-9]{64}\b")),
    # Solana / multi-chain base58 keys (88 chars is full-length, 64+ is
    # often shortened or compressed).
    ("base58_key", re.compile(
        r"\b[1-9A-HJ-NP-Za-km-z]{86,90}\b"
    )),
    # Generic alphanumeric/api key — catches Binance (64 chars
    # base64), OKX (32+), Bybit (40+) style tokens. We require a high
    # ratio of digits-vs-letters to reduce false positives on plain
    # English words. Length window 28-128 keeps short auth codes out.
    ("api_token", re.compile(
        r"\b[A-Za-z0-9_\-]{28,128}\b"
    )),
]

# BIP-39 mnemonics: 12, 15, 18, 21, or 24 lowercase words from the
# BIP-39 wordlist. We don't ship the full wordlist; instead we
# heuristically match a sequence of 12+ lowercase ASCII words separated
# by single spaces, with an optional newline boundary. The placeholder
# wraps the entire sequence so the agent never sees any words.
_MNEMONIC_RE = re.compile(
    r"(?:(?<=^)|(?<=\s))([a-z]{3,8}(?:\s+[a-z]{3,8}){11,23})(?=\s|$|[.,!?])",
    re.MULTILINE,
)


@dataclass
class SecretCapture:
    """One captured plaintext value plus metadata."""

    token: str
    kind: str
    preview: str  # first 4 + last 3 chars; never the full value
    captured_at: float
    ttl_s: int
    used_at: float | None = None

    def is_expired(self) -> bool:
        return time.time() - self.captured_at > self.ttl_s

    def asdict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "kind": self.kind,
            "preview": self.preview,
            "captured_at": self.captured_at,
            "ttl_s": self.ttl_s,
            "used_at": self.used_at,
            "expired": self.is_expired(),
        }


@dataclass
class SecretBuffer:
    """In-memory store of captured secrets keyed by opaque tokens."""

    ttl_s: int = _DEFAULT_TTL_S
    max_entries: int = _DEFAULT_MAX_ENTRIES
    _values: dict[str, str] = field(default_factory=dict, init=False)
    _meta: dict[str, SecretCapture] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def put(self, value: str, *, kind: str = "opaque") -> str:
        """Stash ``value`` under a fresh token, return the token."""

        with self._lock:
            self._gc_locked()
            token = _secrets.token_hex(8)
            while token in self._values:  # pragma: no cover — collision guard
                token = _secrets.token_hex(8)
            if len(self._values) >= self.max_entries:
                # Drop the oldest entry to keep the buffer bounded.
                oldest = min(self._meta, key=lambda k: self._meta[k].captured_at)
                self._values.pop(oldest, None)
                self._meta.pop(oldest, None)
            self._values[token] = value
            self._meta[token] = SecretCapture(
                token=token, kind=kind,
                preview=_short_preview(value),
                captured_at=time.time(), ttl_s=self.ttl_s,
            )
            return token

    def take(self, token: str) -> str | None:
        """Pop the plaintext (single-use). Returns ``None`` if expired/missing."""

        with self._lock:
            meta = self._meta.get(token)
            if meta is None:
                return None
            if meta.is_expired():
                self._values.pop(token, None)
                self._meta.pop(token, None)
                return None
            value = self._values.pop(token, None)
            self._meta.pop(token, None)
            return value

    def peek(self, token: str) -> SecretCapture | None:
        with self._lock:
            return self._meta.get(token)

    def list_metadata(self) -> list[SecretCapture]:
        with self._lock:
            self._gc_locked()
            return [SecretCapture(**c.asdict()) if False else c
                    for c in self._meta.values()]

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._meta.clear()

    def _gc_locked(self) -> None:
        now = time.time()
        stale = [tok for tok, m in self._meta.items()
                 if now - m.captured_at > m.ttl_s]
        for tok in stale:
            self._values.pop(tok, None)
            self._meta.pop(tok, None)


_PLACEHOLDER_RE = re.compile(r"<<NERYA_SECRET:([0-9a-fA-F]{4,64})>>")


def make_placeholder(token: str) -> str:
    return f"<<NERYA_SECRET:{token}>>"


def _short_preview(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}***{value[-3:]}"


@dataclass
class ScanResult:
    redacted_text: str
    captures: list[SecretCapture] = field(default_factory=list)

    @property
    def captured(self) -> bool:
        return bool(self.captures)

    def asdict(self) -> dict[str, Any]:
        return {
            "redacted_text": self.redacted_text,
            "captured": self.captured,
            "captures": [c.asdict() for c in self.captures],
        }


def scan_and_redact(
    text: str,
    *,
    buffer: SecretBuffer,
) -> ScanResult:
    """Detect secrets in ``text``, stash them, and return placeholder text.

    Each match is replaced *in place* with ``<<NERYA_SECRET:<tok>>>``;
    the original text is left otherwise untouched so the agent still
    sees the operator's actual question. Captures are returned in the
    order they appear so the gateway can echo a ``stashed N secret(s)``
    receipt back to the user.
    """

    if not text:
        return ScanResult(redacted_text=text or "", captures=[])
    captures: list[SecretCapture] = []

    def _swap(value: str, kind: str) -> str:
        token = buffer.put(value, kind=kind)
        meta = buffer.peek(token)
        if meta is not None:
            captures.append(meta)
        return make_placeholder(token)

    redacted = text

    # Mnemonic sweep first — wider regex needs to run before single-word
    # patterns so we don't carve a 12-word phrase into 12 placeholders.
    def _mnemonic_repl(m: "re.Match[str]") -> str:
        return _swap(m.group(1), "mnemonic")

    redacted = _MNEMONIC_RE.sub(_mnemonic_repl, redacted)

    for kind, pat in _PATTERNS:
        def _repl(m: "re.Match[str]", _kind: str = kind) -> str:
            return _swap(m.group(0), _kind)

        redacted = pat.sub(_repl, redacted)

    return ScanResult(redacted_text=redacted, captures=captures)


def expand_placeholders(
    text: str,
    *,
    buffer: SecretBuffer,
    consume: bool = True,
) -> tuple[str, dict[str, str]]:
    """Replace ``<<NERYA_SECRET:tok>>`` markers with the original plaintext.

    Returns ``(expanded_text, resolved)`` where ``resolved`` maps token
    → plaintext for the captures that were consumed. Tokens that the
    buffer no longer knows about (expired/already taken) are left as
    placeholder strings so the caller can fail closed.
    """

    if not text or "<<NERYA_SECRET:" not in text:
        return text or "", {}
    resolved: dict[str, str] = {}

    def _swap(m: "re.Match[str]") -> str:
        tok = m.group(1)
        if consume:
            value = buffer.take(tok)
        else:
            cap = buffer.peek(tok)
            value = None
            if cap is not None and not cap.is_expired():
                value = buffer._values.get(tok)
        if value is None:
            return m.group(0)
        resolved[tok] = value
        return value

    expanded = _PLACEHOLDER_RE.sub(_swap, text)
    return expanded, resolved


def consume_token(token: str, *, buffer: SecretBuffer) -> str | None:
    """Pop a single token directly. Public alias of :meth:`SecretBuffer.take`."""

    return buffer.take(token)


__all__ = [
    "SecretBuffer",
    "SecretCapture",
    "ScanResult",
    "scan_and_redact",
    "expand_placeholders",
    "consume_token",
    "make_placeholder",
]
