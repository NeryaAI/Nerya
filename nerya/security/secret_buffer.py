"""Process-wide singleton SecretBuffer used by the gateway pipeline.

Lives in this module rather than on ``InternalClient`` so the same
buffer can be shared between the gateway scanners (which redact
inbound text) and the account-intake submit handler (which expands
placeholders back to plaintext at the moment a credential is written
into the vault). Per-process is enough — placeholders are never
persisted to disk and a process restart should drop pending captures.
"""

from __future__ import annotations

import threading
from typing import Optional

from .secret_scanner import SecretBuffer


_BUFFER_LOCK = threading.Lock()
_BUFFER: Optional[SecretBuffer] = None


def get_default_buffer() -> SecretBuffer:
    """Return the lazily-initialised process-wide :class:`SecretBuffer`."""

    global _BUFFER
    if _BUFFER is None:
        with _BUFFER_LOCK:
            if _BUFFER is None:
                _BUFFER = SecretBuffer()
    return _BUFFER


def reset_default_buffer() -> None:
    """Drop every captured secret. Intended for tests / kill-switch."""

    global _BUFFER
    with _BUFFER_LOCK:
        if _BUFFER is not None:
            _BUFFER.clear()
        _BUFFER = None


__all__ = ["get_default_buffer", "reset_default_buffer"]
