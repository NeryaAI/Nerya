"""Workbench read-only capabilities and projection routes."""
from __future__ import annotations

import hashlib

from ..agent.command_store import CommandError
from ..agent.history_mutations import HistoryMutationError
from ..agent.workbench import session_view
from ..core.runtime_identity import BUILD_ID, SDK_BUILD_ID, STARTED_AT

# Capture code identity once, never claim that on-disk edits changed a live process.
_STARTED, _BUILD = STARTED_AT, BUILD_ID


def runtime_info(client, _query):
    return {"ok": True, "protocol_version": 1, "build_id": _BUILD, "sdk_build_id": SDK_BUILD_ID, "started_at": _STARTED,
            "workspace_id": hashlib.sha256(str(client.config.paths.root.resolve()).encode()).hexdigest()[:24],
            "capabilities": ["conversation_commands", "session_view", "evidence_delivery", "user_interactions", "plan_mode", "goal_mode",'task_runs','finite_funds_authorization','funds_receipt_reconciliation']}


def view(client, query):
    try:
        return session_view(client.config, query.get("session_id"))
    except HistoryMutationError as exc:
        return {"ok":False,"_status":400,"error":exc.code}
    except CommandError as exc:
        return {"ok": False, "_status": exc.status, "error": exc.code}


def routes():
    return [("GET", "/runtime/info", runtime_info), ("GET", "/agent/sessions/view", view)]
