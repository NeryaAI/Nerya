"""Run strategy-local freeform research backtests.

This runner is for strategies whose evidence is not naturally expressed as
the stock OHLCV replay: meme smart-money, wallet-flow, event, social, or other
SDK-heavy research. The strategy package owns the research code; Nerya only
executes it and normalises the minimum artifact contract:

* ``equity.csv`` with ``ts``/``time`` and ``equity``/``value`` columns.
* ``trades.csv`` with trade-detail rows, or at least a header when no trades
  were produced.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .....core.config import load_config as load_workspace_config
from .....core.errors import TradingError
from .....core.redaction import redact_display_dict, redact_public_record, redact_text
from .freeform_sandbox import execute_freeform, uses_host_lp_replay
from .backtest_run import (
    _load_target_package,
    _metrics_display,
    _operator_summary,
    _operator_summary_text,
)


class NoFreeformBacktestScript(TradingError):
    """Raised when a strategy package has no freeform backtest entrypoint."""


class FreeformDependencyError(TradingError):
    """An actionable environment failure, not invalid strategy parameters."""

    def __init__(self, module: str, script: Path) -> None:
        self.module = module
        super().__init__(f"Freeform replay {script.name} requires unavailable module {module!r} in the Nerya interpreter. "
            "Do not repeat the unchanged backtest. Use engine='native' only when the saved main entrypoint implements the same strategy, "
            "or repair the custom replay to use available providers/dependencies. Do not delete the strategy or weaken its trading rules.")


SCRIPT_CANDIDATES = (
    "backtests/research_backtest.py",
    "backtests/freeform_backtest.py",
    "backtests/custom_backtest.py",
    "scripts/research_backtest.py",
    "scripts/freeform_backtest.py",
    "scripts/custom_backtest.py",
    "scripts/custom_replay.py",
)

RESULT_NAMES = (
    "freeform_backtest_result.json",
    "custom_backtest_result.json",
    "research_backtest_result.json",
    "result.json",
)

REPORT_NAMES = (
    "freeform_backtest_report.md",
    "custom_backtest_report.md",
    "research_backtest_report.md",
    "report.md",
)

EQUITY_NAMES = ("equity.csv", "equity_curve.csv", "nav.csv")
TRADES_NAMES = ("trades.csv", "trade_details.csv", "fills.csv")
DECISION_NAMES = ("decisions.csv",)
TIME_KEYS = ("ts", "time", "timestamp", "date", "datetime", "t")
EQUITY_KEYS = ("equity", "value", "balance", "nav")


def has_freeform_backtest_script(strategy_root: str | Path) -> bool:
    return _find_script(Path(strategy_root)) is not None


def _redact_equity_row(row):
    row = dict(row)
    for key in EQUITY_KEYS:
        if key in row:
            value = _float_or_none(row[key])
            row[key] = value if value is not None and math.isfinite(value) else None
    return redact_display_dict(row)


def _redact_artifact(name, text, prepared):
    if name in RESULT_NAMES:
        return json.dumps(_redact_replay_payload(json.loads(text), prepared), ensure_ascii=False)
    if name in EQUITY_NAMES:
        reader = csv.DictReader(io.StringIO(text))
        rows = [_redact_equity_row(row) for row in reader]
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=list(redact_display_dict(
            {key: None for key in reader.fieldnames or []})))
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue()
    if name in REPORT_NAMES:
        # Shield only standalone denominations admitted by the JSON path's
        # host receipt/hash checks; embedded secret-shaped strings stay redacted.
        marker = '__nerya_public_denomination_'
        while marker in text:
            marker += '_'
        identities = {}
        def protect(match):
            value = match.group()
            token = value.rsplit(':', 1)[-1]
            for role in ('token0', 'token1'):
                currency = role + ':' + token
                if value not in (token, currency):
                    continue
                clean = _redact_replay_payload(
                    {'quote_token': token, 'accounting_currency': currency}, prepared)
                if clean.get('quote_token') == token and clean.get('accounting_currency') == currency:
                    placeholder = marker + str(len(identities)) + '__'
                    identities[placeholder] = value
                    return placeholder
            return value
        text = re.sub(r'(?<!\S)(?:token[01]:)?0x[0-9a-fA-F]{40}(?!\S)', protect, text)
        text = redact_text(text)
        for placeholder, value in identities.items():
            text = text.replace(placeholder, value)
        return text
    return redact_text(text)


def _redact_replay_payload(payload, prepared):
    """Restore only the denomination bound to host-prepared public pool data."""
    import hashlib
    clean = redact_display_dict(payload)
    if not isinstance(payload, dict):
        return clean
    for key in ('equity_curve', 'equity', 'nav_curve'):
        if isinstance(payload.get(key), list):
            clean[key] = [_redact_equity_row(row) for row in _payload_rows(payload, (key,))]
    for receipt in prepared['receipts']:
        if receipt.get('provider') != 'uniswap' or receipt.get('action') != 'pool_history':
            continue
        blob = prepared['files'].get(receipt['id'] + '.json')
        if blob is None or hashlib.sha256(blob).hexdigest() != receipt['sha256']:
            continue
        dataset = json.loads(blob)
        envelope = dataset.get('_envelope', {})
        cadence = dataset.get('protocol') == 'nerya.lp_cadence.v1'
        pool = dataset.get('pool', {}) if cadence else envelope.get('pool', {})
        if ((not cadence and (envelope.get('provider') != 'uniswap' or envelope.get('action') != 'pool_history'
                or envelope.get('row_contract') != 'uniswap_v3_pool_history.v1'
                or envelope.get('mock') is not False or envelope.get('chain_id') != 1))
                or pool.get('chain_id') != 1
                or any(not isinstance(pool.get(key), str)
                       or not re.fullmatch(r'0x[0-9a-fA-F]{40}', pool[key])
                       or int(pool[key], 16) == 0 for key in ('token0', 'token1'))):
            continue
        for role in ('token0', 'token1'):
            token = pool[role]
            if (payload.get('quote_token') == token
                    and payload.get('accounting_currency') == role + ':' + token):
                clean.update(redact_public_record(
                    {key: payload[key] for key in ('quote_token', 'accounting_currency')},
                    identity_fields=frozenset({'quote_token', 'accounting_currency'})))
    return clean


def run_freeform_backtest(
    *,
    strategy_id: str | None = None,
    proposal_id: str | None = None,
    workspace: str | Path | None = None,
    timeout_seconds: int = 180,
    cancel_token: object | None = None,
    progress=None,
    validate_prepared_inputs=None,
    runtime_config=None,
) -> dict[str, Any]:
    from .run_state import RunTracker
    tracker = RunTracker(progress=progress, cancel_token=cancel_token)
    try:
        return _run_freeform_backtest(strategy_id=strategy_id, proposal_id=proposal_id,
            workspace=workspace, timeout_seconds=timeout_seconds, cancel_token=cancel_token, tracker=tracker,
            validate_prepared_inputs=validate_prepared_inputs, runtime_config=runtime_config)
    except BaseException as exc:
        tracker.fail(exc)
        raise


def _run_freeform_backtest(*, strategy_id, proposal_id, workspace, timeout_seconds, cancel_token, tracker,
                           validate_prepared_inputs=None, runtime_config=None):
    """Execute the strategy package's freeform backtest script.

    The script is run from a read-only snapshot with these environment variables:
    ``NERYA_BACKTEST_OUT_DIR``, ``NERYA_STRATEGY_ROOT``, and
    ``NERYA_WORKSPACE`` (empty scratch, never the runtime workspace). Prepared
    package history is readable; network and child processes are denied. Only
    the dedicated output directory and scratch are writable. CSV artifacts or
    JSON with ``equity_curve``/``trades`` remain supported, including stdout.
    macOS only, with no unsafe fallback; wall time is capped at the shared
    validation limit. This is isolation evidence, not performance attestation.
    """

    if bool(strategy_id) == bool(proposal_id):
        raise TradingError("exactly one of strategy_id or proposal_id is required")

    config_obj = load_workspace_config(Path(workspace).expanduser() if workspace else None)
    package = _load_target_package(config_obj.paths, strategy_id, proposal_id)
    root = config_obj.paths.root.resolve()
    if not package.root.resolve().is_relative_to(root):
        raise TradingError('freeform package must stay inside the workspace')
    if any(path.is_symlink() for path in [package.root, *package.root.parents] if path != root and path.is_relative_to(root)):
        raise TradingError('freeform package path must not contain symlinks')
    script_path = _find_script(package.root)
    if script_path is None:
        raise NoFreeformBacktestScript(
            f"no freeform backtest script found under {package.root}"
        )
    from .writers import create_run_dir
    from .run_state import atomic_json
    from .replay_inputs import prepare_inputs
    from .....strategies.verification import source_revision
    from .....strategies.workflow_service import _read_files
    from .....core.runtime_identity import BUILD_ID, SDK_BUILD_ID
    files, omitted = _read_files(package.root)
    if omitted:
        raise TradingError('custom replay failed closed: source cannot be completely snapshotted')
    revision = source_revision(files)
    out_dir = create_run_dir(package.root, kind='freeform')
    ts_name = out_dir.name
    tracker.bind(out_dir, strategy_id=package.manifest.strategy_id, proposal_id=proposal_id)
    tracker.phase('preparing_data')
    cadence = None
    lp_mode = package.manifest.extras.get('params', {}).get('lp_replay', {}).get('mode')
    if lp_mode == 'cadence' and uses_host_lp_replay(package, script_path):
        from .lp_state_requirements import prepare_lp_cadence
        from .lp_replay import cadence_receipt
        from .replay_inputs import json_bytes, read_manifest
        from .....harness.cancellation import raise_if_cancelled
        from .....data_api.uniswap_history import UniswapHistoryProvider
        import hashlib
        import time
        target = {'proposal_id': proposal_id} if proposal_id else {'strategy_id': strategy_id}
        cadence = prepare_lp_cadence(config_obj, target, lambda: raise_if_cancelled(cancel_token))
        if cadence['source_revision'] != revision:
            raise TradingError('LP source changed during preparation')
        compact = cadence_receipt(cadence)
        pool = cadence['observations'][0]['checkpoint']['pool']
        identities, catalog = UniswapHistoryProvider()._token_catalog(
            pool, time.monotonic() + 15, cancel_token=cancel_token)
        if not identities:
            raise TradingError('LP cadence token catalog identity unavailable')
        compact['_envelope'] = {'provider': 'uniswap', 'action': 'pool_history', 'mock': False,
            'row_contract': 'nerya.lp_cadence.v1', 'observation_kind': 'historical_lp_cadence',
            'pool_identity_scope': 'historical_onchain_pool_with_current_token_catalog',
            'chain_id': 1, 'pool': pool, 'token_identities': identities,
            'sources': [{'sha256': compact['prepared_sha256']}, catalog],
            'requested_window': cadence['requested_window']}
        blob = json_bytes(compact)
        if len(blob) > 4_000_000:
            raise TradingError('LP cadence evidence receipt exceeds byte limit')
        name = package.manifest.extras['params']['lp_replay']['dataset_id']
        prepared = {'files': {name + '.json': blob}, 'manifest': read_manifest(package.root), 'receipts': [{
            'id': name, 'provider': 'uniswap', 'action': 'pool_history',
            'sha256': hashlib.sha256(blob).hexdigest(), 'rows': compact['observation_count'],
            'sources': compact['_envelope']['sources'], 'requested_window': cadence['requested_window'],
            'coverage': {'completeness': 'complete'}, 'requested_window_complete': True,
            'snapshot_file': 'prepared_inputs/' + name + '.json',
            'observation_kind': 'historical_lp_cadence', 'data_kind': 'historical_observations'}]}
    else:
        prepared = prepare_inputs(package.root, cancel_token=cancel_token, config=runtime_config or config_obj)
    if validate_prepared_inputs is not None:
        validate_prepared_inputs(prepared)
    if prepared["files"]:
        persisted_inputs = out_dir / "prepared_inputs"
        persisted_inputs.mkdir()
        for name, blob in prepared["files"].items():
            (persisted_inputs / name).write_bytes(blob)
    atomic_json(out_dir / 'data_manifest.json', {'datasets': prepared['receipts']})
    tracker.phase('executing')

    proc, artifacts, isolation = execute_freeform(
        package, script_path, artifact_names=(*RESULT_NAMES, *REPORT_NAMES, *EQUITY_NAMES, *TRADES_NAMES, *DECISION_NAMES),
        timeout_seconds=timeout_seconds, cancel_token=cancel_token,
        prepared_inputs=prepared['files'], **({'trusted_lp_cadence': cadence} if cadence is not None else {}),
    )
    if proc.returncode != 0:
        missing = re.search(r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]", proc.stderr or "")
        if missing:
            raise FreeformDependencyError(redact_text(missing.group(1)), script_path)
        raise TradingError(
            "freeform backtest script failed "
            f"({script_path}, exit={proc.returncode}): "
            f"{redact_text(_tail(proc.stderr or proc.stdout))}"
        )

    tracker.phase('writing_artifacts')
    for name, data in artifacts.items():
        # Persist only bounded regular artifacts after the isolated child exits.
        text = data.decode('utf-8')
        text = _redact_artifact(name, text, prepared)
        (out_dir / name).write_text(text, encoding='utf-8')
    payload = _read_first_json(out_dir, RESULT_NAMES)
    if not payload:
        payload = _redact_replay_payload(_read_payload_from_stdout(proc.stdout or ""), prepared)
    equity_path = _find_artifact(out_dir, EQUITY_NAMES)
    trades_path = _find_artifact(out_dir, TRADES_NAMES)

    if equity_path is None:
        equity_rows = _payload_rows(payload, ("equity_curve", "equity", "nav_curve"))
        if equity_rows:
            equity_path = out_dir / "equity.csv"
            _write_rows(equity_path, equity_rows, fallback_header=("ts", "equity"))
    if trades_path is None:
        trade_rows = _payload_rows(payload, ("trades", "trade_details", "fills"))
        trades_path = out_dir / "trades.csv"
        _write_rows(
            trades_path,
            trade_rows,
            fallback_header=("ts", "side", "price", "size", "equity", "reason"),
        )

    if equity_path is None or not equity_path.exists():
        raise TradingError(
            "freeform backtest must produce equity.csv, equity_curve.csv, "
            "or result.json with an equity_curve array"
        )
    if trades_path is None or not trades_path.exists():
        raise TradingError(
            "freeform backtest must produce trades.csv, trade_details.csv, "
            "or result.json with a trades array"
        )

    equity_rows = _read_csv(equity_path)
    trade_rows = _read_csv(trades_path)
    if not equity_rows:
        raise TradingError("freeform backtest equity file has no rows")

    equity_path = _canonicalise_csv(
        equity_path,
        out_dir / "equity.csv",
        equity_rows,
        fallback_header=("ts", "equity"),
    )
    trades_path = _canonicalise_csv(
        trades_path,
        out_dir / "trades.csv",
        trade_rows,
        fallback_header=("ts", "side", "price", "size", "equity", "reason"),
    )

    metrics = _build_metrics(
        payload=payload,
        equity_rows=equity_rows,
        trade_rows=trade_rows,
        strategy_id=str(package.manifest.strategy_id or ""),
        proposal_id=proposal_id,
        script_path=script_path,
    )
    current, omitted = _read_files(package.root)
    if omitted or source_revision(current) != revision:
        raise TradingError('custom replay source changed during execution')
    import hashlib
    if any(isolation['snapshot_files'].get(name) != hashlib.sha256(content.encode()).hexdigest()
           for name, content in files.items() if name != 'workflow.json'):
        raise TradingError('custom replay executed a different source snapshot')
    datasets = prepared['receipts']
    for receipt in datasets:
        if isolation['prepared_inputs'].get(receipt['snapshot_file']) != receipt['sha256']:
            raise TradingError('custom replay input snapshot mismatch')
    coverage = (False if any(row['requested_window_complete'] is False for row in datasets)
                else True if datasets and all(row['requested_window_complete'] is True for row in datasets) else None)
    windows=[row.get("requested_window") for row in datasets]
    if windows and all(isinstance(window,dict) and window==windows[0] for window in windows):
        requested_start=_coerce_unix_seconds(windows[0].get("start"))
        requested_end=_coerce_unix_seconds(windows[0].get("end"))
        if requested_start is not None and requested_end is not None and requested_end>=requested_start:
            metrics.update(requested_start_utc=_iso_utc(requested_start),requested_end_utc=_iso_utc(requested_end),
                requested_window_days=(requested_end-requested_start)/86400,
                requested_end_inclusive=windows[0].get("inclusive"))
    metrics.update(coverage_ok=coverage, requested_window_complete=coverage,
        provenance={'version': 1, 'strategy_id': package.manifest.strategy_id, 'proposal_id': proposal_id,
            'runtime_build_id': BUILD_ID, 'sdk_build_id': SDK_BUILD_ID, 'source_revision': revision,
            'source_changed_during_run': False, 'package_hash': package.content_hash,
            'replay_snapshot_sha256': isolation['snapshot_sha256'], 'datasets': datasets,
            "data_manifest": str((out_dir / "data_manifest.json").relative_to(root)),
            'data_kind': 'historical_observations' if datasets and all(row['data_kind'] == 'historical_observations' for row in datasets) else 'unverified',
            'scope': 'Isolated custom calculation; no execution-model, strategy equivalence or historical performance attestation.'},
        replay={'process_exited': True, 'process_group_stopped': True, 'errors': 0,
            'scope': 'isolated_custom_calculation', 'strategy_execution_verified': False})
    if coverage is False:
        metrics['coverage_message'] = 'Prepared observations have coverage gaps; the requested historical window is incomplete.'
    if isolation.get('lp_execution'):
        metrics['replay'].update(strategy_execution_verified=True, accounting_verified=True, **isolation['lp_execution'])
        metrics['coverage_message'] = (
            'Sampled LP strategy execution and accounting are verified. Requested-window completeness '
            'remains false: boundary gaps and unobserved state/tick transitions between samples are '
            'separate limitations. Changing step_blocks, max_samples, or endpoint alignment alone '
            'cannot establish full LP coverage. Full coverage requires an admitted historical '
            'event/state producer and validated reconstruction for the same pool and window.')
        metrics['provenance']['scope'] = (
            'Saved entrypoint executed in isolation; host-owned sampled fixed-range LP accounting. '
            'Partial observations only; no fills, complete tick path or performance attestation.')
        if cadence is not None:
            metrics['requested_window_complete'] = metrics['coverage_ok'] = isolation['lp_execution']['requested_window_complete']
            metrics['coverage_message'] = ('Saved cadence states and intervening range continuity are verified.'
                if metrics['coverage_ok'] else 'Saved policy max_gap_seconds omitted fee intervals; accounting remains partial.')
            metrics['provenance']['scope'] = (
                'Saved scheduled decisions executed in isolation with verified historical checkpoints and range continuity; '
                'host-owned fixed-range counterfactual accounting. No live fills or historical performance attestation.')
    metrics_path = out_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    report_path = _find_artifact(out_dir, REPORT_NAMES)
    if report_path is None:
        report_path = out_dir / "report.md"
        report_path.write_text(_render_report(metrics), encoding="utf-8")

    chart = _render_chart(
        out_dir=out_dir,
        strategy_id=str(package.manifest.strategy_id or ""),
        backtest_ts=ts_name,
        equity_rows=equity_rows,
        trade_rows=trade_rows,
        metrics=metrics,
        workspace_root=config_obj.paths.root,
    )
    decisions_path = _find_artifact(out_dir, DECISION_NAMES)
    if decisions_path is not None:
        decision_rows = _read_csv(decisions_path)
        chart['tables'].append(_table('decisions', decision_rows[:100]))
        chart['meta']['recorded_decision_rows'] = len(decision_rows)
    chart_path = out_dir / "chart.json"
    chart_path.write_text(
        json.dumps(chart, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    def _ws_rel(p: Path) -> str:
        # Workspace-relative form for reply-visible informational fields so
        # absolute host paths (C:\Users\...) stop leaking into operator
        # replies. Locator fields that tools/tests resolve directly
        # (out_dir, metrics_path, equity/trades/chart paths) stay absolute.
        root = config_obj.paths.root
        return str(p.relative_to(root)) if p.is_relative_to(root) else str(p)

    result_path = out_dir / "freeform_backtest_result.json"
    result_payload = {
        "ok": True,
        "kind": "freeform_backtest",
        "replay_kind": "strategy_sdk_freeform",
        "strategy_id": package.manifest.strategy_id,
        "proposal_id": proposal_id,
        "backtest_ts": ts_name,
        "script_path": str(script_path),
        "out_dir": str(out_dir),
        "metrics_path": str(metrics_path),
        "report_path": _ws_rel(report_path),
        "equity_path": str(equity_path),
        "trades_path": str(trades_path),
        "chart_path": str(chart_path),
        "has_equity_curve": True,
        "has_trade_details": True,
        "equity_points": len(equity_rows),
        "simulated_trades": len(trade_rows),
        "total_trades": len(trade_rows),
        "total_return_pct": metrics.get("total_return_pct"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "final_equity_usd": metrics.get("final_equity_usd"),
        **{key:metrics[key] for key in ('accounting_currency', 'modeled_initial_value', 'modeled_final_value') if key in metrics},
        "coverage_ok": metrics["coverage_ok"],
        "requested_window_complete": metrics["requested_window_complete"],
        "performance_evidence": metrics["performance_evidence"],
        "recommended_coverage_ok": None,
        "coverage_message": metrics.get("coverage_message"),
        **{key:metrics.get(key) for key in ("requested_window_days","requested_start_utc",
            "requested_end_utc","requested_end_inclusive")},
        "limitations": [*(payload.get("limitations", []) if isinstance(payload.get("limitations"), list) else []),
                        "Historical data provenance, requested coverage and trading performance are not verified."],
        "isolation": isolation,
        "run_receipt": _ws_rel(out_dir / 'run.json'),
        "provenance": metrics['provenance'],
        "replay": metrics['replay'],
    }
    result_path.write_text(
        json.dumps(result_payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    metrics_display = _metrics_display(metrics)
    operator_summary = _operator_summary(metrics)
    result = {
        **result_payload,
        "result_path": str(result_path),
        "strategy_root": _ws_rel(package.root),
        "strategy_yml_path": _ws_rel(package.root / "strategy.yml"),
        "strategy_md_path": _ws_rel(package.root / "strategy.md"),
        "main_path": _ws_rel(package.root / "main.py"),
        "verdict": metrics.get("verdict"),
        "metrics_display": metrics_display,
        "operator_summary": operator_summary,
        "operator_summary_text": _operator_summary_text(operator_summary),
        "metric_units": {
            "*_pct": "percentage points; display 0.15 as 0.15%, not 15%",
            "*_usd": "US dollars",
            "total_trades": "trade-detail rows emitted by the freeform script",
        },
        "metrics": metrics,
        "chart_panels": len(chart.get("panels", [])),
    }
    if chart.get("chart_blocks"):
        result["chart_blocks"] = chart["chart_blocks"]
    tracker.complete(source_revision=revision, snapshot_sha256=isolation['snapshot_sha256'],
                     report_path=str(report_path), performance_evidence=False)
    return result


def _find_script(strategy_root: Path) -> Path | None:
    for rel in SCRIPT_CANDIDATES:
        path = strategy_root / rel
        if path.exists() and path.is_file():
            return path
    return None


def _tail(text: str, *, limit: int = 1200) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _fresh_file(path: Path, *, min_mtime: float | None = None) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if min_mtime is None:
        return True
    try:
        return path.stat().st_mtime >= min_mtime - 1.0
    except OSError:
        return False


def _read_first_json(
    root: Path,
    names: tuple[str, ...],
    *,
    min_mtime: float | None = None,
) -> dict[str, Any]:
    for name in names:
        path = root / name
        if not _fresh_file(path, min_mtime=min_mtime):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        return data if isinstance(data, dict) else {}
    return {}


def _read_payload_from_stdout(stdout: str) -> dict[str, Any]:
    """Extract a freeform result object from script stdout.

    Strategy-local scripts are validated like strategy code, so they should not
    need direct file writes just to hand a replay result back to the runner.
    Accept either a marked JSON line or a final raw JSON object.
    """

    text = str(stdout or "").strip()
    if not text:
        return {}
    marker = "NERYA_FREEFORM_RESULT_JSON="
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(marker):
            return _loads_json_object(stripped[len(marker):].strip())
    parsed = _loads_json_object(text)
    if parsed:
        return parsed
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            data, _end = decoder.raw_decode(text[idx:])
        except Exception:
            continue
        if isinstance(data, dict):
            candidates.append(data)
    for data in reversed(candidates):
        if any(key in data for key in ("equity_curve", "equity", "nav_curve")):
            return data
    if candidates:
        return candidates[-1]
    return {}


def _loads_json_object(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _find_artifact(
    root: Path,
    names: tuple[str, ...],
    *,
    min_mtime: float | None = None,
) -> Path | None:
    for name in names:
        path = root / name
        if _fresh_file(path, min_mtime=min_mtime):
            return path
    return None


def _payload_rows(payload: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            rows: list[dict[str, Any]] = []
            for idx, row in enumerate(value):
                if isinstance(row, dict):
                    rows.append(dict(row))
                elif key in {"equity_curve", "equity", "nav_curve"}:
                    rows.append({"ts": idx, "equity": row})
            if rows:
                return rows
    return []


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_rows(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    fallback_header: tuple[str, ...],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = _fieldnames(rows) or list(fallback_header)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def _canonicalise_csv(
    source: Path,
    target: Path,
    rows: list[dict[str, Any]],
    *,
    fallback_header: tuple[str, ...],
) -> Path:
    if source.resolve() == target.resolve():
        return target
    if rows:
        _write_rows(target, rows, fallback_header=fallback_header)
    else:
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def _fieldnames(rows: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in rows:
        for key in row:
            if key not in out:
                out.append(key)
    return out


def _build_metrics(
    *,
    payload: dict[str, Any],
    equity_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
    strategy_id: str,
    proposal_id: str | None,
    script_path: Path,
) -> dict[str, Any]:
    first_value = _first_float(equity_rows[0], EQUITY_KEYS)
    last_value = _first_float(equity_rows[-1], EQUITY_KEYS)
    initial = _float_or_none(payload.get("initial_capital_usd")) or first_value or 0.0
    final = _float_or_none(payload.get("final_equity_usd")) or last_value or initial
    total_return = (
        _float_or_none(payload.get("total_return_pct"))
        if payload.get("total_return_pct") is not None
        else ((final - initial) / initial * 100.0 if initial else 0.0)
    )
    max_dd = _float_or_none(payload.get("max_drawdown_pct"))
    if max_dd is None:
        max_dd = _max_drawdown_pct(equity_rows)
    start_ts = _first_time(equity_rows[0])
    end_ts = _first_time(equity_rows[-1])
    days = ((end_ts - start_ts) / 86400.0) if start_ts and end_ts and end_ts > start_ts else 0.0
    result = {
        "backtest_kind": "freeform_backtest",
        "verdict": "RESEARCH",
        "strategy_id": strategy_id,
        "proposal_id": proposal_id,
        "script_path": str(script_path),
        "initial_capital_usd": initial,
        "final_equity_usd": final,
        "total_return_pct": total_return,
        "total_return_usd": final - initial,
        "benchmark_buy_hold_return_pct": payload.get("benchmark_buy_hold_return_pct"),
        "alpha_vs_benchmark_pct": payload.get("alpha_vs_benchmark_pct"),
        "max_drawdown_pct": max_dd,
        "sharpe_ratio": payload.get("sharpe_ratio"),
        "profit_factor": payload.get("profit_factor"),
        "win_rate_pct": payload.get("win_rate_pct"),
        "total_trades": len(trade_rows),
        "equity_points": len(equity_rows),
        "backtest_days": round(days, 6),
        "start_utc": _iso_utc(start_ts) if start_ts else None,
        "end_utc": _iso_utc(end_ts) if end_ts else None,
        "coverage_ok": None,
        "requested_window_complete": None,
        "performance_evidence": False,
        "recommended_coverage_ok": None,
        "coverage_message": (
            "Custom replay artifacts were generated; historical input provenance "
            "and requested-window coverage remain unverified. Script output and "
            "sandbox hashes do not attest historical trading performance."
        ),
        "flags": list(payload.get("flags") or []),
    }
    currency = payload.get('accounting_currency')
    if isinstance(currency, str) and currency.strip() and currency != 'USD':
        # Quote-token units are not USD, even for a token named USDC. Keep the
        # recorded curve without manufacturing dollar values or performance.
        result.update(accounting_currency=currency[:120], modeled_initial_value=first_value,
                      modeled_final_value=last_value)
        for key in ('initial_capital_usd', 'final_equity_usd', 'total_return_usd',
                    'total_return_pct', 'max_drawdown_pct', 'sharpe_ratio',
                    'profit_factor', 'win_rate_pct', 'benchmark_buy_hold_return_pct',
                    'alpha_vs_benchmark_pct'):
            result[key] = None
    return result


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_float(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _float_or_none(row.get(key))
        if value is not None:
            return value
    return None


def _first_time(row: dict[str, Any]) -> int | None:
    for key in TIME_KEYS:
        if key in row:
            return _coerce_unix_seconds(row.get(key))
    return None


def _coerce_unix_seconds(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 1e12:
            return int(raw / 1000.0)
        if raw > 0:
            return int(raw)
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return _coerce_unix_seconds(float(text))
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(text.replace("Z", "+0000"), fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


def _iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _max_drawdown_pct(equity_rows: list[dict[str, Any]]) -> float:
    peak: float | None = None
    max_dd = 0.0
    for row in equity_rows:
        value = _first_float(row, EQUITY_KEYS)
        if value is None:
            continue
        peak = value if peak is None else max(peak, value)
        if peak:
            max_dd = max(max_dd, (peak - value) / peak * 100.0)
    return max_dd


def _render_report(metrics: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Freeform SDK Backtest",
            "",
            f"- verdict: {metrics.get('verdict')}",
            f"- total_return_pct: {metrics.get('total_return_pct')}",
            f"- max_drawdown_pct: {metrics.get('max_drawdown_pct')}",
            f"- total_trades: {metrics.get('total_trades')}",
            f"- coverage: {metrics.get('coverage_message')}",
            "",
            "This is not a stock OHLCV-template replay. The strategy-local "
            "script supplied the capital curve and trade details.",
        ]
    )


def _render_chart(
    *,
    out_dir: Path,
    strategy_id: str,
    backtest_ts: str,
    equity_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
    metrics: dict[str, Any],
    workspace_root: Path,
) -> dict[str, Any]:
    equity = [
        {"time": ts, "value": value}
        for row in equity_rows
        if (ts := _first_time(row)) is not None
        if (value := _first_float(row, EQUITY_KEYS)) is not None
    ]
    chart = {
        "schema_version": "1.0",
        "meta": {
            "strategy_id": strategy_id,
            "backtest_ts": backtest_ts,
            "kind": "freeform_backtest",
            "start": metrics.get("start_utc"),
            "end": metrics.get("end_utc"),
            "initial_capital_usd": metrics.get("initial_capital_usd"),
            **{key: metrics[key] for key in ('accounting_currency', 'modeled_initial_value', 'modeled_final_value') if key in metrics},
            **{key:metrics.get(key) for key in ("coverage_ok","requested_window_complete","requested_window_days",
                "requested_start_utc","requested_end_utc","requested_end_inclusive","coverage_message",
                "performance_evidence","provenance","replay","backtest_days")},
        },
        "panels": [
            {
                "id": "equity",
                "type": "line",
                "title": f"Modeled value ({metrics['accounting_currency']})" if metrics.get('accounting_currency') else "Equity",
                "series": [{"kind": "line", "name": "modeled_value" if metrics.get('accounting_currency') else "equity", "data": equity}],
            }
        ],
        "summary_cards": [
            {"label": "verdict", "value": metrics.get("verdict"), "tone": "neutral"},
            {"label": "total_return_pct", "value": metrics.get("total_return_pct"), "tone": "neutral"},
            {"label": "max_drawdown_pct", "value": metrics.get("max_drawdown_pct"), "tone": "warning"},
            {"label": "total_trades", "value": metrics.get("total_trades"), "tone": "neutral"},
        ],
        "tables": [_table("trades", trade_rows[:100])],
    }
    # Legacy chart blocks assume dollar-equity accounting. Non-USD curves
    # use the explicit unit-bearing panel above instead.
    block = None if metrics.get('accounting_currency') else _build_equity_chart_block(
        workspace_root=workspace_root,
        out_dir=out_dir,
        strategy_id=strategy_id,
        backtest_ts=backtest_ts,
        equity_rows=equity_rows,
        metrics=metrics,
    )
    if block is not None:
        chart["chart_blocks"] = [block]
    return chart


def _build_equity_chart_block(
    *,
    workspace_root: Path,
    out_dir: Path,
    strategy_id: str,
    backtest_ts: str,
    equity_rows: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        from .....charting import BulkContext, equity_curve_from_rows
        from .....core.paths import WorkspacePaths
        from .....workspace.artifact_store import ArtifactStore

        ctx = None
        path = "inline"
        if (workspace_root / "nerya.yml").exists():
            ctx = BulkContext(artifact_store=ArtifactStore(WorkspacePaths(root=workspace_root)))
            path = "bulk"
        return equity_curve_from_rows(
            equity_rows,
            title=f"{strategy_id} · Freeform equity · {backtest_ts}",
            skill="backtest",
            action="freeform_run",
            path=path,
            ctx=ctx,
            initial_capital=_float_or_none(metrics.get("initial_capital_usd")),
            insights=[
                "kind: freeform_sdk_backtest",
                f"trades: {metrics.get('total_trades')}",
            ],
            as_of=str(out_dir),
        )
    except Exception:
        return None


def _table(table_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    columns = _fieldnames(rows)
    return {
        "id": table_id,
        "columns": columns,
        "rows": [[row.get(column) for column in columns] for row in rows],
    }


__all__ = [
    "NoFreeformBacktestScript",
    "has_freeform_backtest_script",
    "run_freeform_backtest",
]
