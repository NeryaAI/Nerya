"""Read projections over existing owners; this module never executes work."""
from __future__ import annotations

import hashlib
import json
import time
import sqlite3

from ..db.sqlite import connect
from .history_mutations import _session_id, is_session_deleted
from .command_store import CommandError


def record(value):
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def execution_view(commands, turns, source="", approvals=(), interactions=(), agents=()):
    """No inference of success from prose or the existence of an artifact."""
    work = sorted((c for c in commands if c.get("kind") != "guide" and c.get("state") != "removed"),
                  key=lambda c: c.get("created_at", 0))
    latest_executed=next((c for c in reversed(work) if c.get("state")!="queued"),{})
    current = latest_executed if latest_executed.get("state") in ("failed","blocked","awaiting_approval","awaiting_input","interrupted") else work[-1] if work else {}
    active = next((c for c in reversed(work) if c.get("state") in ("running", "stopping", "unconfirmed")), None)
    if active:
        current = active
    state = current.get("state", "idle")
    latest = record(turns[-1]) if turns else {}
    reported = False
    if not work:
        stop = latest.get("stopped_reason") or latest.get("stop_reason")
        if stop in ("end_turn", "completed", "stop"):
            state = "succeeded"
        elif stop and "approval" in stop:
            state = "awaiting_approval"
        elif stop and ("cancel" in stop or "interrupt" in stop):
            state = "interrupted"
        elif stop:
            state = "blocked"
        if source in ("mcp", "tunnel"):
            traces = [record(t.get("external_call")) for t in turns if t.get("external_call")]
            for trace in reversed(traces):
                if trace.get("tool") == "nerya_progress":
                    status = record(trace.get("arguments")).get("status")
                    if status in ("completed", "failed", "blocked", "running"):
                        state = "succeeded" if status == "completed" else status
                        reported = status == "completed"
                        break
            if traces and traces[-1].get("status") == "running":
                state, reported = "running", False
    pending = [i for i in interactions if i.get("state") in ("pending", "deferred", "answered")]
    waiting = "user" if pending else "approval" if approvals or state == "awaiting_approval" else "configuration" if state == "blocked" else None
    if any(a.get("state") in ("running", "queued") for a in agents) and state not in ("stopping", "unconfirmed"):
        state = "running"
    verifier = record(latest.get("verifier_outcome"))
    completion = "external_reported" if reported else "turn_finished" if state == "succeeded" else None
    # hard_status means a validation tool succeeded; it is not proof of every goal.
    validation = verifier.get("hard_status", "unknown")
    return {"execution": state, "waiting_for": waiting, "completion": completion,
            "validation": validation, "turn_id": current.get("turn_id") or latest.get("turn_id"),
            "needs_attention": bool(waiting or state in ("failed", "blocked", "unconfirmed", "interrupted") or any(a.get("state") in ("failed","blocked","interrupted") for a in agents)),
            "external": source in ("mcp", "tunnel")}


def session_view(config, sid, *, summary=False):
    sid = _session_id(sid)
    if not config.paths.db.exists():
        raise CommandError("session_not_found",404)
    # The unified dashboard projection must stay readable while a long command
    # is writing WAL. A raw SQLite ``mode=ro`` URI can intermittently surface
    # ``disk I/O error`` while WAL/SHM files are being maintained. Open through
    # the shared query-only helper instead; it cannot mutate the database and
    # follows the same WAL path as repository readers.
    from ..db.sqlite import connect_readonly
    con = connect_readonly(config.paths.db)
    try:
        con.execute("BEGIN")
        if is_session_deleted(con, sid):
            raise CommandError("session_deleted", 410)
        session = con.execute("SELECT * FROM agent_sessions WHERE session_id=?", (sid,)).fetchone()
        commands = [dict(r) for r in con.execute("SELECT command_id,kind,state,revision,turn_id,created_at,updated_at FROM agent_commands WHERE session_id=? AND (state IN ('running','stopping','queued','unconfirmed') OR command_id IN (SELECT command_id FROM agent_commands WHERE session_id=? ORDER BY created_at DESC LIMIT 30)) ORDER BY created_at", (sid,sid))]
        rows = list(con.execute("SELECT message_id,turn_id,meta_json FROM agent_messages WHERE session_id=? AND role='assistant' AND deleted=0 ORDER BY ts DESC LIMIT ?", (sid,1 if summary else 60)))
        interactions = []
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='agent_interactions'").fetchone():
            interactions = [dict(r) for r in con.execute("SELECT * FROM agent_interactions WHERE session_id=? AND state IN ('pending','deferred','answered') ORDER BY created_at", (sid,))]
        queue = con.execute("SELECT paused,pause_reason,revision,lease_until,worker_owner FROM agent_command_queues WHERE session_id=?", (sid,)).fetchone()
        if queue and queue["worker_owner"] and queue["lease_until"] < time.time():
            for command in commands:
                if command["state"] in ("running", "stopping"):
                    command["state"] = "unconfirmed"
        con.commit()
    finally:
        con.close()
    turns = [record(record(r["meta_json"]).get("turn")) for r in reversed(rows)]
    for item in interactions:
        item["payload"] = record(item.pop("payload_json"))
        for key in ("response_json", "response_id", "actor_id"):
            item.pop(key, None)
    if not session and not commands:
        from .session import SessionStore
        legacy = SessionStore(config.paths.root).load(sid)
        if not legacy:
            raise CommandError("session_not_found", 404)
        session = legacy.asdict()
    session = dict(session or {})
    from ..core import jsonl
    approvals = []
    path = config.paths.approvals_pending
    if path.exists():
        from ..approval_service import ApprovalService
        for item in jsonl.read_all(path):
            payload = record(item.get("payload"))
            if (item.get("requester_session_id") or item.get("session_id") or payload.get("session_id")) == sid and item.get("state", "pending") == "pending" and not ApprovalService.expired(item):
                approvals.append({"id": item.get("approval_id") or item.get("id"), "kind": item.get("kind"), "state": "pending"})
    agents = []
    child_db = config.paths.root / "state" / "agent_threads.sqlite3"
    if child_db.exists():
        child = sqlite3.connect(child_db.as_uri() + "?mode=ro", uri=True)
        try:
            for raw in child.execute("SELECT data FROM child_threads WHERE session_id=?", (sid,)):
                row = record(raw[0])
                agents.append({k: row.get(k) for k in ("id", "name", "state", "title", "attempt", "updated_at")})
        finally:
            child.close()
    status = execution_view(commands, turns, session.get("source", ""), approvals, interactions, agents)
    from ..approval_service import ApprovalService
    approval_resolutions = ApprovalService(config).resolution_states(sid)
    refs = []
    for row, turn in zip(reversed(rows), turns):
        index = record(turn.get("artifact_index"))
        for name in dict.fromkeys(index.get("created", []) + index.get("modified", [])):
            refs.append({"kind": "file", "path": name, "turn_id": row["turn_id"], "message_id": row["message_id"]})
    versions = {"commands": [[c["command_id"], c["revision"]] for c in commands],
                "session_updated_at": session.get("updated_at"), "status":status, "queue_revision":queue["revision"] if queue else 0, "approvals": approvals,
                "interactions": [[i["interaction_id"], i["revision"]] for i in interactions], "agents": agents,
                "approval_resolutions": approval_resolutions}
    revision = hashlib.sha256(json.dumps(versions, sort_keys=True, default=str).encode()).hexdigest()[:20]
    return {"ok": True, "session_id": sid, "title": session.get("title", ""), "source": session.get("source", ""),
            "status": status, "revision": revision, "observed_at": time.time(), "source_revisions": versions,
            "queue": {"count": sum(c["state"] == "queued" and c["kind"] != "guide" for c in commands), "paused": bool(queue and queue["paused"])},
            "pending_interactions": interactions, "approvals": approvals, "approval_resolutions": approval_resolutions, "agents": agents, "result_refs": refs,
            "available_actions": {"send": not status["external"] and not interactions and status["execution"] != "unconfirmed",
                                  "stop": not status["external"] and status["execution"] == "running",
                                  "guide": not status["external"] and status["execution"] == "running"}}


def filter_sessions(config, sessions, query):
    """Compatibility list filtering, with an explicit cap owned by the list route."""
    q = str(query.get("q") or "").strip()[:200]
    matched = {}
    if q:
        con = connect(config.paths.db)
        try:
            for row in con.execute("SELECT session_id,message_id,content FROM agent_messages WHERE deleted=0 AND instr(lower(content), lower(?))>0 ORDER BY ts DESC LIMIT 1000", (q,)):
                matched.setdefault(row["session_id"], {"message_id": row["message_id"], "snippet": row["content"][:240]})
        finally:
            con.close()
    result = []
    for session in sessions:
        sid = session["session_id"]
        if q and q.casefold() not in str(session.get("title", "")).casefold() and sid not in matched:
            continue
        try:
            view = session_view(config, sid, summary=True)
        except CommandError:
            continue
        status = view["status"]
        selected = query.get("state", "all")
        if selected == "active" and status["execution"] not in ("running", "stopping", "queued"):
            continue
        if selected == "attention" and not status["needs_attention"]:
            continue
        if selected == "completed" and status["execution"] != "succeeded":
            continue
        result.append({**session, "workbench_status": status, "match": matched.get(sid)})
        if len(result) >= min(1000, int(query.get("limit") or 50)+int(query.get("offset") or 0)+1):
            break
    return result
