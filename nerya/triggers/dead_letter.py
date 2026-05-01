"""Read dead-letter folder."""

from __future__ import annotations

import json
from typing import Any

from ..core.paths import WorkspacePaths


def list_dead_letter(paths: WorkspacePaths) -> list[dict[str, Any]]:
    if not paths.dead_letter.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(paths.dead_letter.iterdir()):
        if not p.is_file():
            continue
        try:
            out.append({"file": p.name, **json.loads(p.read_text(encoding="utf-8"))})
        except Exception:
            out.append({"file": p.name, "error": "unreadable"})
    return out
