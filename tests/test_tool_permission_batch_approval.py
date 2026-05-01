from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.agent.kernel import AgentKernel
from nerya.api import routes_approvals
from nerya.core import jsonl
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths


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
        block=_permission_block("call_a", "run_shell", {"cmd": "echo a"}),
    )
    second = kernel._record_tool_permission_request(
        turn_id="turn1",
        session_id="session1",
        strategy_id=None,
        block=_permission_block("call_b", "write_file", {"path": "notes.txt"}),
    )

    assert first["approval_id"] == second["approval_id"] == "tool_batch_turn1"
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
    ) is None

    client = SimpleNamespace(config=cfg)
    outcome = routes_approvals._callback(
        client,
        {"callback_data": "approve:tool_batch_turn1", "actor_id": "dashboard"},
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
    ) is True
    assert kernel._lookup_tool_permission_decision(
        session_id="session1",
        tool_name="write_file",
        payload={"path": "notes.txt"},
        call_id="call_b",
    ) is True
