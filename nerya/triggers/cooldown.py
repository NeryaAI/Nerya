"""Thin wrapper over db.CooldownRepository for direct agent use."""

from __future__ import annotations

from ..core.paths import WorkspacePaths
from ..db import CooldownRepository
from ..db.sqlite import connect


def check(paths: WorkspacePaths, scope: str, key: str, cooldown_s: int) -> bool:
    con = connect(paths.db)
    return CooldownRepository(con).hit_and_check(scope, key, cooldown_s)
