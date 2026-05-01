"""Dashboard channel — writes messages to workspace/outbox/messages/*.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.atomic_write import atomic_write_text


def send(outbox_messages: Path, message: dict[str, Any]) -> Path:
    outbox_messages.mkdir(parents=True, exist_ok=True)
    path = outbox_messages / f"{message['message_id']}.json"
    atomic_write_text(path, json.dumps(message, indent=2, default=str))
    return path
