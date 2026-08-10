from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import time

import pytest

from nerya.agent.kernel import AgentKernel
from nerya.acp.server import AcpServer
from nerya.api import routes_approvals
from nerya.core import jsonl
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core.time import now_iso


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(
        paths=WorkspacePaths(root=tmp_path),
        data=deepcopy(DEFAULT_CONFIG),
    )


def _kernel(cfg: Config) -> AgentKernel:
    return AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]


def _permission_block(call_id: str, action: str, payload: dict) -> dict:
    return {
        "kind": "tool_result",
        "call_id": call_id,
        "skill_id": "native",
        "action": action,
        "ok": False,
        "error": f"{action} requires approval",
        "error_kind": "permission_pending",
        "payload": payload,
    }


def test_same_turn_tool_permissions_are_merged_and_approved_as_batch(tmp_path):
    cfg = _config(tmp_path)
    kernel = _kernel(cfg)

    first = kernel._record_tool_permission_request(
        turn_id="turn1",
        session_id="session1",
        strategy_id=None,
        requester_actor_id="actor1",
        block=_permission_block("call_a", "run_shell", {"cmd": "echo a"}),
    )
    second = kernel._record_tool_permission_request(
        turn_id="turn1",
        session_id="session1",
        strategy_id=None,
        requester_actor_id="actor1",
        block=_permission_block("call_b", "write_file", {"path": "notes.txt"}),
    )

    assert first["approval_id"] == second["approval_id"]
    assert first["approval_id"].startswith("tool_batch_turn1_")
    pending = jsonl.read_all(cfg.paths.approvals_pending)
    assert len(pending) == 1
    assert pending[0]["kind"] == "tool_permission_batch"
    assert pending[0]["tool_use_ids"] == ["call_a", "call_b"]
    assert len(pending[0]["items"]) == 2
    assert second["prompt"]["metadata"]["tool_batch"] is True
    assert second["prompt"]["metadata"]["tool_count"] == 2
    assert "native.run_shell" in second["prompt"]["text"]
    assert "native.write_file" in second["prompt"]["text"]

    assert kernel._lookup_tool_permission_decision(
        session_id="session1",
        tool_name="run_shell",
        payload={"cmd": "echo a"},
        call_id="call_a",
        requester_actor_id="actor1",
    ) is None

    client = SimpleNamespace(config=cfg)
    outcome = routes_approvals._callback(
        client,
        {
            "callback_data": f"approve:{first['approval_id']}",
            "actor_id": "actor1",
        },
    )

    assert outcome["ok"] is True
    assert outcome["state"] == "approved"
    assert outcome["batch"] is True
    assert outcome["item_count"] == 2
    assert jsonl.read_all(cfg.paths.approvals_pending) == []
    approved = jsonl.read_all(cfg.paths.approvals_approved)
    assert len(approved) == 1
    assert approved[0]["kind"] == "tool_permission_batch"

    assert kernel._lookup_tool_permission_decision(
        session_id="session1",
        tool_name="run_shell",
        payload={"cmd": "echo a"},
        call_id="call_a",
        requester_actor_id="actor1",
    ) is True
    assert kernel._lookup_tool_permission_decision(
        session_id="session1",
        tool_name="run_shell",
        payload={"cmd": "echo a"},
        call_id="call_a_retry",
        requester_actor_id="actor1",
    ) is None
    assert kernel._lookup_tool_permission_decision(
        session_id="session1",
        tool_name="write_file",
        payload={"path": "notes.txt"},
        call_id="call_b",
        requester_actor_id="actor1",
    ) is True

    approved = jsonl.read_all(cfg.paths.approvals_approved)[0]
    assert approved["requester_actor_id"] == "actor1"
    assert approved["resolved_by_actor_id"] == "actor1"


def test_http_approval_scope_is_trusted_and_body_actor_is_ignored(tmp_path):
    cfg = _config(tmp_path)
    kernel = _kernel(cfg)
    request = kernel._record_tool_permission_request(
        turn_id="turn-http-operator",
        session_id="session-http-operator",
        strategy_id=None,
        requester_actor_id="requester",
        block=_permission_block("call-http-operator", "run_shell", {"cmd": "echo safe"}),
    )

    outcome = routes_approvals._callback(
        SimpleNamespace(config=cfg),
        {
            "callback_data": f"approve:{request['approval_id']}",
            "actor_id": "spoofed-body-actor",
            "_auth_actor_id": "trusted-operator",
            "_auth_scopes": ["approve:tool"],
        },
    )

    assert outcome["ok"] is True
    approved = jsonl.read_all(cfg.paths.approvals_approved)
    assert approved[0]["resolved_by_actor_id"] == "trusted-operator"
    assert kernel._lookup_tool_permission_decision(
        session_id="session-http-operator",
        requester_actor_id="requester",
        tool_name="run_shell",
        payload={"cmd": "echo safe"},
        call_id="call-http-operator",
    ) is True


def test_unowned_native_approval_cannot_be_resolved(tmp_path):
    cfg = _config(tmp_path)
    jsonl.append(
        cfg.paths.approvals_pending,
        {
            "approval_id": "unowned-native",
            "kind": "tool_permission",
            "state": "pending",
            "expires_at": time.time() + 60,
        },
    )

    outcome = routes_approvals._callback(
        SimpleNamespace(config=cfg),
        {
            "callback_data": "approve:unowned-native",
            "actor_id": "operator",
        },
    )

    assert outcome["ok"] is False
    assert jsonl.read_all(cfg.paths.approvals_pending)[0]["state"] == "pending"


def test_acp_approval_uses_canonical_move_and_owner(tmp_path):
    cfg = _config(tmp_path)
    kernel = _kernel(cfg)
    request = kernel._record_tool_permission_request(
        turn_id="turn-acp",
        session_id="session-acp",
        strategy_id=None,
        requester_actor_id="acp-owner",
        block=_permission_block("call-acp", "run_shell", {"cmd": "echo acp"}),
    )
    server = AcpServer(client=SimpleNamespace(config=cfg))
    subscription = server.events.subscribe(
        kinds=("approval.resolved",),
        now_iso=now_iso,
    )

    result = server._move_approval(
        request["approval_id"],
        "approved",
        note="acp approval",
        resolver_actor_id="acp-owner",
    )

    assert result["ok"] is True
    assert jsonl.read_all(cfg.paths.approvals_pending) == []
    approved = jsonl.read_all(cfg.paths.approvals_approved)
    assert approved[0]["resolved_by_actor_id"] == "acp-owner"
    events = server.events.drain(subscription.id)
    assert events and events[0]["kind"] == "approval.resolved"


def test_tool_permission_scope_and_expiry_fail_closed(tmp_path):
    cfg = _config(tmp_path)
    kernel = _kernel(cfg)
    request = kernel._record_tool_permission_request(
        turn_id="turn-scoped",
        session_id="session-scoped",
        strategy_id="strategy-scoped",
        requester_actor_id="actor-scoped",
        block=_permission_block("call-scoped", "run_shell", {"cmd": "echo safe"}),
    )
    client = SimpleNamespace(config=cfg)
    assert routes_approvals._callback(
        client,
        {
            "callback_data": f"approve:{request['approval_id']}",
            "actor_id": "actor-scoped",
        },
    )["ok"] is True

    base = {
        "session_id": "session-scoped",
        "strategy_id": "strategy-scoped",
        "requester_actor_id": "actor-scoped",
        "tool_name": "run_shell",
        "payload": {"cmd": "echo safe"},
        "call_id": "call-scoped",
    }
    for changed in (
        {"session_id": "other-session"},
        {"session_id": None},
        {"strategy_id": "other-strategy"},
        {"requester_actor_id": "other-actor"},
        {"requester_actor_id": None},
        {"tool_name": "write_file"},
        {"payload": {"cmd": "echo changed"}},
        {"approval_id": "other-approval"},
    ):
        assert kernel._lookup_tool_permission_decision(**{**base, **changed}) is None

    approved = jsonl.read_all(cfg.paths.approvals_approved)
    approved[0]["expires_at"] = time.time() - 1
    jsonl.write_all(cfg.paths.approvals_approved, approved)
    assert kernel._lookup_tool_permission_decision(**base) is None


def test_expired_native_approval_cannot_be_moved(tmp_path):
    cfg = _config(tmp_path)
    kernel = _kernel(cfg)
    request = kernel._record_tool_permission_request(
        turn_id="turn-expired",
        session_id="session-expired",
        strategy_id=None,
        requester_actor_id="actor-expired",
        block=_permission_block("call-expired", "run_shell", {"cmd": "echo no"}),
    )
    pending = jsonl.read_all(cfg.paths.approvals_pending)
    pending[0]["expires_at"] = time.time() - 1
    jsonl.write_all(cfg.paths.approvals_pending, pending)

    client = SimpleNamespace(config=cfg)
    assert routes_approvals._read_pending(client) == []
    outcome = routes_approvals._callback(
        client,
        {
            "callback_data": f"approve:{request['approval_id']}",
            "actor_id": "actor-expired",
        },
    )
    assert outcome["ok"] is False
    assert jsonl.read_all(cfg.paths.approvals_approved) == []


def test_rejected_tool_permission_is_consumed_once(tmp_path):
    cfg = _config(tmp_path)
    kernel = _kernel(cfg)
    request = kernel._record_tool_permission_request(
        turn_id="turn-rejected",
        session_id="session-rejected",
        strategy_id=None,
        requester_actor_id="actor-rejected",
        block=_permission_block("call-rejected", "run_shell", {"cmd": "echo no"}),
    )
    client = SimpleNamespace(config=cfg)
    outcome = routes_approvals._callback(
        client,
        {
            "callback_data": f"reject:{request['approval_id']}",
            "actor_id": "actor-rejected",
        },
    )
    assert outcome["ok"] is True

    lookup = {
        "session_id": "session-rejected",
        "requester_actor_id": "actor-rejected",
        "approval_id": request["approval_id"],
        "tool_name": "run_shell",
        "payload": {"cmd": "echo no"},
        "call_id": "call-rejected-retry",
    }
    assert kernel._lookup_tool_permission_decision(**lookup) is False
    assert kernel._lookup_tool_permission_decision(**lookup) is None
    rejected = jsonl.read_all(cfg.paths.approvals_rejected)
    assert rejected[0]["items"][0]["consumed_call_id"] == "call-rejected-retry"


def test_tool_permission_batches_do_not_merge_across_requester_scope(tmp_path):
    cfg = _config(tmp_path)
    kernel = _kernel(cfg)

    first = kernel._record_tool_permission_request(
        turn_id="shared-turn",
        session_id="session-a",
        strategy_id="strategy-a",
        requester_actor_id="actor-a",
        block=_permission_block("shared-call", "run_shell", {"cmd": "echo safe"}),
    )
    second = kernel._record_tool_permission_request(
        turn_id="shared-turn",
        session_id="session-b",
        strategy_id="strategy-a",
        requester_actor_id="actor-b",
        block=_permission_block("shared-call", "run_shell", {"cmd": "echo safe"}),
    )

    assert first["approval_id"] != second["approval_id"]
    pending = jsonl.read_all(cfg.paths.approvals_pending)
    assert len(pending) == 2
    assert {row["requester_session_id"] for row in pending} == {
        "session-a",
        "session-b",
    }


def test_consumed_rejection_does_not_swallow_a_new_request(tmp_path):
    cfg = _config(tmp_path)
    kernel = _kernel(cfg)
    first = kernel._record_tool_permission_request(
        turn_id="turn-rejected-first",
        session_id="session-rejected",
        strategy_id=None,
        requester_actor_id="actor-rejected",
        block=_permission_block("call-rejected", "run_shell", {"cmd": "echo no"}),
    )
    client = SimpleNamespace(config=cfg)
    assert routes_approvals._callback(
        client,
        {
            "callback_data": f"reject:{first['approval_id']}",
            "actor_id": "actor-rejected",
        },
    )["ok"] is True
    lookup = {
        "session_id": "session-rejected",
        "requester_actor_id": "actor-rejected",
        "approval_id": first["approval_id"],
        "tool_name": "run_shell",
        "payload": {"cmd": "echo no"},
        "call_id": "call-rejected-retry",
    }
    assert kernel._lookup_tool_permission_decision(**lookup) is False

    second = kernel._record_tool_permission_request(
        turn_id="turn-rejected-second",
        session_id="session-rejected",
        strategy_id=None,
        requester_actor_id="actor-rejected",
        block=_permission_block("call-rejected", "run_shell", {"cmd": "echo no"}),
    )

    assert second["record"]["state"] == "pending"
    assert second["approval_id"] != first["approval_id"]
    assert len(jsonl.read_all(cfg.paths.approvals_pending)) == 1
