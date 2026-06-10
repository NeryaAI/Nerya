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
import os
import re
from typing import Any

_REDACT_ENABLED = os.getenv("NERYA_REDACT_ENABLED", "1")
_REDACTION_ACTIVE = str(_REDACT_ENABLED).strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
_REDACTION_DISABLED_PLACEHOLDER = (
    "***REDACTION_DISABLED_AT_IMPORT_TEXT_WITHHELD***"
)

_PATTERNS = [
    # Ethereum private key / generic 32-byte hex
    re.compile(r"\b0x[a-fA-F0-9]{64}\b"),
    # AWS-style access key
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Vendor-prefixed API tokens such as ``sk-...`` / ``tp-...``.
    re.compile(r"\b[A-Za-z]{2,12}(?:-[A-Za-z0-9_]{2,16})?-[A-Za-z0-9_-]{20,}\b"),
    # Provider keys that use a hex id plus a dot-suffixed secret segment.
    re.compile(r"\b[a-fA-F0-9]{24,64}\.[A-Za-z0-9_-]{8,}\b"),
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
    if not _REDACTION_ACTIVE:
        return _REDACTION_DISABLED_PLACEHOLDER if text else text
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


_DISPLAY_SECRET_KEYS = {
    "api_key", "apikey", "secret", "secret_key", "secretkey",
    "private_key", "privatekey", "token_secret", "refresh_token",
    "access_token", "bot_token", "telegram_token", "discord_token",
    "passphrase", "password", "pass", "x-api-key", "x_api_key",
    "authorization", "auth_header",
}


def redact_display_dict(data: Any) -> Any:
    """Redact for operator-facing event/detail panes.

    This keeps observability fields such as ``task_id`` and ``tokens``
    readable. The stricter :func:`redact_dict` intentionally treats
    short substrings such as ``sk`` and ``token`` as secret hints, which
    is too aggressive for UI traces where those words are common
    telemetry names.
    """

    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            raw_key = str(k)
            key_lc = raw_key.lower()
            key_norm = key_lc.replace("-", "_")
            is_secret_key = (
                key_lc in _DISPLAY_SECRET_KEYS
                or key_norm in _DISPLAY_SECRET_KEYS
                or key_norm.endswith("_api_key")
                or key_norm.endswith("_secret")
                or key_norm.endswith("_private_key")
                or key_norm.endswith("_password")
                or key_norm.endswith("_passphrase")
                or key_norm.endswith("_access_token")
                or key_norm.endswith("_refresh_token")
            )
            if is_secret_key:
                if isinstance(v, str):
                    out[k] = {
                        "__redacted__": True,
                        "preview": preview(v),
                        "sha12": fingerprint(v),
                    }
                else:
                    out[k] = {"__redacted__": True}
            else:
                out[k] = redact_display_dict(v)
        return out
    if isinstance(data, list):
        return [redact_display_dict(x) for x in data]
    if isinstance(data, str):
        return redact_text(data)
    return data
