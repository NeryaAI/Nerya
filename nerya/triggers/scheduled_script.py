"""Approved-script schedule runner.

``session_kind='script'`` schedules execute an approved script directly
instead of routing through a strategy package or agent turn. The trigger
package deliberately receives the script runner as an injected callable so
the scheduler boundary stays small and testable.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from ..core import jsonl
from ..core.config import Config
from ..core.time import now_iso
from .schedule import ScheduleEntry


@dataclass
class ScheduledScriptResult:
    schedule_id: str
    script_id: str | None
    ok: bool
    script_run_id: str | None = None
    result: dict[str, Any] | list[Any] | None = None
    error: dict[str, Any] | None = None
    delivery: list[dict[str, Any]] = field(default_factory=list)
    wall_ms: int = 0

    def asdict(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "script_id": self.script_id,
            "ok": self.ok,
            "script_run_id": self.script_run_id,
            "result": self.result,
            "error": self.error,
            "delivery": list(self.delivery),
            "wall_ms": self.wall_ms,
        }


@dataclass
class ScheduledScriptRunner:
    config: Config
    script_runner: Any
    delivery_fn: Any = None

    def run_once(
        self,
        entry: ScheduleEntry,
        *,
        now_ts: float | None = None,
    ) -> ScheduledScriptResult:
        now_ts = time.time() if now_ts is None else float(now_ts)
        script_id = _script_id_from_entry(entry)
        result = ScheduledScriptResult(
            schedule_id=entry.id,
            script_id=script_id,
            ok=False,
        )
        if not script_id:
            result.error = {
                "code": "script_id_required",
                "message": (
                    "script schedules require payload.script_id or "
                    "target='script:<id>'"
                ),
            }
            self._journal(entry, result, now_ts)
            return result

        args = _script_args_from_payload(entry.payload)
        t0 = time.monotonic()
        script_out: dict[str, Any] | None = None
        try:
            script_out = self.script_runner(
                self.config,
                script_id,
                args=args,
            )
        except Exception as exc:  # noqa: BLE001
            result.error = {
                "code": "script_run_failed",
                "message": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exception(
                    type(exc),
                    exc,
                    exc.__traceback__,
                )[-6:],
            }
            result.wall_ms = int((time.monotonic() - t0) * 1000)
            self._journal(entry, result, now_ts)
            return result

        result.wall_ms = int((time.monotonic() - t0) * 1000)
        result.ok = True
        result.script_run_id = str(script_out.get("script_run_id") or "") or None
        script_result = script_out.get("result")
        if isinstance(script_result, (dict, list)):
            result.result = script_result

        if entry.delivery_targets and self.delivery_fn is not None:
            try:
                result.delivery = list(
                    self.delivery_fn(
                        self.config,
                        entry,
                        {
                            "script_id": script_id,
                            "script_run_id": result.script_run_id,
                            "result": result.result,
                            "session_kind": "script",
                            "schedule_id": entry.id,
                        },
                    ) or []
                )
            except Exception as exc:  # pragma: no cover - defensive
                result.delivery = [{
                    "ok": False,
                    "kind": "delivery_dispatch_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }]

        self._journal(entry, result, now_ts)
        return result

    def _journal(
        self,
        entry: ScheduleEntry,
        result: ScheduledScriptResult,
        now_ts: float,
    ) -> None:
        row = {
            "kind": "scheduled_script.tick",
            "ts": now_iso(),
            "ts_epoch": float(now_ts),
            "schedule_id": entry.id,
            "script_id": result.script_id,
            "target": entry.target,
            "ok": result.ok,
            "script_run_id": result.script_run_id,
            "wall_ms": result.wall_ms,
            "error": result.error,
            "delivery": result.delivery,
        }
        try:
            jsonl.append(self.config.paths.journal("scheduled_script"), row)
        except Exception:  # pragma: no cover - best-effort journaling
            pass


def _script_id_from_entry(entry: ScheduleEntry) -> str | None:
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    raw = payload.get("script_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    target = str(entry.target or "").strip()
    if target.startswith("script:") and target.split(":", 1)[1].strip():
        return target.split(":", 1)[1].strip()
    kind = str(entry.kind or "").strip()
    if kind.startswith("script_tick:") and kind.split(":", 1)[1].strip():
        return kind.split(":", 1)[1].strip()
    return None


def _script_args_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    args = payload.get("args")
    if isinstance(args, dict):
        return dict(args)
    args = payload.get("script_args")
    if isinstance(args, dict):
        return dict(args)
    return {}


__all__ = ["ScheduledScriptRunner", "ScheduledScriptResult"]
