"""HTTP routes for the strategy runtime control plane.

Returns a list of ``(method, path, handler)`` tuples in the same shape
as the rest of :mod:`nerya.api.routes_*`. Mounted by the runtime
HTTP server alongside ``routes_triggers``, ``routes_strategy_history``,
and the dashboard's existing endpoints.

Routes
------
| Method | Route                                | Purpose                                                 |
|--------|--------------------------------------|---------------------------------------------------------|
| GET    | /strategies/runtime/list             | List promoted strategy packages.                        |
| GET    | /strategies/runtime/get              | Fetch one package's manifest + content hash + files.    |
| POST   | /strategies/runtime/generate         | Generate a strategy package proposal.                   |
| POST   | /strategies/runtime/validate         | Validate a promoted package or in-flight proposal.      |
| POST   | /strategies/runtime/promote          | Approve + apply a strategy package proposal.            |
| POST   | /strategies/runtime/run_tick         | Run one tick.                                           |
| POST   | /strategies/runtime/schedule         | Re-install trading + tuning schedules from manifest.    |
| GET    | /strategies/runtime/schedule_status  | Read trading + tuning schedule rows.                    |
| POST   | /strategies/runtime/pause            | Disable both schedules.                                 |
| POST   | /strategies/runtime/resume           | Enable both schedules.                                  |
| POST   | /strategies/runtime/kill_switch      | Set / clear / inspect the per-strategy kill switch.     |
| GET    | /strategies/runtime/runs             | List recent strategy runs.                              |
| GET    | /strategies/runtime/status           | Aggregate manifest + schedule + kill switch + last run. |
| GET    | /strategies/runtime/workspace        | Aggregate workspace endpoint.   |

Why a "runtime" sub-namespace
-----------------------------
The legacy strategy routes (``/strategies/<id>``) return the older
``trading.strategies.Strategy`` rows. We don't want to break the
operator's existing dashboard while we migrate to the new
StrategyPackage model — namespacing the new endpoints under
``/strategies/runtime/`` lets both surfaces coexist until finishes the dashboard refactor.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import NeryaError, TradingError
from ..evolution.strategy_code_generator import StrategyGenerationRequest


def _request_from_payload(payload: dict[str, Any]) -> StrategyGenerationRequest:
    return StrategyGenerationRequest(
        strategy_id=str(payload.get("strategy_id") or "").strip(),
        title=str(payload.get("title") or ""),
        description=str(payload.get("description") or ""),
        prompt=str(payload.get("prompt") or ""),
        strategy_class=str(payload.get("strategy_class") or "scalping").strip().lower(),
        execution_mode=str(payload.get("execution_mode") or "").strip().lower(),
        mode=str(payload.get("mode") or "paper").strip().lower(),
        markets=tuple(str(m) for m in (payload.get("markets") or ())),
        accounts=tuple(str(a) for a in (payload.get("accounts") or ())),
        schedule_cron=str(payload.get("schedule_cron") or "").strip(),
        schedule_every_seconds=(
            int(payload.get("schedule_every_seconds"))
            if payload.get("schedule_every_seconds") is not None
            else None
        ),
        news_sources=tuple(str(s) for s in (payload.get("news_sources") or ())),
        subagents=tuple(str(s) for s in (payload.get("subagents") or ())),
        policy_overrides=dict(payload.get("policy_overrides") or {}),
        llm_policy_overrides=dict(payload.get("llm_policy_overrides") or {}),
        create_tuning=bool(payload.get("create_tuning", True)),
        tuning_prompt=str(payload.get("tuning_prompt") or ""),
        tuning_cron=str(payload.get("tuning_cron") or "0 */6 * * *"),
        tuning_objectives=tuple(
            str(o) for o in (payload.get("tuning_objectives") or ())
        ),
        extra_subagent_prompts=dict(payload.get("extra_subagent_prompts") or {}),
        files=dict(payload.get("files") or {}),
    )


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a successful response with ``ok=True`` if the SDK didn't already."""

    if "ok" not in payload:
        payload = {"ok": True, **payload}
    return payload


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


def _id_tuple(value: Any) -> tuple[str, ...]:
    values = (value,) if isinstance(value, str) else (value or ())
    if not isinstance(values, (list, tuple, set)):
        return ()
    return tuple(
        item
        for item in (str(raw).strip() for raw in values)
        if item
    )


def routes():
    def list_packages(client, _query):
        return _ok({"strategies": client.strategy.list_packages()})

    def get_package(client, query):
        sid = (query or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        try:
            return _ok(client.strategy.get_package(sid))
        except TradingError as exc:
            return _error(str(exc))

    def generate(client, payload):
        try:
            request = _request_from_payload(payload or {})
            validate = bool((payload or {}).get("validate", True))
            result = client.strategy.generate_proposal(
                request, validate=validate
            )
        except NeryaError as exc:
            return _error(str(exc))
        return _ok(result)

    def validate(client, payload):
        sid = (payload or {}).get("strategy_id")
        pid = (payload or {}).get("proposal_id")
        if not sid and not pid:
            return _error("strategy_id or proposal_id required")
        try:
            return _ok(client.strategy.validate(sid, proposal_id=pid))
        except NeryaError as exc:
            return _error(str(exc))

    def promote(client, payload):
        pid = (payload or {}).get("proposal_id") or ""
        if not pid:
            return _error("proposal_id required")
        note = str((payload or {}).get("note") or "")
        try:
            return _ok(client.strategy.promote(pid, note=note))
        except NeryaError as exc:
            return _error(str(exc))

    def run_tick(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        try:
            record = client.strategy.run_tick(
                sid,
                trigger_payload=dict((payload or {}).get("trigger_payload") or {}),
                trigger_event_id=(payload or {}).get("trigger_event_id"),
                operator=(payload or {}).get("operator"),
                note=str((payload or {}).get("note") or ""),
                mode_override=(
                    str((payload or {})["mode_override"]).strip().lower()
                    if (payload or {}).get("mode_override")
                    else None
                ),
            )
        except NeryaError as exc:
            return _error(str(exc))
        return _ok(record)

    def schedule(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        try:
            return _ok(client.strategy.schedule(sid))
        except (NeryaError, TradingError) as exc:
            return _error(str(exc))

    def schedule_status(client, query):
        sid = (query or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        return _ok(client.strategy.schedule_status(sid))

    def pause(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        return _ok(client.strategy.pause(sid))

    def resume(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        return _ok(client.strategy.resume(sid))

    def kill_switch(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        action = str((payload or {}).get("action") or "get").strip().lower()
        try:
            return _ok(
                client.strategy.kill_switch(
                    sid,
                    action=action,
                    reason=str((payload or {}).get("reason") or ""),
                    by=str((payload or {}).get("by") or "operator"),
                )
            )
        except NeryaError as exc:
            return _error(str(exc))

    def runs(client, query):
        sid = (query or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        limit = max(1, int((query or {}).get("limit") or 50))
        return _ok(client.strategy.runs(sid, limit=limit))

    def agent_tasks(client, query):
        sid = (query or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        limit = max(1, int((query or {}).get("limit") or 50))
        return _ok(client.strategy.agent_tasks(sid, limit=limit))

    def agent_task(client, query):
        sid = (query or {}).get("strategy_id") or ""
        task_id = (query or {}).get("task_id") or ""
        if not sid:
            return _error("strategy_id required")
        if not task_id:
            return _error("task_id required")
        include_prompt = str((query or {}).get("include_prompt", "true")).lower()
        return _ok(
            client.strategy.agent_task(
                sid,
                str(task_id),
                include_prompt=include_prompt not in {"0", "false", "no"},
            )
        )

    def status(client, query):
        sid = (query or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        return _ok(client.strategy.status(sid))

    def workspace(client, query):
        """Aggregate everything the dashboard's StrategyWorkspace needs.

        instead of the frontend making 6 separate calls to
        rebuild a strategy view, we ship one endpoint that joins the
        manifest, schedules, kill switch, last 50 runs, and the
        legacy strategy-history ledgers. of the dashboard
        refactor consumes this verbatim.
        """

        sid = (query or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        runs_limit = max(1, int((query or {}).get("runs_limit") or 50))
        try:
            base = client.strategy.status(sid)
        except Exception as exc:
            return _error(str(exc))
        if not base.get("ok"):
            return base
        base.update(
            {
                "runs": client.strategy.runs(sid, limit=runs_limit),
                "history": client.strategy.history(sid, limit=runs_limit),
            }
        )
        return _ok(base)

    def tuning_generate(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        try:
            return _ok(
                client.strategy.tuning.generate(
                    sid,
                    prompt=str((payload or {}).get("prompt") or ""),
                    cron=str((payload or {}).get("cron") or "0 */6 * * *"),
                    every_seconds=(
                        int((payload or {}).get("every_seconds"))
                        if (payload or {}).get("every_seconds") is not None
                        else None
                    ),
                    objectives=list((payload or {}).get("objectives") or []),
                    require_backtest=bool(
                        (payload or {}).get("require_backtest", True)
                    ),
                    require_shadow_run=bool(
                        (payload or {}).get("require_shadow_run", False)
                    ),
                )
            )
        except NeryaError as exc:
            return _error(str(exc))

    def tuning_schedule(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        try:
            return _ok(client.strategy.tuning.schedule(sid))
        except (NeryaError, TradingError) as exc:
            return _error(str(exc))

    def tuning_pause(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        return _ok(client.strategy.tuning.pause(sid))

    def tuning_resume(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        return _ok(client.strategy.tuning.resume(sid))

    def tuning_run(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        try:
            return _ok(
                client.strategy.tuning.run(
                    sid,
                    dry_run=bool((payload or {}).get("dry_run", False)),
                    operator=(payload or {}).get("operator"),
                    note=str((payload or {}).get("note") or ""),
                    trigger_event_id=(payload or {}).get("trigger_event_id"),
                    evidence_run_ids=_id_tuple(
                        (payload or {}).get("evidence_run_ids")
                    ),
                    evidence_session_ids=_id_tuple(
                        (payload or {}).get("evidence_session_ids")
                    ),
                )
            )
        except NeryaError as exc:
            return _error(str(exc))

    def tuning_status(client, query):
        sid = (query or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        lookback = max(1, int((query or {}).get("lookback_runs") or 200))
        try:
            return _ok(
                client.strategy.tuning.status(sid, lookback_runs=lookback)
            )
        except NeryaError as exc:
            return _error(str(exc))

    def tuning_snapshot(client, query):
        sid = (query or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        lookback = max(1, int((query or {}).get("lookback_runs") or 200))
        return _ok(
            {
                "strategy_id": sid,
                "snapshot": client.strategy.tuning.snapshot(
                    sid, lookback_runs=lookback
                ),
            }
        )

    return [
        ("GET", "/strategies/runtime/list", list_packages),
        ("GET", "/strategies/runtime/get", get_package),
        ("POST", "/strategies/runtime/generate", generate),
        ("POST", "/strategies/runtime/validate", validate),
        ("POST", "/strategies/runtime/promote", promote),
        ("POST", "/strategies/runtime/run_tick", run_tick),
        ("POST", "/strategies/runtime/schedule", schedule),
        ("GET", "/strategies/runtime/schedule_status", schedule_status),
        ("POST", "/strategies/runtime/pause", pause),
        ("POST", "/strategies/runtime/resume", resume),
        ("POST", "/strategies/runtime/kill_switch", kill_switch),
        ("GET", "/strategies/runtime/runs", runs),
        ("GET", "/strategies/runtime/agent_tasks", agent_tasks),
        ("GET", "/strategies/runtime/agent_task", agent_task),
        ("GET", "/strategies/runtime/status", status),
        ("GET", "/strategies/runtime/workspace", workspace),
        ("POST", "/strategies/runtime/tuning/generate", tuning_generate),
        ("POST", "/strategies/runtime/tuning/schedule", tuning_schedule),
        ("POST", "/strategies/runtime/tuning/pause", tuning_pause),
        ("POST", "/strategies/runtime/tuning/resume", tuning_resume),
        ("POST", "/strategies/runtime/tuning/run", tuning_run),
        ("GET", "/strategies/runtime/tuning/status", tuning_status),
        ("GET", "/strategies/runtime/tuning/snapshot", tuning_snapshot),
    ]


__all__ = ["routes"]
