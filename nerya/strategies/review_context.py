"""Frozen, strategy-local evidence for tuning reviews.

This module is the only evidence-selection seam used by strategy evolution.
It never reads chat history, global memory, operator profiles, or generic
agent journals. Strategy run records are the authoritative spine; ledger and
post-apply rows must join to selected run/session ids or they fail closed.
"""

from __future__ import annotations

import fnmatch
import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ..core import jsonl
from ..core.paths import WorkspacePaths
from ..core.time import now
from .package import StrategyPackage, resolve_package_relative_path
from .performance import (
    STRATEGY_REVIEW_LEDGER_NAMES,
    StrategyPerformanceSnapshot,
    _compose_snapshot,
    read_strategy_ledger,
)
from .state import StrategyRunRecord, StrategyRunStore


@dataclass(frozen=True)
class StrategyReviewPolicy:
    """Selection policy for a reproducible, strategy-local review."""

    lookback_runs: int = 200
    max_age_hours: int = 168
    execution_mode: str = "paper"
    run_ids: tuple[str, ...] = ()
    session_ids: tuple[str, ...] = ()
    allowed_targets: tuple[str, ...] = ()
    forbidden_targets: tuple[str, ...] = ()
    max_source_bytes: int = 262_144


@dataclass(frozen=True)
class _RunScope:
    strategy_id: str
    package_hash: str
    execution_mode: str
    cutoff: datetime
    anchor: datetime
    requested_run_ids: frozenset[str]
    requested_session_ids: frozenset[str]


@dataclass(frozen=True)
class _RowScope:
    strategy_id: str
    package_hash: str
    execution_mode: str
    run_ids: frozenset[str]
    session_ids: frozenset[str]


def build_strategy_review_context(
    paths: WorkspacePaths,
    package: StrategyPackage,
    *,
    policy: StrategyReviewPolicy,
    config_like: Any | None = None,
) -> StrategyPerformanceSnapshot:
    """Return one frozen tuning snapshot from attributable strategy evidence."""

    anchor = _utc(now())
    lookback_runs = max(1, int(policy.lookback_runs or 1))
    max_age_hours = max(1, int(policy.max_age_hours or 1))
    run_scope = _RunScope(
        strategy_id=package.strategy_id,
        package_hash=package.content_hash,
        execution_mode=policy.execution_mode,
        cutoff=anchor - timedelta(hours=max_age_hours),
        anchor=anchor,
        requested_run_ids=frozenset(_clean_ids(policy.run_ids)),
        requested_session_ids=frozenset(_clean_ids(policy.session_ids)),
    )
    excluded_runs: Counter[str] = Counter()
    eligible: list[tuple[datetime, StrategyRunRecord]] = []
    for record in StrategyRunStore(paths, package.strategy_id).list(limit=0):
        reason = _run_exclusion_reason(record, run_scope)
        if reason:
            excluded_runs[reason] += 1
            continue
        observed_at = _record_time(record)
        if observed_at is None:
            excluded_runs["invalid_timestamp"] += 1
            continue
        eligible.append((observed_at, record))

    eligible.sort(key=lambda item: (item[0], item[1].run_id), reverse=True)
    if len(eligible) > lookback_runs:
        excluded_runs["lookback_limit"] += len(eligible) - lookback_runs
    runs = [record for _observed_at, record in eligible[:lookback_runs]]
    selected_run_ids = [record.run_id for record in runs]
    selected_session_ids = _selected_session_ids(runs)
    row_scope = _RowScope(
        strategy_id=package.strategy_id,
        package_hash=package.content_hash,
        execution_mode=policy.execution_mode,
        run_ids=frozenset(selected_run_ids),
        session_ids=frozenset(selected_session_ids),
    )

    ledgers: dict[str, list[dict[str, Any]]] = {}
    excluded_ledgers: dict[str, dict[str, int]] = {}
    for name in STRATEGY_REVIEW_LEDGER_NAMES:
        included, excluded = _filter_review_rows(
            read_strategy_ledger(paths, package.strategy_id, name),
            row_scope,
        )
        ledgers[name] = included
        excluded_ledgers[name] = dict(sorted(excluded.items()))

    evolution_rows, evolution_excluded = _filter_review_rows(
        [
            row
            for row in jsonl.read_all(paths.journal("evolution"))
            if row.get("kind") == "proposal.post_apply_observation"
        ],
        row_scope,
    )
    evidence_scope = {
        "version": "strategy_review_evidence_v1",
        "strategy_id": package.strategy_id,
        "package_hash": package.content_hash,
        "execution_mode": policy.execution_mode,
        "lookback_runs": lookback_runs,
        "max_age_hours": max_age_hours,
        "window_started_at": run_scope.cutoff.isoformat(),
        "window_ended_at": anchor.isoformat(),
        "requested_run_ids": sorted(run_scope.requested_run_ids),
        "requested_session_ids": sorted(run_scope.requested_session_ids),
        "selected_run_ids": selected_run_ids,
        "selected_session_ids": selected_session_ids,
        "ledger_row_counts": {
            name: len(rows) for name, rows in sorted(ledgers.items())
        },
        "excluded_run_counts": dict(sorted(excluded_runs.items())),
        "excluded_ledger_counts": excluded_ledgers,
        "excluded_evolution_counts": dict(sorted(evolution_excluded.items())),
    }
    return _compose_snapshot(
        paths,
        package.strategy_id,
        package=package,
        lookback_runs=lookback_runs,
        runs=runs,
        ledgers=ledgers,
        evolution_rows=evolution_rows,
        package_context=_build_package_context(package, policy=policy),
        evidence_scope=evidence_scope,
        config_like=config_like,
    )


def _run_exclusion_reason(
    record: StrategyRunRecord,
    scope: _RunScope,
) -> str | None:
    if record.strategy_id != scope.strategy_id:
        return "strategy_id"
    if record.package_hash != scope.package_hash:
        return "package_hash"
    if record.mode != scope.execution_mode:
        return "execution_mode"
    observed_at = _record_time(record)
    if observed_at is None:
        return "invalid_timestamp"
    if observed_at < scope.cutoff or observed_at > scope.anchor:
        return "max_age"
    if scope.requested_run_ids and record.run_id not in scope.requested_run_ids:
        return "requested_run_ids"
    if scope.requested_session_ids and not (
        scope.requested_session_ids & set(_record_session_ids(record))
    ):
        return "requested_session_ids"
    return None


def _filter_review_rows(
    rows: Iterable[dict[str, Any]],
    scope: _RowScope,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    included: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    for row in rows:
        if not isinstance(row, dict):
            excluded["invalid_row"] += 1
            continue
        strategy_values = _identity_values(row, "strategy_id")
        package_values = _identity_values(row, "package_hash")
        mode_values = _identity_values(row, "mode")
        if strategy_values and strategy_values != {scope.strategy_id}:
            excluded["strategy_id"] += 1
            continue
        if package_values and package_values != {scope.package_hash}:
            excluded["package_hash"] += 1
            continue
        if mode_values and mode_values != {scope.execution_mode}:
            excluded["execution_mode"] += 1
            continue

        row_run_ids = _identity_values(row, "run_id")
        row_session_ids = _identity_values(row, "session_id")
        if not row_run_ids and not row_session_ids:
            excluded["unattributed"] += 1
            continue
        if row_run_ids and not row_run_ids.issubset(scope.run_ids):
            excluded["unselected_run_id"] += 1
            continue
        if row_session_ids and not row_session_ids.issubset(scope.session_ids):
            excluded["unselected_session_id"] += 1
            continue
        included.append(row)
    return included, excluded


def _identity_values(row: dict[str, Any], key: str) -> set[str]:
    values: set[str] = set()
    _add_identity_value(values, row.get(key))
    for block_name in ("identity", "provenance", "metadata", "metrics", "decision"):
        block = row.get(block_name)
        if isinstance(block, dict):
            _add_identity_value(values, block.get(key))
    return values


def _record_time(record: StrategyRunRecord) -> datetime | None:
    return _parse_time(record.finished_at) or _parse_time(record.started_at)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _utc(datetime.fromisoformat(text))
    except (TypeError, ValueError):
        return None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clean_ids(values: Iterable[str]) -> set[str]:
    return {
        str(value).strip()
        for value in values or ()
        if str(value or "").strip()
    }


def _record_session_ids(record: StrategyRunRecord) -> list[str]:
    values = [str(record.session_id or "").strip()]
    result = record.outputs.get("result") if isinstance(record.outputs, dict) else None
    if isinstance(result, dict):
        values.append(str(result.get("session_id") or "").strip())
    return _unique_strings(values)


def _selected_session_ids(runs: list[StrategyRunRecord]) -> list[str]:
    return _unique_strings([
        session_id
        for record in runs
        for session_id in _record_session_ids(record)
    ])


def _add_identity_value(values: set[str], value: Any) -> None:
    text = str(value or "").strip()
    if text:
        values.add(text)


def _unique_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _build_package_context(
    package: StrategyPackage,
    *,
    policy: StrategyReviewPolicy,
) -> dict[str, Any]:
    allowed = _clean_patterns(policy.allowed_targets)
    forbidden = _clean_patterns(policy.forbidden_targets)
    budget = max(0, int(policy.max_source_bytes or 0))
    files: dict[str, dict[str, Any]] = {}
    excluded: dict[str, str] = {}
    used = 0
    tuner_prompt_path = ""
    if package.manifest.tuning.enabled:
        resolved_prompt = resolve_package_relative_path(
            package,
            package.manifest.tuning.subagent.prompt_file,
        )
        if resolved_prompt is not None and resolved_prompt[1] in package.files:
            tuner_prompt_path = resolved_prompt[1]
    ordered_files = sorted(
        package.files,
        key=lambda relative_path: (
            relative_path != tuner_prompt_path,
            relative_path,
        ),
    )
    for relative_path in ordered_files:
        is_tuner_prompt = relative_path == tuner_prompt_path
        if not is_tuner_prompt and not any(
            fnmatch.fnmatchcase(relative_path, pattern) for pattern in allowed
        ):
            continue
        if not is_tuner_prompt and any(
            fnmatch.fnmatchcase(relative_path, pattern) for pattern in forbidden
        ):
            excluded[relative_path] = "forbidden_target"
            continue
        path = package.root / relative_path
        try:
            blob = path.read_bytes()
        except OSError:
            excluded[relative_path] = "unreadable"
            continue
        if used + len(blob) > budget:
            excluded[relative_path] = "source_budget_exceeded"
            continue
        try:
            content = blob.decode("utf-8")
        except UnicodeDecodeError:
            excluded[relative_path] = "non_utf8"
            continue
        files[relative_path] = {
            "sha256": hashlib.sha256(blob).hexdigest(),
            "bytes": len(blob),
            "content": content,
        }
        used += len(blob)
    tuner_prompt = (
        {"path": tuner_prompt_path}
        if tuner_prompt_path in files
        else {}
    )
    return {
        "version": "strategy_review_package_context_v1",
        "package_hash": package.content_hash,
        "allowed_targets": list(allowed),
        "forbidden_targets": list(forbidden),
        "max_source_bytes": budget,
        "source_bytes": used,
        "files": files,
        "tuner_prompt": tuner_prompt,
        "excluded_files": excluded,
    }


def _clean_patterns(patterns: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        str(pattern).strip()
        for pattern in patterns or ()
        if str(pattern or "").strip()
    )


__all__ = [
    "StrategyReviewPolicy",
    "build_strategy_review_context",
]
