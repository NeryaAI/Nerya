"""Reviewed lifecycle settings for continuous strategy listeners."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..core.errors import TradingError


def is_continuous(manifest: Any) -> bool:
    raw = manifest.extras if hasattr(manifest, "extras") else manifest
    return isinstance(raw, dict) and isinstance(raw.get("runtime"), dict) and raw["runtime"].get("mode") == "continuous"


@dataclass(frozen=True)
class ContinuousConfig:
    queue_size: int = 32
    max_event_age_seconds: float = 60
    min_dispatch_interval_seconds: float = 1
    max_restarts: int = 3
    restart_backoff_seconds: float = 2
    resume_on_start: bool = False
    streams: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> "ContinuousConfig":
        if not isinstance(raw, dict) or raw.get("mode") != "continuous":
            raise TradingError("runtime.mode must be continuous")
        allowed = {"mode", "queue_size", "max_event_age_seconds", "min_dispatch_interval_seconds", "max_restarts", "restart_backoff_seconds", "resume_on_start", "streams"}
        if set(raw) - allowed:
            raise TradingError("unknown continuous runtime fields")
        def number(name: str, default: float, low: float, high: float, integer: bool = False):
            value = raw.get(name, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high or integer and int(value) != value:
                raise TradingError(f"runtime.{name} must be a finite number between {low} and {high}")
            return int(value) if integer else float(value)
        resume = raw.get("resume_on_start", False)
        if not isinstance(resume, bool):
            raise TradingError("runtime.resume_on_start must be boolean")
        streams = raw.get("streams", {})
        if not isinstance(streams, dict) or len(streams) > 16:
            raise TradingError("runtime.streams must contain at most 16 named feeds")
        for name, spec in streams.items():
            if not isinstance(name, str) or not name or len(name) > 80 or not isinstance(spec, dict):
                raise TradingError("invalid named stream")
            if set(spec) - {"url", "subscribe", "max_message_bytes", "reconnect_seconds"}:
                raise TradingError(f"stream {name}: unsupported fields")
            url = urlsplit(str(spec.get("url", "")))
            if url.scheme not in {"ws", "wss"} or not url.hostname or url.username or url.password or url.fragment:
                raise TradingError(f"stream {name}: requires a public ws/wss URL without credentials")
            for key, default, low, high in (("max_message_bytes", 262144, 1024, 1048576), ("reconnect_seconds", 2, 0.1, 60)):
                value = spec.get(key, default)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                    raise TradingError(f"stream {name}: invalid {key}")
            if not isinstance(spec.get("subscribe", []), list) or len(spec.get("subscribe", [])) > 16:
                raise TradingError(f"stream {name}: invalid subscriptions")
        return cls(
            queue_size=number("queue_size", 32, 1, 1024, True),
            max_event_age_seconds=number("max_event_age_seconds", 60, 1, 3600),
            min_dispatch_interval_seconds=number("min_dispatch_interval_seconds", 1, 0, 3600),
            max_restarts=number("max_restarts", 3, 0, 20, True),
            restart_backoff_seconds=number("restart_backoff_seconds", 2, 0.1, 60),
            resume_on_start=resume, streams=streams,
        )
