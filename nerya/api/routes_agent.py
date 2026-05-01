from __future__ import annotations

import json
import re
import traceback
from datetime import datetime, timezone
from typing import Any

from ..agent.kernel import AgentKernel
from ..agent.recovery import list_open_turns, load_turn_state
from ..agent.session import SessionStore
from ..core import jsonl
from ..observability.trace import build_trace, explain_trace
from .gateway_events import turn_events


def _iso_from_db_ts(value: Any) -> str:
    try:
        ts = float(value)
    except Exception:
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_meta_json(raw: Any) -> dict[str, Any]:
    try:
        meta = json.loads(str(raw or "{}"))
    except Exception:
        meta = {}
    return meta if isinstance(meta, dict) else {}


def _db_session_asdict(row: dict[str, Any]) -> dict[str, Any]:
    title = str(row.get("title") or "").strip()
    meta = _parse_meta_json(row.get("meta_json"))
    if title and not meta.get("title"):
        meta["title"] = title
        meta.setdefault("title_source", "db")
    return {
        "session_id": str(row.get("session_id") or ""),
        "strategy_id": row.get("strategy_id"),
        "created_at": _iso_from_db_ts(row.get("created_at")),
        "updated_at": _iso_from_db_ts(row.get("updated_at")),
        "turn_ids": [],
        "invoked_skills": [],
        "skill_state": {},
        "last_action": None,
        "meta": meta,
        "source": row.get("source") or "",
    }


def _merge_session_dict(file_state: dict[str, Any], db_row: dict[str, Any] | None) -> dict[str, Any]:
    if not db_row:
        return file_state
    db_state = _db_session_asdict(db_row)
    merged = dict(db_state)
    merged.update(file_state)
    file_meta = file_state.get("meta") if isinstance(file_state.get("meta"), dict) else {}
    db_meta = db_state.get("meta") if isinstance(db_state.get("meta"), dict) else {}
    meta = {**db_meta, **file_meta}
    if not meta.get("title") and db_meta.get("title"):
        meta["title"] = db_meta["title"]
    merged["meta"] = meta
    if not merged.get("created_at"):
        merged["created_at"] = db_state.get("created_at") or ""
    if _session_updated_ts(db_state) > _session_updated_ts(file_state):
        merged["updated_at"] = db_state.get("updated_at") or merged.get("updated_at")
    if not merged.get("source"):
        merged["source"] = db_state.get("source") or ""
    return merged


def _session_updated_ts(session: dict[str, Any]) -> float:
    raw = session.get("updated_at")
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def normalise_trigger_payload(payload):
    """Accept both {trigger: {...}} and a bare trigger object.

    The dashboard posts the bare trigger shape. Older callers use the
    wrapper shape. Treat both as the same API contract.
    """
    if isinstance(payload.get("trigger"), dict):
        return payload["trigger"]
    keys = {"source", "kind", "target", "payload", "id", "event_id"}
    if any(k in payload for k in keys):
        return payload
    return {}


def _payload_text(payload) -> str:
    """Pull human-visible text out of a ``send_message`` payload.

    We accept three nesting shapes here because the LLM sometimes wraps
    the real payload in another ``payload`` envelope (mirroring the
    ``payload=<shape>`` style we render in the action catalog). Without
    this, a perfectly good Chinese/English reply at
    ``payload.payload.text`` would be discarded and the kernel would
    fall back to "I could not produce a reply for that turn.".
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("text", "message", "reply", "body", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    inner = payload.get("payload")
    if isinstance(inner, dict):
        for key in ("text", "message", "reply", "body", "title"):
            value = inner.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _summarise_action_result(action) -> str:
    if not isinstance(action, dict):
        return ""
    name = action.get("action")
    result = action.get("result") if isinstance(action.get("result"), dict) else {}
    if name == "create_strategy" and result:
        return (
            f"Created strategy `{result.get('strategy_id')}` "
            f"with status `{result.get('status')}`."
        )
    if name == "set_strategy_status" and result:
        return f"Set strategy `{result.get('strategy_id')}` to `{result.get('status')}`."
    if name == "propose_script" and result:
        return (
            f"Proposed script `{result.get('script_id')}` "
            f"as `{result.get('state')}`."
        )
    if name == "create_subagent" and result:
        return f"Created subagent `{result.get('name')}` ({result.get('state')})."
    if name == "add_schedule" and result:
        entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
        return (
            f"Added schedule `{result.get('id')}` for `{entry.get('kind')}` "
            f"({result.get('state')})."
        )
    if name == "explain_turn" and result:
        stages = result.get("stages") if isinstance(result.get("stages"), dict) else {}
        return (
            f"Loaded trace for turn `{result.get('turn_id')}`: "
            f"{len(stages)} stage(s), {len(result.get('degradations') or [])} degradation(s)."
        )
    return ""


def _summarise_actions(actions) -> str:
    lines = [_summarise_action_result(a) for a in (actions or [])]
    lines = [line for line in lines if line]
    return "\n".join(f"- {line}" for line in lines)


def _decision_payload_text(decision) -> str:
    """Pull human text from a decision dict when no tool actually fired.

    Some LLM responses (notably structured-output adapters that wrap
    the model's JSON in an envelope) arrive as
    ``{"raw": "<json string>"}``. The parsed JSON inside still has a
    perfectly valid ``payload.text`` we should surface — otherwise the
    operator sees the canned "I could not produce a reply" fallback
    even though the model gave a complete answer.

    This helper tries, in order:

    1. ``decision["payload"]["text"]`` (and aliases).
    2. ``json.loads(decision["raw"])`` and the same payload mining.
    3. Anywhere ``payload.payload.text`` is nested inside the payload.
    """

    if not isinstance(decision, dict):
        return ""
    payload = decision.get("payload")
    text = _payload_text(payload)
    if text:
        return text
    raw = decision.get("raw")
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            import json as _json
            parsed = _json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            text = _payload_text(parsed.get("payload"))
            if text:
                return text
            for key in ("text", "message", "reply"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def agent_reply_text(result) -> str:
    """Best-effort human text extraction from an agent turn result.

    Order of preference:

    1. ``result.final_text`` — the workspace-native loop's terminal
       assistant text (the model's answer at ``stop_reason == end_turn``).
    2. The last ``send_message`` payload in ``result.tool_trace`` — for
       skill bridges that route the answer through the legacy
       ``message`` skill.
    3. Decision-level fallbacks (legacy harness shapes).

    ``reasoning`` explains why the model chose tools; we never surface
    it as the chat reply when tools actually ran, to avoid leaking
    chain-of-thought into operator-facing surfaces.
    """

    final_text = getattr(result, "final_text", "")
    if isinstance(final_text, str) and final_text.strip():
        return final_text.strip()
    decision = result.decision or {}
    last_send_text = ""
    for rec in result.tool_trace or []:
        if (
            isinstance(rec, dict)
            and (rec.get("skill_id") or rec.get("skill")) == "message"
            and rec.get("action") == "send_message"
        ):
            text = _payload_text(rec.get("payload"))
            if text:
                last_send_text = text
    if last_send_text:
        return last_send_text
    for key in ("text", "message", "reply"):
        value = decision.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # Decision-level send_message: the model emitted a fully-formed
    # ``{"action":"send_message", "payload":{"text":...}}`` but no
    # tool actually ran (some structured-output adapters route the
    # whole envelope into ``decision.raw`` and leave the action lane
    # empty). The user's reply text still lives there — surface it.
    decision_text = _decision_payload_text(decision)
    if decision_text:
        return decision_text
    # Only mine ``send_message`` action records for reply text. Reading
    # ``result.text`` from arbitrary read-only actions (``recall``,
    # ``list_messages``, ``get_social_signals``, ...) used to leak raw
    # memory snapshots / past trace dumps into the operator-visible reply
    # whenever the LLM forgot to wrap its answer in ``send_message``.
    for action in result.actions or []:
        if not isinstance(action, dict):
            continue
        if action.get("action") != "send_message":
            continue
        for key in ("text", "message", "reply"):
            value = action.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        result_obj = action.get("result")
        if isinstance(result_obj, dict):
            for key in ("text", "message", "reply"):
                value = result_obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    action_summary = _summarise_actions(result.actions)
    if action_summary:
        return action_summary
    # Special case: an explicit ``noop`` decision with no executed actions
    # is the LLM saying "nothing to do" — its ``reasoning`` is the only
    # operator-facing payload it produced and is safe to surface (the
    # rule below about not leaking chain-of-thought applies to *tool*
    # turns, where reasoning is internal commentary). Without this
    # fallback, ``/agent/run_turn`` returns "I could not produce a reply"
    # whenever an LLM explicitly says noop, which is wrong.
    if (
        not result.actions
        and not result.tool_trace
        and isinstance(decision, dict)
        and str(decision.get("action") or "").strip() == "noop"
    ):
        reasoning = decision.get("reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning.strip()
    # Intentionally do NOT fall back to ``decision.get("reasoning")`` for
    # tool-running turns. ``reasoning`` is the model's internal
    # chain-of-thought (e.g. "the user asks for X, we should answer
    # concisely ...") — surfacing it as the chat reply leaks
    # meta-commentary to operators. When the turn ends without a real
    # ``send_message`` payload, the kernel sets ``stopped_reason``
    # (typically ``needs_summarisation`` or ``max_iterations``) so
    # callers can show a "no reply yet" state and decide whether to
    # nudge the agent for a follow-up.
    return "I could not produce a reply for that turn."


def routes():
    def _normalise_reasoning_effort(value: object) -> str | None:
        if value is None:
            return None
        raw = str(value).strip().lower()
        if raw in {"", "default", "none", "off", "disabled"}:
            return None
        aliases = {
            "min": "minimal",
            "normal": "medium",
            "x-high": "xhigh",
            "extra_high": "xhigh",
            "extra-high": "xhigh",
        }
        raw = aliases.get(raw, raw)
        if raw in {"minimal", "low", "medium", "high", "xhigh", "max"}:
            return raw
        return None

    def _normalise_llm_tier(value: object, client) -> str | None:
        if value is None:
            return None
        raw = str(value).strip()
        if not raw or raw.lower() in {"default", "auto"}:
            return None
        tiers = client.config.get("llm.tiers") or {}
        return raw if raw in tiers else None

    def _normalise_model_provider(value: object) -> str | None:
        if value is None:
            return None
        raw = str(value).strip().lower()
        if not raw or raw in {"default", "auto"}:
            return None
        if not re.match(r"^[a-z0-9_.-]{1,64}$", raw):
            return None
        return raw

    def _normalise_model_id(value: object) -> str | None:
        if value is None:
            return None
        raw = str(value).strip()
        if not raw or raw.lower() in {"default", "auto"}:
            return None
        return raw[:160]

    def run_turn(client, payload):
        # Permission mode resolution order (most → least specific):
        # explicit payload field, env override, default. Env override
        # ``NERYA_PERMISSION_MODE`` exists so headless / unattended runs
        # (eval harness, integration tests, dev sessions) can flip the
        # kernel into ``auto`` / ``yolo`` without round-tripping every
        # tool through the dashboard approval pane.
        import os as _os
        from ..tools.permissions import PermissionMode as _PM
        raw = (
            (payload.get("permission_mode") if isinstance(payload, dict) else None)
            or _os.environ.get("NERYA_PERMISSION_MODE")
            or "default"
        )
        try:
            pmode = _PM(str(raw).strip().lower())
        except Exception:
            pmode = _PM.DEFAULT
        reasoning_effort = _normalise_reasoning_effort(
            payload.get("reasoning_effort")
            if isinstance(payload, dict)
            else None
        )
        reasoning_summary_raw = (
            payload.get("reasoning_summary")
            if isinstance(payload, dict)
            else None
        )
        reasoning_summary = (
            str(reasoning_summary_raw).strip().lower()
            if reasoning_summary_raw is not None
            else None
        )
        if reasoning_summary not in {"auto", "concise", "detailed"}:
            reasoning_summary = None
        llm_tier = _normalise_llm_tier(payload.get("model_tier"), client)
        model_provider = _normalise_model_provider(payload.get("model_provider"))
        model_id = _normalise_model_id(payload.get("model_id") or payload.get("model"))
        kernel = AgentKernel(
            config=client.config,
            skills=client.skills,
            permission_mode=pmode,
            reasoning_effort=reasoning_effort,
            reasoning_summary=reasoning_summary,
            llm_tier=llm_tier,
            model_provider=model_provider,
            model_id=model_id,
        )
        trigger = normalise_trigger_payload(payload)
        try:
            result = kernel.run_turn(
                trigger=trigger,
                strategy_id=payload.get("strategy_id"),
                session_id=payload.get("session_id"),
            )
        except Exception as exc:
            tb = traceback.format_exc()
            jsonl.append(client.config.paths.journal("errors"), {
                "kind": "api.run_turn.error",
                "trigger_event_id": trigger.get("id") or trigger.get("event_id"),
                "error": f"{type(exc).__name__}: {exc}",
                "trace": tb.splitlines()[-20:],
            })
            raise
        # The workspace-native loop emits :class:`BlockEnvelope`
        # transcripts; ``steps`` and ``blocks`` carry the same payload
        # so old clients pointed at ``steps`` keep working while the
        # canonical name is ``blocks``.
        return {
            "trigger_event_id": result.trigger_event_id,
            "decision": result.decision,
            "actions": result.actions,
            "tool_trace": result.tool_trace,
            "budget": result.budget,
            "reply_text": agent_reply_text(result),
            "events": turn_events(result),
            "turn_id": result.turn_id,
            "stopped_reason": result.stopped_reason,
            "final_text": getattr(result, "final_text", ""),
            "iterations": getattr(result, "iterations", 0),
            "steps": list(result.steps or []),
            "blocks": list(result.blocks or []),
            "harness": getattr(result, "harness", "native"),
        }

    def get_trace(client, payload):
        """POST /agent/trace — rebuild the end-to-end trace for a correlator.

        operators can hand in any of ``trigger_id``/``turn_id``/
        ``session_id`` (optionally scoped to ``strategy_id``) and get
        back a time-ordered list of every runtime event that touched
        the request. Zero state: re-built from the journals each time.
        """
        trace = build_trace(
            client.config.paths,
            trigger_id=payload.get("trigger_id"),
            turn_id=payload.get("turn_id"),
            session_id=payload.get("session_id"),
            strategy_id=payload.get("strategy_id"),
        )
        return trace.as_dict()

    def explain(client, payload):
        """POST /agent/explain — operator-oriented explain surface.

        Returns the same trace plus stage counts, detected degradation
        rows attribution, and the active strategy version —
        everything an operator needs to answer "what happened and why".
        """
        return explain_trace(
            client.config.paths,
            trigger_id=payload.get("trigger_id"),
            turn_id=payload.get("turn_id"),
            session_id=payload.get("session_id"),
            strategy_id=payload.get("strategy_id"),
        )

    def open_turns(client, _params):
        """GET /agent/open_turns — resumable / halted turn inventory. operator surface: renders every turn the journal
        knows about that never emitted a ``close`` step, so the on-call
        can decide which ones to resume and which to abandon.
        """
        return {
            "open_turns": [s.asdict() for s in list_open_turns(client.config.paths)],
        }

    def turn_state(client, payload):
        """POST /agent/turn_state — full recovery view for one turn."""
        tid = payload.get("turn_id")
        if not tid:
            return {"error": "turn_id required"}
        return load_turn_state(client.config.paths, tid).asdict()

    def sessions_list(client, query):
        q = dict(query or {})
        store = SessionStore(client.config.paths.root)
        strategy_id = q.get("strategy_id") or None
        limit = int(q.get("limit") or 50)
        states = [s.asdict() for s in store.list(strategy_id=strategy_id, limit=limit)]
        by_id = {str(s.get("session_id") or ""): dict(s) for s in states}
        try:
            from ..db.repositories import AgentSessionRepository
            from ..db.sqlite import connect

            con = connect(client.config.paths.db)
            rows = AgentSessionRepository(con).list_sessions(limit=max(limit * 2, limit))
            con.close()
            for row in rows:
                if strategy_id and row.get("strategy_id") != strategy_id:
                    continue
                sid = str(row.get("session_id") or "")
                if not sid:
                    continue
                if sid in by_id:
                    by_id[sid] = _merge_session_dict(by_id[sid], row)
                else:
                    by_id[sid] = _db_session_asdict(row)
        except Exception:
            pass
        sessions = list(by_id.values())
        sessions.sort(
            key=_session_updated_ts,
            reverse=True,
        )
        return {"sessions": sessions[:limit]}

    def session_get(client, query):
        q = dict(query or {})
        sid = q.get("session_id") or q.get("id")
        if not sid:
            return {"error": "session_id required"}
        store = SessionStore(client.config.paths.root)
        state = store.load(sid)
        try:
            from ..db.repositories import AgentSessionRepository
            from ..db.sqlite import connect

            con = connect(client.config.paths.db)
            db_row = AgentSessionRepository(con).get_session(str(sid))
            con.close()
        except Exception:
            db_row = None
        if state is None:
            if db_row:
                return _db_session_asdict(db_row)
            return {"error": "session not found", "session_id": sid}
        return _merge_session_dict(state.asdict(), db_row)

    def session_delete(client, payload):
        sid = (payload or {}).get("session_id")
        if not sid:
            return {"ok": False, "error": "session_id required"}
        store = SessionStore(client.config.paths.root)
        ok = store.delete(sid)
        db_deleted = False
        try:
            from ..db.sqlite import connect

            con = connect(client.config.paths.db)
            cur = con.execute("DELETE FROM agent_sessions WHERE session_id=?", (sid,))
            db_deleted = db_deleted or bool(cur.rowcount or 0)
            cur = con.execute("DELETE FROM agent_messages WHERE session_id=?", (sid,))
            db_deleted = db_deleted or bool(cur.rowcount or 0)
            cur = con.execute("DELETE FROM agent_tool_events WHERE session_id=?", (sid,))
            db_deleted = db_deleted or bool(cur.rowcount or 0)
            con.close()
        except Exception:
            pass
        return {"ok": bool(ok or db_deleted)}

    def session_rename(client, payload):
        p = payload or {}
        sid = str(p.get("session_id") or "").strip()
        title = str(p.get("title") or "").strip()
        if not sid or not title:
            return {"ok": False, "error": "session_id + title required"}
        title = " ".join(title.split())[:80]
        store = SessionStore(client.config.paths.root)
        state = store.update_meta(
            sid,
            {
                "title": title,
                "title_source": "operator",
            },
        )
        try:
            from ..db.repositories import AgentSessionRepository
            from ..db.sqlite import connect

            con = connect(client.config.paths.db)
            AgentSessionRepository(con).upsert_session(
                session_id=sid,
                strategy_id=state.strategy_id,
                title=title,
                meta=state.meta,
            )
            con.close()
        except Exception:
            pass
        return {"ok": True, "session": state.asdict()}

    def session_message_edit(client, payload):
        p = payload or {}
        sid = str(p.get("session_id") or "").strip()
        message_id = str(p.get("message_id") or "").strip()
        content = str(p.get("content") or "")
        if not sid or not message_id:
            return {"ok": False, "error": "session_id + message_id required"}
        if not content.strip():
            return {"ok": False, "error": "content required"}
        try:
            from ..db.repositories import AgentSessionRepository
            from ..db.sqlite import connect

            con = connect(client.config.paths.db)
            ok = AgentSessionRepository(con).update_message_content(
                session_id=sid,
                message_id=message_id,
                content=content[:16_000],
            )
            con.close()
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not ok:
            return {"ok": False, "error": "message not found"}
        return {"ok": True, "session_id": sid, "message_id": message_id}

    def session_message_delete(client, payload):
        p = payload or {}
        sid = str(p.get("session_id") or "").strip()
        message_id = str(p.get("message_id") or "").strip()
        if not sid or not message_id:
            return {"ok": False, "error": "session_id + message_id required"}
        try:
            from ..db.repositories import AgentSessionRepository
            from ..db.sqlite import connect

            con = connect(client.config.paths.db)
            ok = AgentSessionRepository(con).delete_session_message(
                session_id=sid,
                message_id=message_id,
            )
            con.close()
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not ok:
            return {"ok": False, "error": "message not found"}
        return {"ok": True, "session_id": sid, "message_id": message_id}

    def session_record_skill_state(client, payload):
        p = payload or {}
        sid = p.get("session_id")
        skill_id = p.get("skill_id")
        if not sid or not skill_id:
            return {"ok": False, "error": "session_id + skill_id required"}
        store = SessionStore(client.config.paths.root)
        state = store.record_skill_state(sid, skill_id, p.get("state"))
        return {"ok": True, "session": state.asdict()}

    def session_search(client, payload):
        """POST /agent/session/search — full-journal search.

        Body: ``{query, session_id?, strategy_id?, limit?, journals?}``.
        Returns matching events newest-first with payload + preview.
        """
        from ..agent.session_search import search as _search
        p = payload or {}
        q = str(p.get("query") or "").strip()
        if not q:
            return {"ok": False, "error": "query required"}
        rows = _search(
            client.config.paths,
            q,
            session_id=p.get("session_id"),
            strategy_id=p.get("strategy_id"),
            limit=int(p.get("limit") or 50),
            journals=tuple(p.get("journals") or ("turn_steps", "agent_decisions", "skills", "messages")),
            case_sensitive=bool(p.get("case_sensitive", False)),
        )
        return {"ok": True, "matches": rows, "count": len(rows)}

    def session_recent_events(client, query):
        """GET /agent/session/events — recent journal events for a session."""
        from ..agent.session_search import recent_events as _recent
        q = dict(query or {})
        rows = _recent(
            client.config.paths,
            session_id=q.get("session_id"),
            strategy_id=q.get("strategy_id"),
            limit=int(q.get("limit") or 50),
        )
        return {"events": rows, "count": len(rows)}

    def session_transcript_handler(client, query):
        """GET /agent/session/transcript — chat-shaped transcript.

        Returns user/assistant pairs reconstructed from the agent
        journal so the dashboard chat can fold in conversations that
        were started outside the dashboard (curl, gateway, scripts).
        """
        from ..agent.session_search import session_transcript as _txn
        q = dict(query or {})
        sid = q.get("session_id") or q.get("id")
        if not sid:
            return {"ok": False, "error": "session_id required"}
        messages: list[dict] = []
        try:
            import json as _json

            from ..db.repositories import AgentSessionRepository
            from ..db.sqlite import connect

            con = connect(client.config.paths.db)
            rows = AgentSessionRepository(con).transcript(
                str(sid),
                limit=int(q.get("max_pairs") or 200) * 2,
            )
            con.close()
            for r in rows:
                if r.get("role") not in {"user", "assistant"}:
                    continue
                meta = {}
                try:
                    meta = _json.loads(r.get("meta_json") or "{}")
                except Exception:
                    meta = {}
                messages.append(
                    {
                        "message_id": r.get("message_id"),
                        "role": r.get("role"),
                        "content": r.get("content"),
                        "turn_id": r.get("turn_id"),
                        "ts": r.get("ts"),
                        "meta": meta if isinstance(meta, dict) else {},
                    }
                )
        except Exception:
            messages = []
        if not messages:
            try:
                messages = _txn(
                    client.config.paths,
                    session_id=str(sid),
                    per_msg_cap=int(q.get("per_msg_cap") or 12_000),
                    max_pairs=int(q.get("max_pairs") or 200),
                )
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        store = SessionStore(client.config.paths.root)
        state = store.load(str(sid))
        try:
            from ..db.repositories import AgentSessionRepository
            from ..db.sqlite import connect

            con = connect(client.config.paths.db)
            db_row = AgentSessionRepository(con).get_session(str(sid))
            con.close()
        except Exception:
            db_row = None
        db_state = _db_session_asdict(db_row) if db_row else {}
        db_meta = db_state.get("meta") if isinstance(db_state.get("meta"), dict) else {}
        state_meta = state.meta if state else {}
        return {
            "ok": True,
            "session_id": str(sid),
            "strategy_id": (
                state.strategy_id if state else db_state.get("strategy_id")
            ),
            "title": (state_meta.get("title") or db_meta.get("title") or ""),
            "created_at": (state.created_at if state else db_state.get("created_at", "")),
            "updated_at": (state.updated_at if state else db_state.get("updated_at", "")),
            "messages": messages,
            "count": len(messages),
        }

    def stream_events(client, query):
        """GET /agent/stream/events — streaming bus replay.

        Returns recently-published events from the process-wide
        :class:`StreamingEventBus`. The dashboard polls this endpoint
        (or any other subscriber that cannot hold a long-lived
        socket) to render assistant deltas, tool start/progress, and
        approval cards in real time. Replays the last N events so a
        new client never misses the just-finished turn.

        Query parameters
        ----------------
        ``session_id`` (optional)
            Restrict the response to events whose payload is tagged
            with this session.
        ``after_seq`` (optional)
            Return only events with ``seq > after_seq``. Implements
            the reconnect contract: a client persists the
            largest seq it has rendered and replays from there on
            reconnect, so it never duplicates or drops events.
        ``limit`` (optional)
            Cap the number of events returned (newest-N).

        Response
        --------
        ``{events, count, cursor, latest_seq}``

        ``cursor`` is the largest seq in the returned slice (or the
        bus's ``latest_seq`` if no events match) — clients pass it
        back as ``after_seq`` on the next poll. ``latest_seq`` is the
        bus-wide cursor regardless of filtering, useful for clients
        that filter by ``session_id`` and still want a global pointer.
        """

        from ..agent.streaming import get_default_bus
        bus = get_default_bus()
        q = query or {}
        try:
            after_seq_raw = q.get("after_seq")
            after_seq = int(after_seq_raw) if after_seq_raw not in (None, "") else None
        except (TypeError, ValueError):
            after_seq = None
        events = bus.recent(after_seq=after_seq)
        sid = q.get("session_id")
        if sid:
            events = [e for e in events if e.get("session_id") == sid]
        try:
            raw_limit = q.get("limit")
            limit = int(raw_limit) if raw_limit not in (None, "") else len(events)
        except (TypeError, ValueError):
            limit = len(events)
        if limit > 0 and len(events) > limit:
            events = events[-limit:]
        return {
            "events": events,
            "count": len(events),
            "cursor": bus.cursor_after(events),
            "latest_seq": bus.latest_seq(),
        }

    def interrupt(client, payload):
        """POST /agent/interrupt — stop control.

        Best-effort cooperative cancellation of the current turn(s)
        for ``session_id``. The kernel's :class:`CancelToken` is
        consulted at every step boundary, so signalling here causes
        the in-flight turn to return on the next checkpoint.
        """

        from ..harness.cancellation import signal_cancel
        p = payload or {}
        sid = p.get("session_id") or p.get("turn_id")
        if not sid:
            return {"ok": False, "error": "session_id required"}
        cancelled = signal_cancel(str(sid), reason=str(p.get("reason") or "operator_interrupt"))
        return {"ok": True, "cancelled": cancelled, "session_id": str(sid)}

    def tool_registry(client, _payload):
        """GET /agent/tools — enumerate native tools.

        Builds an ephemeral :class:`ToolRegistry` with the native
        bootstrap so the dashboard can show every tool the workspace-
        native loop is allowed to call, with risk / scope / provenance
        metadata. The registry is rebuilt per-call so it always reflects
        the live config (hot-loaded user skills, MCP-registered tools,
        etc.).
        """

        from pathlib import Path as _Path

        from ..agent.file_state import FileStateCache
        from ..tools import ToolRegistry
        from ..tools.native import build_native_tool_deps, register_native_tools

        registry = ToolRegistry()
        try:
            workspace_root = _Path(client.config.paths.root)
        except Exception:
            workspace_root = _Path.cwd()

        skill_roots: list[_Path] = []
        try:
            for entry in client.skills.registry.list():
                root = getattr(entry, "skill_dir", None) or getattr(entry, "path", None)
                if root:
                    skill_roots.append(_Path(str(root)).parent)
        except Exception:
            pass

        deps = build_native_tool_deps(
            workspace_root=workspace_root,
            skill_roots=skill_roots,
            file_state=FileStateCache(),
            paths=client.config.paths,
            config=client.config,
            skills=client.skills,
        )
        register_native_tools(registry, deps)

        items = []
        for descriptor in registry.list_tools():
            items.append({
                "name": descriptor.name,
                "description": descriptor.description,
                "namespace": descriptor.namespace,
                "risk": getattr(descriptor.risk, "value", str(descriptor.risk)),
                "permission_scope": getattr(
                    descriptor.permission_scope, "value", str(descriptor.permission_scope)
                ),
                "read_only": bool(descriptor.read_only),
                "is_concurrency_safe": bool(descriptor.is_concurrency_safe),
                "requires_fresh_read": bool(descriptor.requires_fresh_read),
                "mutates_paths": bool(descriptor.mutates_paths),
                "result_kind": descriptor.result_kind,
                "auto_approve": bool(descriptor.auto_approve),
                "tags": list(descriptor.tags or []),
                "input_schema": dict(descriptor.input_schema or {}),
            })
        items.sort(key=lambda r: (r["namespace"], r["name"]))
        return {
            "ok": True,
            "count": len(items),
            "tools": items,
            "harness": "native",
        }

    return [
        ("POST", "/agent/run_turn", run_turn),
        ("POST", "/agent/trace", get_trace),
        ("POST", "/agent/explain", explain),
        ("GET",  "/agent/open_turns", open_turns),
        ("POST", "/agent/turn_state", turn_state),
        ("GET",  "/agent/sessions", sessions_list),
        ("GET",  "/agent/session", session_get),
        ("POST", "/agent/session/delete", session_delete),
        ("POST", "/agent/session/rename", session_rename),
        ("POST", "/agent/session/message/edit", session_message_edit),
        ("POST", "/agent/session/message/delete", session_message_delete),
        ("POST", "/agent/session/skill_state", session_record_skill_state),
        ("POST", "/agent/session/search", session_search),
        ("GET",  "/agent/session/events", session_recent_events),
        ("GET",  "/agent/session/transcript", session_transcript_handler),
        ("GET",  "/agent/stream/events", stream_events),
        ("POST", "/agent/interrupt", interrupt),
        ("GET",  "/agent/tools", tool_registry),
    ]
