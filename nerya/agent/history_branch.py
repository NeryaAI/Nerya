"""History forks preserve provenance, never duplicate execution records."""
from __future__ import annotations

import hashlib
import json
import re
import time

from ..db.repositories import AgentSessionRepository
from .history_mutations import HistoryMutationError, _session_id, _transaction, is_session_deleted


def fork_session(paths, payload: dict) -> dict:
    sid = _session_id(payload.get("session_id"))
    mid = payload.get("message_id")
    key = payload.get("client_request_id")
    expected = payload.get("expected_content")
    if not isinstance(mid,str) or not mid or not isinstance(expected,str):
        raise HistoryMutationError("invalid_message_id")
    if not isinstance(key,str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,160}",key):
        raise HistoryMutationError("invalid_request_id")
    digest = hashlib.sha256(json.dumps([sid,mid,expected],ensure_ascii=False).encode()).hexdigest()
    new_sid = "branch_"+hashlib.sha256((sid+"\0"+key).encode()).hexdigest()[:32]
    with _transaction(paths) as con:
        if is_session_deleted(con,sid) or is_session_deleted(con,new_sid):
            raise HistoryMutationError("session_deleted")
        repo = AgentSessionRepository(con)
        existing = repo.get_session(new_sid)
        if existing:
            if json.loads(existing.get("meta_json") or "{}").get("branch_of",{}).get("request_hash")!=digest:
                raise HistoryMutationError("message_conflict")
            return {"ok":True,"session_id":new_sid,"source_session_id":sid,"duplicate":True}
        source = repo.get_session(sid)
        if not source or source.get("source") in ("mcp","tunnel"):
            raise HistoryMutationError("session_not_found")
        # A fork is a new editable conversation, not a runtime rewind. A running
        # source can be read transactionally without stopping or mutating it.
        count = con.execute("SELECT count(*) FROM agent_messages WHERE session_id=? AND deleted=0",(sid,)).fetchone()[0]
        if count>2000:
            raise HistoryMutationError("branch_history_too_large")
        rows = repo.transcript(sid,limit=0)
        anchor = next((index for index,row in enumerate(rows) if row["message_id"]==mid),None)
        if anchor is None or rows[anchor]["role"]!="user":
            raise HistoryMutationError("message_not_found")
        if rows[anchor]["content"]!=expected:
            raise HistoryMutationError("message_conflict")
        source_meta=json.loads(source.get("meta_json") or "{}")
        now=time.time()
        repo.upsert_session(session_id=new_sid,source="user_chat",strategy_id=source.get("strategy_id"),
            title=(str(source.get("title") or "Conversation")[:65]+" · "+("分支" if payload.get("language")=="zh" else "Branch")),
            meta={"assistant_id":source_meta.get("assistant_id"), "assistant_name":source_meta.get("assistant_name"),
                  "strategy_proposal_id":source_meta.get("strategy_proposal_id"),
                  "branch_of":{"session_id":sid,"message_id":mid,"request_hash":digest,"created_at":now},
                  "title_source":"operator"})
        # Do not copy active approval cards, tool executions, checkpoints,
        # financial records or remembered credentials into the branch.
        for index,row in enumerate(rows[:anchor]):
            old_meta=json.loads(row.get("meta_json") or "{}")
            repo.record_message(message_id=f"{new_sid}:history:{index}",session_id=new_sid,
                role=row["role"],content=row["content"],turn_id=f"{new_sid}:history:{index//2}",
                ts=now-(anchor-index)*.01,
                meta={"branch_reference":{"session_id":sid,"message_id":row["message_id"]},
                      "attachments":old_meta.get("attachments",[])})
    return {"ok":True,"session_id":new_sid,"source_session_id":sid,"duplicate":False,
            "copied_messages":anchor,"strategy_id":source.get("strategy_id")}
