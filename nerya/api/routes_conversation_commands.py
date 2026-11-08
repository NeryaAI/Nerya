"""Dashboard conversation ingress; guarded by the same internal assertion as run_turn."""
from __future__ import annotations

import logging
from functools import wraps

from ..agent.command_runtime import runtime
from ..agent.command_store import CommandError
from ..agent.loop_state import TurnCheckpointResumeError
from ..sdk.internal_client import InternalClient

_LOG = logging.getLogger(__name__)


def routes():
    """Discovery hook: routes_agent owns registration with its canonical runner."""
    return []


def command_routes(run_turn):
    def execute(config, request):
        # Request-local clients never leak a sqlite connection or actor across threads.
        return run_turn(InternalClient.from_config(config), request, enforce_public_gate=False)

    def safe(handler):
        @wraps(handler)
        def wrapped(client, payload):
            try:
                if not isinstance(payload, dict):
                    raise CommandError("invalid_command_request", 400)
                return handler(runtime(client.config, execute), payload)
            except CommandError as exc:
                return {"ok": False, "_status": exc.status, "error": exc.code, "retrying": False}
            except TurnCheckpointResumeError as exc:
                result = exc.asdict()
                result["_status"] = result.pop("status", 409)
                return result
            except (ValueError, TypeError):
                return {"ok": False, "_status": 400, "error": "invalid_command_request"}
            except Exception:
                _LOG.exception("conversation command request failed")
                return {"ok": False, "_status": 500, "error": "command_storage_unavailable"}
        return wrapped

    @safe
    def submit(manager, payload):
        return manager.submit(payload)

    @safe
    def snapshot(manager, query):
        return manager.store.snapshot(query.get("session_id"), query.get("command_id"))

    @safe
    def events(manager, query):
        return manager.store.events(query.get("session_id"), query.get("command_id"),
                                    int(query.get("after_seq") or 0), int(query.get("limit") or 500))

    @safe
    def control(manager, payload):
        revision = payload.get("expected_revision")
        if payload.get("action") != "reconcile" and (isinstance(revision, bool) or not isinstance(revision, int)):
            raise CommandError("expected_revision_required", 400)
        return manager.control(payload)

    @safe
    def reference(manager, query):
        from ..agent.reference_preview import reference_preview
        return reference_preview(manager.config.paths, query.get("uri"))

    @safe
    def goal_control(manager, payload):
        from ..agent.goals import control as change_goal, enqueue_if_idle
        sid=payload.get("session_id")
        goal=change_goal(manager.store,sid,payload.get("action"),payload.get("expected_revision"),
            actor=str(payload.get("_auth_actor_id") or "local"))
        if payload.get("action")=="resume":
            enqueue_if_idle(manager.store,sid)
            manager.kick(sid)
        return {"ok":True,"goal":goal}

    @safe
    def respond(manager, payload):
        from ..agent.interactions import respond as apply_response
        return apply_response(manager, payload)

    @safe
    def fork(manager, payload):
        from ..agent.history_branch import fork_session
        from ..agent.history_mutations import history_response
        return history_response(fork_session, manager.config.paths, payload)

    return [("POST", "/agent/commands", submit), ("GET", "/agent/commands", snapshot),
            ("GET", "/agent/commands/events", events), ("POST", "/agent/commands/control", control),
            ("GET", "/agent/commands/reference", reference), ("POST", "/agent/commands/fork", fork), ("POST", "/agent/interactions/respond", respond),
            ("POST", "/agent/goals/control", goal_control)]
