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

from typing import Any

from ..core.errors import NeryaError, TradingError
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
    ]


__all__ = ["routes"]
