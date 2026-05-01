"""Gateway identity + event-id helpers.

Plan 23 §10 calls out that ``telegram_{chat_id}`` session ids and
``tg_reply_{chat_id}_{time}`` outbox ids are constructed inline, so we
cannot dedupe properly across threads / users / workspaces and cannot
build a clean platform-id mirror. This module centralises that contract
so every gateway adapter (Telegram today, Discord/Slack tomorrow) shares
the same shape.

Design notes:

- ``session_id`` keeps backward compatibility with the existing
  ``telegram_{chat_id}`` shape when only ``chat_id`` is provided. Adding
  ``thread_id`` / ``user_id`` / ``workspace_id`` only extends the suffix
  so persisted sessions keep resolving.
- ``message_id`` includes a monotonic counter so two replies in the same
  millisecond get distinct ids on Windows (``time.time()`` resolution is
  16ms on some boxes).
- All identifiers are sanitised to ``[A-Za-z0-9._:-]`` so they can be
  embedded in filesystem paths and URLs without escape hazards.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Iterable, Optional


_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._:-]+")


def _slug(value: str | int | None, *, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    return _SAFE_CHARS.sub("_", text)


_counter_lock = threading.Lock()
_counter_seed = 0


def _next_seq() -> int:
    global _counter_seed
    with _counter_lock:
        _counter_seed += 1
        return _counter_seed


def session_id(
    platform: str,
    *,
    chat_id: str | int | None = None,
    thread_id: str | int | None = None,
    user_id: str | int | None = None,
    workspace_id: str | int | None = None,
) -> str:
    """Build a stable session id for a gateway turn.

    Returns ``platform_{chat_id}`` for the Telegram baseline, then
    appends ``_t{thread}`` / ``_u{user}`` / ``_w{workspace}`` only when
    callers provide those parts. That keeps the legacy ids intact while
    letting newer adapters express thread/group context.
    """

    plat = _slug(platform, default="gateway")
    parts = [plat, _slug(chat_id, default="default")]
    if thread_id is not None and str(thread_id).strip():
        parts.append("t" + _slug(thread_id))
    if user_id is not None and str(user_id).strip():
        parts.append("u" + _slug(user_id))
    if workspace_id is not None and str(workspace_id).strip():
        parts.append("w" + _slug(workspace_id))
    return "_".join(parts)


def message_id(
    platform: str,
    *,
    chat_id: str | int | None = None,
    direction: str = "out",
    update_id: str | int | None = None,
    suffix: str | None = None,
) -> str:
    """Build a deterministic outbox/inbox message id.

    ``direction`` distinguishes mirror entries (``in``) from outbound
    replies (``out``). ``update_id`` is preserved when the platform
    surfaces one (Telegram ``update_id``, WhatsApp message id, etc.) so
    operators can correlate Nerya artefacts with the upstream platform
    log.
    """

    plat = _slug(platform, default="gateway")
    chat = _slug(chat_id, default="default")
    direction_slug = _slug(direction, default="out")
    head = f"{plat}_{direction_slug}_{chat}"
    ts = int(time.time() * 1000)
    seq = _next_seq()
    parts = [head, f"t{ts}", f"s{seq}"]
    if update_id is not None and str(update_id).strip():
        parts.append("u" + _slug(update_id))
    if suffix:
        parts.append(_slug(suffix))
    return "_".join(parts)


def parse_session_id(value: str) -> dict[str, str]:
    """Inverse of :func:`session_id`. Best-effort — returns whatever
    fragments were encoded so observability tools can group sessions."""

    if not isinstance(value, str) or not value:
        return {}
    parts = value.split("_")
    if not parts:
        return {}
    out: dict[str, str] = {"platform": parts[0]}
    if len(parts) >= 2:
        out["chat_id"] = parts[1]
    for part in parts[2:]:
        if not part:
            continue
        head, body = part[:1], part[1:]
        if head == "t" and body:
            out["thread_id"] = body
        elif head == "u" and body:
            out["user_id"] = body
        elif head == "w" and body:
            out["workspace_id"] = body
    return out


def first_present(values: Iterable[Optional[str]]) -> str:
    for v in values:
        if v is not None and str(v).strip():
            return str(v)
    return ""
