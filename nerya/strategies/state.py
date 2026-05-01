"""Per-strategy state, run records, kill switch, and version registry.

Plan ref: ``2026-04-28-agent-generated-strategy-runtime-refactor.md`` §10.

This module owns the *outside-the-context* persistence for a strategy
package. The in-strategy ``ctx.state`` (a small KV store) lives in
:class:`nerya.strategies.context.StrategyState`; this module provides
the records the runner writes:

* ``runs/<run_id>.json`` — full run snapshot (manifest hash, inputs,
  outputs, audit trail, timing). Written atomically once per
  :meth:`StrategyRunner.run_tick` call.
* ``state/state.json`` — the strategy KV (already exposed via the
  :class:`~nerya.workspace.state_store.StateStore` from the context
  facade); this module just centralises the path.
* ``state/kill_switch.json`` — operator-controlled per-strategy
  kill switch. Read on every tick by the runner; if asserted the
  runner skips the entrypoint and writes a HOLD result.
* ``versions/<hash>.json`` — content-hash registry. Each newly
  promoted package version is recorded here so subsequent runs can
  pin / replay an exact version, and so the dashboard can show the
  full version history without reparsing the package on every load.

Why a single module
-------------------
The persistence rules are tiny but live across *three* directories.
Centralising the readers/writers here means the runner, the
dashboard, the validator, and the evolution loop all agree on:

* which keys are required in ``runs/<run_id>.json``,
* how the kill switch is stamped (when, by whom, why),
* and what the version registry's identity is for replay.

Invariants
----------
* Every writer goes through :func:`atomic_write_text` so partial
  writes can never corrupt the strategy package.
* All filenames are derived from sanitized strategy ids and
  hex-only run / version ids — no user-controlled path components.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..core import jsonl
from ..core.atomic_write import atomic_write_text
from ..core.errors import NeryaError
from ..core.ids import new_id
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from .package import StrategyPackage


_LOG = logging.getLogger(__name__)
_HEX_RE = re.compile(r"^[A-Fa-f0-9]+$")


# ---------------------------------------------------------------------------
# IDs
# ---------------------------------------------------------------------------


def new_run_id() -> str:
    """Run id used for the ``runs/<run_id>.json`` filename + journal rows."""

    return new_id("run")


# ---------------------------------------------------------------------------
# Run records
# ---------------------------------------------------------------------------


@dataclass
class StrategyRunRecord:
    """Persistent snapshot of one strategy tick.

    ``status`` mirrors :class:`~nerya.strategies.result.StrategyResultStatus`
    so dashboards / runs index lookups don't need a translation
    layer. ``audit`` carries the in-memory event log captured by
    :class:`~nerya.strategies.context.StrategyAudit`.

    ``inputs`` is the tick context (trigger payload, manifest hash,
    operator metadata) and ``outputs`` is the rendered
    :class:`StrategyResult`. We keep them split so a future
    backtest replay can re-execute the strategy with exactly the
    same inputs and diff the outputs.
    """

    run_id: str
    strategy_id: str
    package_hash: str
    started_at: str
    finished_at: str
    duration_ms: int
    status: str
    mode: str  # paper | shadow | live
    reason: str = ""
    session_id: Optional[str] = None
    trigger_event_id: Optional[str] = None
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[dict[str, Any]] = None

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


class StrategyRunStore:
    """Reader/writer for ``workspace/strategies/<id>/runs/<run_id>.json``.

    The runner writes one record per tick; the dashboard / evolution
    loop / replay tooling read records back. Records are sorted by
    ``started_at`` on listing so callers don't need to sort.
    """

    def __init__(self, paths: WorkspacePaths, strategy_id: str):
        self.paths = paths
        self.strategy_id = strategy_id

    @property
    def runs_dir(self) -> Path:
        return self.paths.strategy(self.strategy_id) / "runs"

    def write(self, record: StrategyRunRecord) -> Path:
        if record.run_id != _safe_id(record.run_id):
            raise NeryaError(f"unsafe run_id: {record.run_id!r}")
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        path = self.runs_dir / f"{record.run_id}.json"
        atomic_write_text(path, json.dumps(record.asdict(), indent=2, default=str))
        # Mirror to the workspace journal so existing dashboard queries
        # ("recent runs across strategies") work without scanning every
        # strategy package.
        try:
            jsonl.append(
                self.paths.journal("strategy_runs"),
                {
                    "kind": "strategy.run",
                    "run_id": record.run_id,
                    "strategy_id": record.strategy_id,
                    "package_hash": record.package_hash,
                    "started_at": record.started_at,
                    "finished_at": record.finished_at,
                    "duration_ms": record.duration_ms,
                    "status": record.status,
                    "mode": record.mode,
                    "reason": record.reason,
                    "session_id": record.session_id,
                    "trigger_event_id": record.trigger_event_id,
                    "error_kind": (record.error or {}).get("kind") if record.error else None,
                    "ts": record.finished_at,
                },
            )
        except Exception:
            _LOG.exception("strategy_runs journal append failed")
        return path

    def read(self, run_id: str) -> Optional[StrategyRunRecord]:
        rid = _safe_id(run_id)
        path = self.runs_dir / f"{rid}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning("malformed run record %s: %s", path, exc)
            return None
        return _record_from_dict(data)

    def list(self, *, limit: int = 50) -> list[StrategyRunRecord]:
        if not self.runs_dir.exists():
            return []
        records: list[StrategyRunRecord] = []
        for path in sorted(self.runs_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
            except (OSError, json.JSONDecodeError):
                continue
            rec = _record_from_dict(data)
            if rec is not None:
                records.append(rec)
        records.sort(key=lambda r: r.started_at, reverse=True)
        if limit > 0:
            records = records[:limit]
        return records


def _record_from_dict(data: dict[str, Any]) -> Optional[StrategyRunRecord]:
    try:
        return StrategyRunRecord(
            run_id=str(data.get("run_id") or ""),
            strategy_id=str(data.get("strategy_id") or ""),
            package_hash=str(data.get("package_hash") or ""),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            duration_ms=int(data.get("duration_ms") or 0),
            status=str(data.get("status") or "ok"),
            mode=str(data.get("mode") or "paper"),
            reason=str(data.get("reason") or ""),
            session_id=data.get("session_id"),
            trigger_event_id=data.get("trigger_event_id"),
            inputs=dict(data.get("inputs") or {}),
            outputs=dict(data.get("outputs") or {}),
            audit=list(data.get("audit") or []),
            error=data.get("error") if isinstance(data.get("error"), dict) else None,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


@dataclass
class KillSwitchState:
    asserted: bool
    reason: str = ""
    by: str = ""
    at: str = ""

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


class StrategyKillSwitch:
    """Per-strategy halt flag.

    Stored at ``state/kill_switch.json``. The runner consults this
    before instantiating the context; an asserted switch produces a
    HOLD result with a clear reason. Operators set/clear via the
    dashboard or CLI; setting requires a non-empty reason.

    File format::

        {"asserted": true, "reason": "stop loss breach",
         "by": "operator:ricky", "at": "2026-04-28T07:00:00+00:00"}
    """

    def __init__(self, paths: WorkspacePaths, strategy_id: str):
        self.paths = paths
        self.strategy_id = strategy_id

    @property
    def path(self) -> Path:
        return self.paths.strategy(self.strategy_id) / "state" / "kill_switch.json"

    def get(self) -> KillSwitchState:
        if not self.path.exists():
            return KillSwitchState(asserted=False)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return KillSwitchState(asserted=False)
        return KillSwitchState(
            asserted=bool(data.get("asserted", False)),
            reason=str(data.get("reason") or ""),
            by=str(data.get("by") or ""),
            at=str(data.get("at") or ""),
        )

    def assert_(self, *, reason: str, by: str = "operator") -> KillSwitchState:
        if not reason.strip():
            raise NeryaError("kill switch requires a non-empty reason")
        state = KillSwitchState(
            asserted=True, reason=reason.strip(), by=by.strip() or "operator", at=now_iso()
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(state.asdict(), indent=2))
        return state

    def clear(self, *, by: str = "operator") -> KillSwitchState:
        state = KillSwitchState(
            asserted=False, reason="", by=by.strip() or "operator", at=now_iso()
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(state.asdict(), indent=2))
        return state


# ---------------------------------------------------------------------------
# Version registry
# ---------------------------------------------------------------------------


@dataclass
class StrategyVersionRecord:
    """One promoted package version.

    Identity is the package's content hash; the registry simply
    pins the human-readable metadata that goes alongside it
    (promoted_at, promoted_by, source proposal id, mode at
    promotion, optional notes).
    """

    package_hash: str
    promoted_at: str
    promoted_by: str
    proposal_id: Optional[str] = None
    mode: str = "paper"
    notes: str = ""
    files: tuple[str, ...] = ()

    def asdict(self) -> dict[str, Any]:
        return {
            "package_hash": self.package_hash,
            "promoted_at": self.promoted_at,
            "promoted_by": self.promoted_by,
            "proposal_id": self.proposal_id,
            "mode": self.mode,
            "notes": self.notes,
            "files": list(self.files),
        }


class StrategyVersionRegistry:
    """Per-strategy version pinboard at ``versions/<hash>.json``."""

    def __init__(self, paths: WorkspacePaths, strategy_id: str):
        self.paths = paths
        self.strategy_id = strategy_id

    @property
    def versions_dir(self) -> Path:
        return self.paths.strategy(self.strategy_id) / "versions"

    def record(
        self,
        package: StrategyPackage,
        *,
        promoted_by: str,
        proposal_id: Optional[str] = None,
        notes: str = "",
    ) -> StrategyVersionRecord:
        """Write the version record for the *currently-loaded* package.

        Idempotent: re-recording the same hash overwrites the JSON
        but is treated as a no-op semantically.
        """

        h = package.content_hash
        if not h or not _HEX_RE.match(h):
            raise NeryaError(f"package content hash is invalid: {h!r}")
        record = StrategyVersionRecord(
            package_hash=h,
            promoted_at=now_iso(),
            promoted_by=str(promoted_by or "operator"),
            proposal_id=proposal_id,
            mode=package.manifest.mode,
            notes=str(notes or "").strip(),
            files=package.files,
        )
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.versions_dir / f"{h}.json",
            json.dumps(record.asdict(), indent=2),
        )
        return record

    def get(self, package_hash: str) -> Optional[StrategyVersionRecord]:
        h = package_hash.strip().lower()
        if not _HEX_RE.match(h):
            return None
        path = self.versions_dir / f"{h}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return None
        return StrategyVersionRecord(
            package_hash=str(data.get("package_hash") or h),
            promoted_at=str(data.get("promoted_at") or ""),
            promoted_by=str(data.get("promoted_by") or ""),
            proposal_id=data.get("proposal_id"),
            mode=str(data.get("mode") or "paper"),
            notes=str(data.get("notes") or ""),
            files=tuple(str(f) for f in data.get("files") or ()),
        )

    def list(self) -> list[StrategyVersionRecord]:
        if not self.versions_dir.exists():
            return []
        out: list[StrategyVersionRecord] = []
        for path in sorted(self.versions_dir.glob("*.json")):
            rec = self.get(path.stem)
            if rec is not None:
                out.append(rec)
        out.sort(key=lambda r: r.promoted_at, reverse=True)
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _safe_id(value: str) -> str:
    s = str(value or "").strip()
    if not _RUN_ID_RE.match(s):
        raise NeryaError(f"unsafe id: {value!r}")
    return s


__all__ = [
    "KillSwitchState",
    "StrategyKillSwitch",
    "StrategyRunRecord",
    "StrategyRunStore",
    "StrategyVersionRecord",
    "StrategyVersionRegistry",
    "new_run_id",
]
