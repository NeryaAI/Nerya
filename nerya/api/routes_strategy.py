"""HTTP routes for legacy strategy CRUD.

Replaces the dashboard's old ``/skills/call`` ``skill_id="strategy"``
calls. The legacy ``strategy`` skill was archived during the workspace-
native rewrite; the dashboard's surface is now a small REST set sitting
directly on top of :mod:`nerya.trading.strategy_crud`.

Why a dedicated route file instead of routing ``/skills/call`` to
``strategy_crud``: skill calls go through the permission engine, the
journal, and a permissive pipeline tuned for *agent* invocations. Plain
operator CRUD is cleaner as plain REST — same shape as
``/strategies/runtime/*`` and ``/portfolio/*``, no permission-pending
queue, no caller-spoofing surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.errors import NeryaError, TradingError
from ..skills.builtin.backtest.scripts.render_chart import render_chart
from ..trading import strategy_crud


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    if "ok" not in payload:
        payload = {"ok": True, **payload}
    return payload


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


def routes():
    def list_strategies(client, payload):
        include_archived = bool((payload or {}).get("include_archived", False))
        try:
            rows = strategy_crud.list_records(
                client.config.paths,
                include_archived=include_archived,
            )
        except Exception as exc:
            return _error(str(exc))
        return {"strategies": rows}

    def get_strategy(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        try:
            return strategy_crud.get_detail(client.config.paths, sid)
        except TradingError as exc:
            return _error(str(exc))
        except Exception as exc:
            return _error(f"{type(exc).__name__}: {exc}")

    def create_strategy(client, payload):
        body = payload or {}
        try:
            req = strategy_crud.CreateRequest(
                strategy_id=str(body.get("strategy_id") or "").strip(),
                title=str(body.get("title") or ""),
                description=str(body.get("description") or ""),
                account_id=str(body.get("account_id") or "paper_main"),
                markets=tuple(str(m) for m in (body.get("markets") or ())),
                trigger_kinds=tuple(
                    str(t) for t in (body.get("trigger_kinds") or ())
                ),
                subagents=tuple(str(s) for s in (body.get("subagents") or ())),
                driver=str(body.get("driver") or "prompt"),
                status=str(body.get("status") or "draft"),
                wallet_id=(
                    str(body.get("wallet_id"))
                    if body.get("wallet_id")
                    else None
                ),
                main_prompt=str(body.get("main_prompt") or ""),
            )
            return strategy_crud.create(client.config.paths, req)
        except TradingError as exc:
            return _error(str(exc))

    def update_strategy(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        patch = {k: v for k, v in body.items() if k != "strategy_id"}
        reason = str(patch.pop("reason", "") or "dashboard_update")
        try:
            return strategy_crud.update(
                client.config.paths, sid, patch=patch, reason=reason,
            )
        except (TradingError, NeryaError) as exc:
            return _error(str(exc))

    def set_status(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        status = body.get("status") or ""
        if not sid or not status:
            return _error("strategy_id and status are required")
        reason = str(body.get("reason") or "dashboard_update")
        try:
            return strategy_crud.set_status(
                client.config.paths, sid, str(status), reason=reason,
            )
        except (TradingError, NeryaError) as exc:
            return _error(str(exc))

    def bind_wallet(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        wallet_id = body.get("wallet_id")
        try:
            return strategy_crud.bind_wallet(
                client.config.paths, sid,
                str(wallet_id) if wallet_id else None,
            )
        except TradingError as exc:
            return _error(str(exc))

    def bind_account(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        aid = body.get("account_id") or ""
        if not sid or not aid:
            return _error("strategy_id and account_id are required")
        try:
            return strategy_crud.bind_account(
                client.config.paths, sid, str(aid),
            )
        except TradingError as exc:
            return _error(str(exc))

    def resolve_runtime(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        try:
            return strategy_crud.resolve_runtime(client.config.paths, sid)
        except TradingError as exc:
            return _error(str(exc))

    def versions(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        try:
            return strategy_crud.versions(client.config.paths, sid)
        except TradingError as exc:
            return _error(str(exc))

    def list_files(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        try:
            return strategy_crud.list_files(client.config.paths, sid)
        except (TradingError, NeryaError) as exc:
            return _error(str(exc))
        except Exception as exc:
            return _error(f"{type(exc).__name__}: {exc}")

    def write_file(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        rel = body.get("rel_path") or ""
        content = body.get("content")
        if not sid or not rel:
            return _error("strategy_id and rel_path are required")
        if not isinstance(content, str):
            return _error("content must be a string")
        reason = str(body.get("reason") or "dashboard_write_file")
        try:
            return strategy_crud.write_file(
                client.config.paths,
                sid,
                rel_path=str(rel),
                content=content,
                reason=reason,
            )
        except (TradingError, NeryaError) as exc:
            return _error(str(exc))
        except Exception as exc:
            return _error(f"{type(exc).__name__}: {exc}")

    def list_backtests(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        root = _backtests_root(client.config.paths.strategy(str(sid)))
        runs = []
        for d in sorted((p for p in root.glob("*") if p.is_dir()), reverse=True):
            metrics = _load_json(d / "metrics.json")
            runs.append({
                "ts": d.name,
                "days": metrics.get("backtest_days"),
                "total_return_pct": metrics.get("total_return_pct"),
                "max_dd_pct": metrics.get("max_drawdown_pct"),
                "sharpe_ratio": metrics.get("sharpe_ratio"),
                "verdict": metrics.get("verdict"),
                "start_utc": metrics.get("start_utc"),
                "end_utc": metrics.get("end_utc"),
            })
        return {"ok": True, "strategy_id": sid, "backtests": runs}

    def backtest_chart(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        ts = (payload or {}).get("ts") or ""
        if not sid or not ts:
            return _error("strategy_id and ts are required")
        try:
            run_dir = _safe_backtest_dir(client.config.paths.strategy(str(sid)), str(ts))
            chart_path = run_dir / "chart.json"
            if not chart_path.exists():
                chart = render_chart(run_dir)
            else:
                chart = _load_json(chart_path)
            return {"ok": True, "strategy_id": sid, "ts": ts, "chart": chart}
        except Exception as exc:
            return _error(f"{type(exc).__name__}: {exc}")

    def backtest_file(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        ts = (payload or {}).get("ts") or ""
        name = (payload or {}).get("name") or ""
        if not sid or not ts or not name:
            return _error("strategy_id, ts and name are required")
        allowed = {
            "config.yml",
            "ohlcv_indicators_portfolio.csv",
            "trades.csv",
            "analysis_by_reason.csv",
            "rejected_signals.csv",
            "metrics.json",
            "report.md",
            "chart.json",
        }
        if name not in allowed:
            return _error("unsupported backtest file")
        try:
            run_dir = _safe_backtest_dir(client.config.paths.strategy(str(sid)), str(ts))
            path = (run_dir / str(name)).resolve()
            if run_dir.resolve() not in path.parents and path != run_dir.resolve():
                return _error("invalid path")
            if not path.exists():
                return _error("file not found")
            return {
                "ok": True,
                "strategy_id": sid,
                "ts": ts,
                "name": name,
                "content": path.read_text(encoding="utf-8"),
            }
        except Exception as exc:
            return _error(f"{type(exc).__name__}: {exc}")

    return [
        ("POST", "/strategy/list_all", list_strategies),
        ("POST", "/strategy/get", get_strategy),
        ("POST", "/strategy/create", create_strategy),
        ("POST", "/strategy/update", update_strategy),
        ("POST", "/strategy/set_status", set_status),
        ("POST", "/strategy/bind_wallet", bind_wallet),
        ("POST", "/strategy/bind_account", bind_account),
        ("POST", "/strategy/resolve_runtime", resolve_runtime),
        ("POST", "/strategy/versions", versions),
        ("POST", "/strategy/files_list", list_files),
        ("POST", "/strategy/files_write", write_file),
        ("POST", "/strategy/backtests", list_backtests),
        ("POST", "/strategy/backtests/chart", backtest_chart),
        ("POST", "/strategy/backtests/file", backtest_file),
    ]


def _backtests_root(strategy_root: Path) -> Path:
    return strategy_root / "backtests"


def _safe_backtest_dir(strategy_root: Path, ts: str) -> Path:
    root = _backtests_root(strategy_root).resolve()
    run_dir = (root / ts).resolve()
    if root not in run_dir.parents and run_dir != root:
        raise TradingError("invalid backtest timestamp")
    if not run_dir.exists() or not run_dir.is_dir():
        raise TradingError("unknown backtest run")
    return run_dir


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = ["routes"]
