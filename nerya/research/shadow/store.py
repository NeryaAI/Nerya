"""Filesystem store for shadow runs — VibeTrading plan §5 Task 8."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..artifacts import strategy_dir
from .models import ShadowRun


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ShadowStore:
    """Persists shadow run state under the candidate's strategy directory.

    Layout::

        workspace/strategies/<strategy_id>/shadow/
          index.jsonl           # one row per run with metadata
          runs/
            <run_id>/
              run.json          # full ShadowRun record
              events.jsonl      # ShadowEvent stream
              fills.jsonl       # ShadowFill stream
              report.json       # final ShadowReport (when completed)
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)

    # ------------------------------------------------------------------
    # path helpers
    # ------------------------------------------------------------------

    def base_dir(self, strategy_id: str) -> Path:
        return strategy_dir(self.workspace, strategy_id) / "shadow"

    def runs_dir(self, strategy_id: str) -> Path:
        return self.base_dir(strategy_id) / "runs"

    def run_dir(self, strategy_id: str, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
            raise ValueError(f"unsafe shadow run_id: {run_id!r}")
        return self.runs_dir(strategy_id) / run_id

    def index_path(self, strategy_id: str) -> Path:
        return self.base_dir(strategy_id) / "index.jsonl"

    def report_path(self, strategy_id: str, run_id: str) -> Path:
        return self.run_dir(strategy_id, run_id) / "report.json"

    def events_path(self, strategy_id: str, run_id: str) -> Path:
        return self.run_dir(strategy_id, run_id) / "events.jsonl"

    def fills_path(self, strategy_id: str, run_id: str) -> Path:
        return self.run_dir(strategy_id, run_id) / "fills.jsonl"

    def run_record_path(self, strategy_id: str, run_id: str) -> Path:
        return self.run_dir(strategy_id, run_id) / "run.json"

    # ------------------------------------------------------------------
    # crud
    # ------------------------------------------------------------------

    def create(self, run: ShadowRun) -> ShadowRun:
        d = self.run_dir(run.strategy_id, run.run_id)
        d.mkdir(parents=True, exist_ok=True)
        run.events_path = str(self.events_path(run.strategy_id, run.run_id))
        run.fills_path = str(self.fills_path(run.strategy_id, run.run_id))
        run.report_path = str(self.report_path(run.strategy_id, run.run_id))
        self._write_run(run)
        self._append_index(run, kind="created")
        return run

    def update(self, run: ShadowRun) -> ShadowRun:
        self._write_run(run)
        self._append_index(run, kind="updated")
        return run

    def get(self, strategy_id: str, run_id: str) -> ShadowRun | None:
        path = self.run_record_path(strategy_id, run_id)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ShadowRun(**payload)

    def list_runs(self, strategy_id: str) -> list[ShadowRun]:
        out: list[ShadowRun] = []
        runs_dir = self.runs_dir(strategy_id)
        if not runs_dir.is_dir():
            return out
        for run_path in sorted(runs_dir.iterdir()):
            if not run_path.is_dir():
                continue
            run = self.get(strategy_id, run_path.name)
            if run is not None:
                out.append(run)
        return out

    # ------------------------------------------------------------------
    # streaming writers
    # ------------------------------------------------------------------

    def append_event(self, strategy_id: str, run_id: str,
                       event_payload: dict) -> None:
        path = self.events_path(strategy_id, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event_payload, ensure_ascii=False) + "\n")

    def append_fill(self, strategy_id: str, run_id: str,
                       fill_payload: dict) -> None:
        path = self.fills_path(strategy_id, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(fill_payload, ensure_ascii=False) + "\n")

    def write_report(self, strategy_id: str, run_id: str,
                       payload: dict) -> Path:
        path = self.report_path(strategy_id, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # iterators
    # ------------------------------------------------------------------

    def iter_events(
        self, strategy_id: str, run_id: str,
    ) -> Iterator[dict]:
        path = self.events_path(strategy_id, run_id)
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def iter_fills(
        self, strategy_id: str, run_id: str,
    ) -> Iterator[dict]:
        path = self.fills_path(strategy_id, run_id)
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _write_run(self, run: ShadowRun) -> None:
        path = self.run_record_path(run.strategy_id, run.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(run.asdict(), indent=2), encoding="utf-8")

    def _append_index(self, run: ShadowRun, *, kind: str) -> None:
        path = self.index_path(run.strategy_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": _now_iso(),
                "kind": kind,
                "run_id": run.run_id,
                "strategy_id": run.strategy_id,
                "candidate_id": run.candidate_id,
                "status": run.status,
            }, ensure_ascii=False) + "\n")


__all__ = ["ShadowStore"]
