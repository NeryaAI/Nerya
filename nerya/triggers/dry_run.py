"""Helper for CLI dry-run."""

from __future__ import annotations

import json
from pathlib import Path

from ..core.config import Config
from .event import TriggerEvent
from .runtime import TriggerRuntime


def dry_run_file(config: Config, path: Path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    event = TriggerEvent.new(**raw) if not raw.get("event_id") else TriggerEvent(**raw)
    result = TriggerRuntime.boot(config).router.dry_run(event)
    return result.asdict()
