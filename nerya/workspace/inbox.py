"""Simple file-based inbox drains. Triggers / SDK orders / LLM requests come in as JSON files."""

from __future__ import annotations

import json
from pathlib import Path

from ..core.atomic_write import atomic_write_text
from ..core.time import now_iso


def drop(path: Path, payload: dict) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    name = (payload.get("idempotency_key") or payload.get("event_id") or payload.get("intent_id")
            or now_iso().replace(":", "-")) + ".json"
    target = path / name
    atomic_write_text(target, json.dumps(payload, default=str, ensure_ascii=False, indent=2))
    return target


def drain(path: Path) -> list[tuple[Path, dict]]:
    out: list[tuple[Path, dict]] = []
    path.mkdir(parents=True, exist_ok=True)
    for p in sorted(path.glob("*.json")):
        try:
            out.append((p, json.loads(p.read_text(encoding="utf-8"))))
        except json.JSONDecodeError:
            continue
    return out
