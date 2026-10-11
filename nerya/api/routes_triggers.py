from __future__ import annotations


def routes():
    def emit(client, payload):
        ev = payload.get("event") or payload
        if not ev.get("dry_run"):
            from ..triggers.event import TriggerEvent
            probe=TriggerEvent.new(source=ev.get("source","user_command"),kind=ev["kind"],payload=ev.get("payload") or {},
                                   target=ev.get("target","main"),strategy_id=ev.get("strategy_id"))
            _,target,_=client.triggers_runtime.router._resolve_route(probe)
            scopes=set(getattr(client,"auth_scopes",()))
            if target=="skill:strategy.agent_task" and not scopes & {"api:all","execute:strategy"}:
                return {"ok":False,"error":"task_scope_denied","_status":403}
        return client.triggers.emit(
            source=ev.get("source", "http"),
            kind=ev["kind"],
            payload=ev.get("payload") or {},
            target=ev.get("target", "main"),
            strategy_id=ev.get("strategy_id"),
            idempotency_key=ev.get("idempotency_key"),
            dry_run=bool(ev.get("dry_run", False)),
            actor_id=getattr(client,"auth_actor_id",None),
        )

    def dry_run(client, payload):
        ev = payload.get("event") or payload
        ev["dry_run"] = True
        return emit(client, {"event": ev})

    def list_routes(client, _query):
        return client.triggers.list_routes()

    def get_result(client, query):
        eid = (query or {}).get("event_id")
        if not eid:
            return {"error": "event_id required"}
        return client.triggers.wait_for_result(eid, timeout_s=0.25)

    def list_schedules(client, _query):
        return {"schedules": client.triggers.list_schedules()}

    def preview_schedule(_client, payload):
        # 纯计算接口：不实例化 Agent，不写调度、不触发交易或消息交付。
        from ..triggers.schedule_preview import PreviewError, preview_schedule as preview
        from ..core.errors import TriggerValidationError
        try:
            return preview(payload or {})
        except PreviewError as exc:
            return {"ok": False, "error": exc.code, "choices": exc.choices, "_status": 400}
        except (TriggerValidationError, ValueError, TypeError, OverflowError):
            return {"ok": False, "error": "invalid_schedule", "_status": 400}

    def add_schedule(client, payload):
        return client.triggers.add_schedule(
            id=payload["id"],
            kind=payload["kind"],
            every_seconds=payload.get("every_seconds"),
            cron=payload.get("cron"),
            starts_at=payload.get("starts_at"),
            ends_at=payload.get("ends_at"),
            enabled=payload.get("enabled"),
            target=payload.get("target", "main"),
            strategy_id=payload.get("strategy_id"),
            payload=payload.get("payload") or {},
            timezone=payload.get("timezone"),
            session_kind=payload.get("session_kind"),
            attached_skills=payload.get("attached_skills"),
            delivery_targets=payload.get("delivery_targets"),
            session_ttl_seconds=payload.get("session_ttl_seconds"),
            session_mode=payload.get("session_mode"),
            session_id=payload.get("session_id"),
            session_ids=payload.get("session_ids"),
            overlap_policy=payload.get("overlap_policy","skip"),
            execution=payload.get("execution") or {},
            owner_actor_id=getattr(client,"auth_actor_id",None) or "local:loopback",
            run_at=payload.get("run_at"),
            anchor_at=payload.get("anchor_at"),
            delay_seconds=payload.get('delay_seconds'),
        )

    def add_schedule_from_text(client, payload):
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"error": "text is required"}
        return client.triggers.add_schedule_from_text(
            text=text,
            defaults=payload.get("defaults") or None,
        )

    def remove_schedule(client, payload):
        return client.triggers.remove_schedule(id=payload["id"])

    def enable_schedule(client, payload):
        return client.triggers.enable_schedule(
            id=payload["id"], enabled=bool(payload.get("enabled", True)))

    def pause_schedule(client, payload):
        return client.triggers.pause_schedule(id=payload["id"])

    def resume_schedule(client, payload):
        return client.triggers.resume_schedule(id=payload["id"])

    def update_schedule(client, payload):
        fields = {k: v for k, v in (payload or {}).items() if k != "id"}
        return client.triggers.update_schedule(id=payload["id"], **fields)

    def run_schedule_now(client, payload):
        return client.triggers.run_schedule_now(
            id=payload["id"],
            reason=str(payload.get("reason") or "operator_manual_run"),
            client_request_id=payload.get("client_request_id"),
            actor_id=getattr(client,"auth_actor_id",None),
        )

    def schedule_status(client, query):
        q = dict(query or {})
        sid = q.get("id")
        return client.triggers.schedule_status(id=sid if sid else None)

    def tick_schedules(client, _payload):
        return client.triggers.tick_schedules()

    def add_route(client, payload):
        return client.triggers.add_route(**payload)

    def update_route(client, payload):
        return client.triggers.update_route(**payload)

    def pause_route(client, payload):
        return client.triggers.pause_route(
            id=payload["id"], paused=bool(payload.get("paused", True)))

    def remove_route(client, payload):
        return client.triggers.remove_route(id=payload["id"])

    def apply_routes(client, payload):
        return client.triggers.apply_routes(
            add=list(payload.get("add") or []),
            update=list(payload.get("update") or []),
            remove=list(payload.get("remove") or []),
        )

    def explain(client, payload):
        """operator-facing static explain.

        Given an event shape, return the route the router *would* take
        without firing the event or touching dedupe/cooldown state.
        """
        ev = payload.get("event") or payload
        return client.triggers.explain(
            source=ev.get("source", "http"),
            kind=ev["kind"],
            payload=ev.get("payload") or {},
            target=ev.get("target", "main"),
            strategy_id=ev.get("strategy_id"),
        )

    def replay(client, payload):
        """operator-facing replay of a historical event by id."""
        eid = payload.get("event_id") or (payload.get("event") or {}).get("event_id")
        if not eid:
            return {"error": "event_id required"}
        reason = str(payload.get("reason") or "operator_replay")
        out = client.triggers.replay(event_id=eid, reason=reason)
        if out is None:
            return {"error": "event not found", "event_id": eid}
        return out

    def stats(client, _query):
        """aggregate per-route terminal-status counters."""
        from nerya.triggers import aggregate_from_journal, summary
        journal = client.config.paths.journal("triggers")
        agg = aggregate_from_journal(journal)
        return {
            "summary": summary(journal),
            "by_route": [stats.asdict() for stats in agg.values()],
        }

    return [
        ("POST", "/triggers/emit", emit),
        ("POST", "/triggers/dry_run", dry_run),
        ("GET", "/triggers/routes", list_routes),
        ("GET", "/triggers/result", get_result),
        ("GET", "/triggers/schedules", list_schedules),
        ("POST", "/triggers/schedules/preview", preview_schedule),
        ("POST", "/triggers/schedules/add", add_schedule),
        ("POST", "/triggers/schedules/add_from_text", add_schedule_from_text),
        ("POST", "/triggers/schedules/remove", remove_schedule),
        ("POST", "/triggers/schedules/enable", enable_schedule),
        ("POST", "/triggers/schedules/pause", pause_schedule),
        ("POST", "/triggers/schedules/resume", resume_schedule),
        ("POST", "/triggers/schedules/update", update_schedule),
        ("POST", "/triggers/schedules/run_now", run_schedule_now),
        ("GET", "/triggers/schedules/status", schedule_status),
        ("POST", "/triggers/schedules/tick", tick_schedules),
        ("POST", "/triggers/routes/add", add_route),
        ("POST", "/triggers/routes/update", update_route),
        ("POST", "/triggers/routes/pause", pause_route),
        ("POST", "/triggers/routes/remove", remove_route),
        ("POST", "/triggers/routes/apply", apply_routes),
        ("POST", "/triggers/explain", explain),
        ("POST", "/triggers/replay", replay),
        ("GET", "/triggers/stats", stats),
    ]
