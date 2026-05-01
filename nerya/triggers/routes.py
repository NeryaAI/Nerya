"""Route loading + matching."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core import yaml_io
from ..core.paths import WorkspacePaths


@dataclass
class TriggerRoute:
    id: str
    match: dict[str, Any]
    target: str
    strategy_id: str | None = None
    cooldown_seconds: int = 0
    max_per_minute: int = 0        # 0 = unlimited
    max_payload_bytes: int = 65536  # 64 KiB default hard cap
    enabled: bool = True
    paused: bool = False
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def is_active(self) -> bool:
        """True when this route is not paused and not explicitly disabled."""
        return bool(self.enabled) and not bool(self.paused)

    def matches(self, event_dict: dict[str, Any]) -> bool:
        for key, expected in self.match.items():
            if not _dotted_eq(event_dict, key, expected):
                return False
        return True


def _dotted_eq(obj: dict[str, Any], key: str, expected: Any) -> bool:
    cur: Any = obj
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return cur == expected


_KNOWN_ROUTE_FIELDS = {
    "id", "match", "target", "strategy_id",
    "cooldown_seconds", "max_per_minute", "max_payload_bytes",
    "enabled", "paused", "description",
}


def load_routes(paths: WorkspacePaths,
                *, include_inactive: bool = False) -> list[TriggerRoute]:
    """Load routes from ``triggers/routes.yml``.

    By default we *hide* paused/disabled routes from the router so a
    paused route acts like an immediate, reversible kill switch. Control
    plane callers that want the full table (list_routes API, config
    export) pass ``include_inactive=True``.
    """
    doc = yaml_io.load(paths.triggers_routes_file, default={}) or {}
    out: list[TriggerRoute] = []
    for row in doc.get("routes") or []:
        if not isinstance(row, dict) or "id" not in row or "target" not in row:
            continue
        enabled = row.get("enabled")
        enabled_b = True if enabled is None else bool(enabled)
        paused = bool(row.get("paused") or False)
        route = TriggerRoute(
            id=row["id"],
            match=row.get("match") or {},
            target=row["target"],
            strategy_id=row.get("strategy_id"),
            cooldown_seconds=int(row.get("cooldown_seconds") or 0),
            max_per_minute=int(row.get("max_per_minute") or 0),
            max_payload_bytes=int(row.get("max_payload_bytes")
                                  or 65536),
            enabled=enabled_b,
            paused=paused,
            description=str(row.get("description") or ""),
            extra={k: v for k, v in row.items()
                   if k not in _KNOWN_ROUTE_FIELDS},
        )
        # Mirror into extra so consumers that only look at ``extra``
        # (older SDK paths, dump helpers) still see the flags.
        route.extra.setdefault("enabled", enabled_b)
        route.extra.setdefault("paused", paused)
        if route.description:
            route.extra.setdefault("description", route.description)
        if not include_inactive and not route.is_active():
            continue
        out.append(route)
    return out
