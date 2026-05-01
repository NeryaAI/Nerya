"""Redaction for logs, journals and LLM prompts.

We do two things:

1. Blanket regex over strings to catch common API key shapes. Pure
   defensive — secrets should never hit a string that reaches here in
   the first place.
2. `redact_dict` used by security-critical journals, which recurses and
   masks any key whose name suggests a secret.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

_PATTERNS = [
    # Ethereum private key / generic 32-byte hex
    re.compile(r"\b0x[a-fA-F0-9]{64}\b"),
    # AWS-style access key
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Generic long base64 / hex secrets (>32 chars, mixed case+digits)
    re.compile(r"\b(?=[A-Za-z0-9+/=]{40,}\b)[A-Za-z0-9+/=]+\b"),
    # Telegram bot token
    re.compile(r"\b\d{9,11}:[A-Za-z0-9_-]{30,}\b"),
    # OpenAI-style secret
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
]

_SECRET_KEY_HINTS = {
    "api_key", "apikey", "secret", "secret_key", "secretkey",
    "private_key", "privatekey", "pk", "sk",
    "token", "bot_token", "telegram_token", "discord_token",
    "passphrase", "password", "pass",
    "x-api-key", "authorization", "auth",
}


def redact_text(text: str) -> str:
    if not isinstance(text, str):
        return text  # type: ignore[return-value]
    out = text
    for pat in _PATTERNS:
        out = pat.sub("***REDACTED***", out)
    return out


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:12]


def preview(value: str, *, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return "*" * max(0, len(value) - visible) + value[-visible:]


def redact_dict(data: Any) -> Any:
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            key_lc = str(k).lower()
            if any(hint in key_lc for hint in _SECRET_KEY_HINTS):
                if isinstance(v, str):
                    out[k] = {"__redacted__": True, "preview": preview(v), "sha12": fingerprint(v)}
                else:
                    out[k] = {"__redacted__": True}
            else:
                out[k] = redact_dict(v)
        return out
    if isinstance(data, list):
        return [redact_dict(x) for x in data]
    if isinstance(data, str):
        return redact_text(data)
    return data
