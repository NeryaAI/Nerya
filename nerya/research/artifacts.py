"""Artifact path helpers.

research runtime spec §3.3 mandates this directory layout:

```
workspace/strategies/<strategy_id>/candidates/<candidate_id>/
    candidate.yml
    signal_engine.py
    backtest_config.yml
    validation_report.json
    artifacts/
      equity_curve.csv
      trades.csv
      metrics.json
      validation.json
      report.md
workspace/strategies/<strategy_id>/validation/latest.json
workspace/strategies/<strategy_id>/validation/history.jsonl
workspace/strategies/<strategy_id>/shadow/runs/<run_id>/
```

Every helper here resolves paths *only* under the workspace strategies
tree.  Path traversal inputs (``../``, absolute paths) are rejected so
research code can never write outside the candidate directory.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from ..core.errors import NeryaError


class ArtifactPathError(NeryaError):
    """Raised when a strategy/candidate/run id is unsafe."""


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")


def _safe_id(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ArtifactPathError(f"{label}_required")
    if value != value.strip():
        raise ArtifactPathError(f"{label}_whitespace:{value!r}")
    if any(token in value for token in ("/", "\\", "..", "\x00")):
        raise ArtifactPathError(f"{label}_path_traversal:{value!r}")
    if not _SAFE_ID_RE.match(value):
        raise ArtifactPathError(f"{label}_invalid:{value!r}")
    return value


def workspace_strategies_root(workspace: str | Path) -> Path:
    return Path(workspace) / "strategies"


def strategy_dir(workspace: str | Path, strategy_id: str) -> Path:
    sid = _safe_id(strategy_id, label="strategy_id")
    return workspace_strategies_root(workspace) / sid


def candidate_dir(
    workspace: str | Path, strategy_id: str, candidate_id: str
) -> Path:
    sid = _safe_id(strategy_id, label="strategy_id")
    cid = _safe_id(candidate_id, label="candidate_id")
    return workspace_strategies_root(workspace) / sid / "candidates" / cid


def candidate_artifact_dir(
    workspace: str | Path, strategy_id: str, candidate_id: str
) -> Path:
    return candidate_dir(workspace, strategy_id, candidate_id) / "artifacts"


def candidate_report_path(
    workspace: str | Path, strategy_id: str, candidate_id: str
) -> Path:
    return candidate_dir(workspace, strategy_id, candidate_id) \
        / "validation_report.json"


def candidate_signal_engine_path(
    workspace: str | Path, strategy_id: str, candidate_id: str
) -> Path:
    return candidate_dir(workspace, strategy_id, candidate_id) \
        / "signal_engine.py"


def validation_history_path(
    workspace: str | Path, strategy_id: str
) -> Path:
    return strategy_dir(workspace, strategy_id) / "validation" / "history.jsonl"


def validation_latest_path(
    workspace: str | Path, strategy_id: str
) -> Path:
    return strategy_dir(workspace, strategy_id) / "validation" / "latest.json"


def shadow_run_dir(
    workspace: str | Path, strategy_id: str, run_id: str
) -> Path:
    sid = _safe_id(strategy_id, label="strategy_id")
    rid = _safe_id(run_id, label="run_id")
    return workspace_strategies_root(workspace) / sid / "shadow" / "runs" / rid


def ensure_dirs(paths: Iterable[Path]) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


__all__ = [
    "ArtifactPathError",
    "candidate_artifact_dir",
    "candidate_dir",
    "candidate_report_path",
    "candidate_signal_engine_path",
    "ensure_dirs",
    "shadow_run_dir",
    "strategy_dir",
    "validation_history_path",
    "validation_latest_path",
    "workspace_strategies_root",
]
