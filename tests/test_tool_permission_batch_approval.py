from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import time

import pytest

from nerya.acp.server import AcpServer
from nerya.api import routes_approvals
from nerya.core import jsonl
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core.time import now_iso
from nerya.tools.permissions import (
    PermissionDecision,
    PermissionDecisionKind,
)
from nerya.tools.registry import make_native_descriptor
from nerya.tools.tool_approvals import (
    ToolApprovalCoordinator,
    ToolApprovalScope,
)
from nerya.tools.types import (
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolResult,
)


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(
        paths=WorkspacePaths(root=tmp_path),
        data=deepcopy(DEFAULT_CONFIG),
    )


def _scope(
    *,
    session: str = "session-1",
    strategy: str = "",
    actor: str = "actor-1",
) -> ToolApprovalScope:
    return ToolApprovalScope.from_values(
        session_id=session,
        strategy_id=strategy,
        actor_id=actor,
    )


def _decision() -> PermissionDecision:
    return PermissionDecision(
        kind=PermissionDecisionKind.ASK,
        reason="operator approval required",
        risk=RiskLevel.EXEC,
        scope=PermissionScope.SYSTEM,
        requires_approval=True,
        approval_reason="operator approval required",
    )


def _descriptor(name: str):
    return make_native_descriptor(
        name=name,
        description=f"approval probe for {name}",
        input_schema={"type": "object"},
        handler=lambda call: ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"ok": True},
        ),
        risk=RiskLevel.EXEC,
        permission_scope=PermissionScope.SYSTEM,
    )


def _call(name: str, call_id: str, arguments: dict, *, turn_id: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        name=name,
        arguments=dict(arguments),
        turn_id=turn_id,
    )


def _coordinator(
    cfg: Config,
    *,
    scope: ToolApprovalScope,
    turn_id: str,
    approval_id: str = "",
) -> ToolApprovalCoordinator:
    return ToolApprovalCoordinator(
        cfg,
        scope=scope,
        turn_id=turn_id,
        resume_approval_id=approval_id,
    )


def test_batch_round_trip_is_scoped_and_one_shot(tmp_path) -> None:
    cfg = _config(tmp_path)
    scope = _scope()
    coordinator = _coordinator(cfg, scope=scope, turn_id="turn-1")
    call_a = _call("run_shell", "call-a", {"cmd": "echo a"}, turn_id="turn-1")
    call_b = _call("write_file", "call-b", {"path": "notes.txt"}, turn_id="turn-1")

    first = coordinator.resolve(call_a, _descriptor(call_a.name), _decision())
    second = coordinator.resolve(call_b, _descriptor(call_b.name), _decision())

    assert first.request is not None and second.request is not None
    approval_id = first.request["approval_id"]
    assert second.request["approval_id"] == approval_id
    pending = jsonl.read_all(cfg.paths.approvals_pending)
    assert len(pending) == 1
    assert pending[0]["tool_use_ids"] == ["call-a", "call-b"]
    assert second.request["prompt"]["metadata"]["tool_count"] == 2

    outcome = routes_approvals._callback(
        SimpleNamespace(config=cfg),
        {
            "callback_data": f"approve:{approval_id}",
            "actor_id": scope.actor_id,
        },
    )
    assert outcome["ok"] is True
    assert outcome["batch"] is True

    resumed = _coordinator(
        cfg,
        scope=scope,
        turn_id="turn-resume",
        approval_id=approval_id,
    )
    assert resumed.resolve(call_a, _descriptor(call_a.name), _decision()).verdict is True
    assert resumed.resolve(call_b, _descriptor(call_b.name), _decision()).verdict is True

    repeated = resumed.resolve(call_a, _descriptor(call_a.name), _decision())
    assert repeated.verdict is None
    assert repeated.request is not None
    assert repeated.request["approval_id"] != approval_id


def test_scope_fingerprint_and_expiry_fail_closed(tmp_path) -> None:
    cfg = _config(tmp_path)
    scope = _scope(session="session-s", strategy="strategy-s", actor="actor-s")
    call = _call("run_shell", "call-s", {"cmd": "echo safe"}, turn_id="turn-s")
    request = _coordinator(cfg, scope=scope, turn_id="turn-s").resolve(
        call,
        _descriptor(call.name),
        _decision(),
    ).request
    assert request is not None
    approval_id = request["approval_id"]
    assert routes_approvals._callback(
        SimpleNamespace(config=cfg),
        {"callback_data": f"approve:{approval_id}", "actor_id": scope.actor_id},
    )["ok"] is True

    mismatches = (
        (_scope(session="other", strategy="strategy-s", actor="actor-s"), call),
        (_scope(session="session-s", strategy="strategy-s", actor="other"), call),
        (scope, _call("run_shell", "call-x", {"cmd": "echo changed"}, turn_id="turn-x")),
        (scope, _call("write_file", "call-x", {"cmd": "echo safe"}, turn_id="turn-x")),
    )
    for changed_scope, changed_call in mismatches:
        result = _coordinator(
            cfg,
            scope=changed_scope,
            turn_id="turn-x",
            approval_id=approval_id,
        ).resolve(changed_call, _descriptor(changed_call.name), _decision())
        assert result.verdict is None
        assert result.request is not None

    approved = jsonl.read_all(cfg.paths.approvals_approved)
    approved[0]["expires_at"] = time.time() - 1
    jsonl.write_all(cfg.paths.approvals_approved, approved)
    expired = _coordinator(
        cfg,
        scope=scope,
        turn_id="turn-expired",
        approval_id=approval_id,
    ).resolve(call, _descriptor(call.name), _decision())
    assert expired.verdict is None
    assert expired.request is not None


def test_http_operator_and_acp_share_canonical_transition(tmp_path) -> None:
    cfg = _config(tmp_path)
    scope = _scope(actor="requester")
    first = _coordinator(cfg, scope=scope, turn_id="turn-http").resolve(
        _call("run_shell", "call-http", {"cmd": "echo http"}, turn_id="turn-http"),
        _descriptor("run_shell"),
        _decision(),
    ).request
    assert first is not None

    http = routes_approvals._callback(
        SimpleNamespace(config=cfg),
        {
            "callback_data": f"approve:{first['approval_id']}",
            "actor_id": "spoofed-body-actor",
            "_auth_actor_id": "trusted-operator",
            "_auth_scopes": ["approve:tool"],
        },
    )
    assert http["ok"] is True
    assert jsonl.read_all(cfg.paths.approvals_approved)[0][
        "resolved_by_actor_id"
    ] == "trusted-operator"

    second = _coordinator(cfg, scope=scope, turn_id="turn-acp").resolve(
        _call("run_shell", "call-acp", {"cmd": "echo acp"}, turn_id="turn-acp"),
        _descriptor("run_shell"),
        _decision(),
    ).request
    assert second is not None
    server = AcpServer(client=SimpleNamespace(config=cfg))
    subscription = server.events.subscribe(
        kinds=("approval.resolved",),
        now_iso=now_iso,
    )
    moved = server._move_approval(
        second["approval_id"],
        "approved",
        note="acp approval",
        resolver_actor_id=scope.actor_id,
    )
    assert moved["ok"] is True
    assert server.events.drain(subscription.id)[0]["kind"] == "approval.resolved"


def test_unowned_or_expired_native_request_cannot_be_resolved(tmp_path) -> None:
    cfg = _config(tmp_path)
    client = SimpleNamespace(config=cfg)
    for approval_id, expires_at in (
        ("unowned", time.time() + 60),
        ("expired", time.time() - 1),
    ):
        jsonl.append(
            cfg.paths.approvals_pending,
            {
                "approval_id": approval_id,
                "kind": "tool_permission",
                "state": "pending",
                "requester_actor_id": "" if approval_id == "unowned" else "actor",
                "expires_at": expires_at,
            },
        )
        result = routes_approvals._callback(
            client,
            {
                "callback_data": f"approve:{approval_id}",
                "actor_id": "actor",
            },
        )
        assert result["ok"] is False

    assert jsonl.read_all(cfg.paths.approvals_approved) == []
