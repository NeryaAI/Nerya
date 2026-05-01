"""Prompt/response redaction before journalling."""

from __future__ import annotations

from ..core.redaction import redact_text


def scrub(text: str) -> str:
    return redact_text(text)
