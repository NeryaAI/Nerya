"""Outbox helpers. Messages and SDK results land in files before the
dashboard / gateway pick them up."""

from __future__ import annotations

import json
from pathlib import Path

from ..core.atomic_write import atomic_write_text
from ..core.time import now_iso


def write_message(outbox: Path, message: dict) -> Path:
    outbox.mkdir(parents=True, exist_ok=True)
    name = f"{now_iso().replace(':', '-')}_{message.get('message_id') or 'msg'}.json"
    target = outbox / name
    atomic_write_text(target, json.dumps(message, default=str, ensure_ascii=False, indent=2))
    return target


def write_sdk_result(outbox: Path, result: dict) -> Path:
    outbox.mkdir(parents=True, exist_ok=True)
    name = f"{now_iso().replace(':', '-')}_{result.get('intent_id') or result.get('event_id') or 'res'}.json"
    target = outbox / name
    atomic_write_text(target, json.dumps(result, default=str, ensure_ascii=False, indent=2))
    return target
