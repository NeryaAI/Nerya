"""File-based trigger inbox drain loop."""

from __future__ import annotations

import json
from typing import Iterator

from ..core.paths import WorkspacePaths
from ..workspace import inbox as inbox_mod
from .event import TriggerEvent


def drain(paths: WorkspacePaths) -> Iterator[tuple[TriggerEvent, dict]]:
    for path in inbox_mod.drain(paths.inbox_triggers):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            event = TriggerEvent(**raw)
        except Exception:
            continue
        yield event, {"source_file": str(path)}
        try:
            path.unlink()
        except FileNotFoundError:
            pass
