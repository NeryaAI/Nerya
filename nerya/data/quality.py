"""Data quality checks — staleness, missing fields, envelope awareness.

``assess`` now surfaces the :class:`nerya.core.truth.RuntimeEnvelope` of
the data so downstream callers don't treat a ``degraded`` or ``mock``
payload as if it were ``live``. A snapshot is only reported as
``fresh`` when it is both recent **and** the envelope is ``live``.
"""

from __future__ import annotations

import time
from typing import Any


def age_seconds(snapshot_ts: float | int) -> int:
    return max(0, int(time.time() - snapshot_ts))


def _envelope_of(payload: Any) -> dict:
    if isinstance(payload, dict):
        env = payload.get("_envelope")
        if isinstance(env, dict):
            return env
    if isinstance(payload, list):
        for row in payload:
            env = _envelope_of(row)
            if env:
                return env
    return {}


def _mode_of(payload: Any) -> str:
    env = _envelope_of(payload)
    mode = str(env.get("mode") or "").lower() if env else ""
    return mode or "unknown"


def assess(snapshot: Any) -> dict:
    """Return a quality report for ``snapshot``.

    The report includes:

    * ``age_s`` — seconds since the snapshot timestamp (best-effort).
    * ``mode`` — truth-gate mode from the attached envelope (``live`` /
      ``mock`` / ``degraded`` / ``unknown``).
    * ``fresh`` — ``True`` iff ``mode == "live"`` and ``age_s < 30``.
    """
    ts: Any = None
    if isinstance(snapshot, dict):
        ts = snapshot.get("ts") or snapshot.get("snapshot_ts")
    age = age_seconds(ts) if ts else 9999
    mode = _mode_of(snapshot)
    return {
        "age_s": age,
        "mode": mode,
        "fresh": age < 30 and mode == "live",
    }
