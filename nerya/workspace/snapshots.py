"""Periodic state snapshots for crash recovery / replay. Best-effort."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from ..core.atomic_write import atomic_write_text
from ..core.paths import WorkspacePaths


def take_snapshot(paths: WorkspacePaths, label: str | None = None) -> Path:
    paths.snapshots.mkdir(parents=True, exist_ok=True)
    name = label or time.strftime("%Y%m%dT%H%M%S")
    target = paths.snapshots / name
    target.mkdir(parents=True, exist_ok=True)
    if paths.runtime_state.exists():
        atomic_write_text(target / "runtime.json", paths.runtime_state.read_text(encoding="utf-8"))
    # copy virtual ledgers
    vls = paths.virtual_ledgers
    if vls.exists():
        dest = target / "virtual_ledgers"
        dest.mkdir(exist_ok=True)
        for p in vls.glob("*.json"):
            shutil.copy2(p, dest / p.name)
    return target
