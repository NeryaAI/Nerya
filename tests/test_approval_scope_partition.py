from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import time

import pytest

from nerya.api import route_scopes, routes_approvals
from nerya.core import jsonl
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(
        paths=WorkspacePaths(root=tmp_path),
        data=deepcopy(DEFAULT_CONFIG),
    )


def _http_callback(approval_id: str, *, scope: str, actor: str = "operator") -> dict:
    return {
        "callback_data": f"approve:{approval_id}",
        "actor_id": "body-controlled",
        "_auth_actor_id": actor,
        "_auth_scopes": [scope],
    }


def _pending_trade(cfg: Config, approval_id: str, *, owner: str = "researcher") -> None:
    jsonl.append(
        cfg.paths.approvals_pending,
        {
            "approval_id": approval_id,
            "kind": "trade_intent",
            "state": "pending",
            "approval_actor_id": owner,
            "actor_id": owner,
            "account_id": "paper_main",
            "strategy_id": "manual_agent",
            "market": "mock:BTC/USDT",
            "side": "buy",
            "order_type": "market",
            "size": 100.0,
            "execution_mode": "paper",
        },
    )


def _pending_tool(cfg: Config, approval_id: str, *, owner: str = "researcher") -> None:
    jsonl.append(
        cfg.paths.approvals_pending,
        {
            "approval_id": approval_id,
            "kind": "tool_permission",
            "state": "pending",
            "requester_actor_id": owner,
            "approval_actor_id": owner,
            "expires_at": time.time() + 60,
            "payload": {"tool": {"name": "run_shell"}},
        },
    )


def test_callback_route_accepts_either_approval_scope_only():
    required = route_scopes.required_scope("POST", "/approvals/callback")
    assert required == "approve:trade|approve:tool"
    assert route_scopes.authorize(
        {"approve:trade"}, "POST", "/approvals/callback"
    ) == (True, None)
    assert route_scopes.authorize(
        {"approve:tool"}, "POST", "/approvals/callback"
    ) == (True, None)
    allowed, reason = route_scopes.authorize(
        {"read:sessions"}, "POST", "/approvals/callback"
    )
    assert allowed is False
    assert reason == "insufficient_scope:needed_any=approve:trade|approve:tool"


def test_trade_scope_can_resolve_trade_approval_for_another_owner(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _pending_trade(cfg, "trade-scope-ok")
    monkeypatch.setattr(
        routes_approvals,
        "_publish_approval_resolution",
        lambda *_args, **_kwargs: None,
    )

    outcome = routes_approvals._callback(
        SimpleNamespace(config=cfg),
        _http_callback("trade-scope-ok", scope="approve:trade"),
    )

    assert outcome["ok"] is True
    assert outcome["state"] == "approved"
    approved = jsonl.read_all(cfg.paths.approvals_approved)
    assert approved[0]["resolved_by_actor_id"] == "operator"
    assert jsonl.read_all(cfg.paths.approvals_pending) == []


def test_tool_scope_cannot_resolve_trade_approval_even_when_actor_owns_it(tmp_path):
    cfg = _config(tmp_path)
    _pending_trade(cfg, "trade-scope-denied", owner="operator")

    outcome = routes_approvals._callback(
        SimpleNamespace(config=cfg),
        _http_callback("trade-scope-denied", scope="approve:tool"),
    )

    assert outcome == {
        "ok": False,
        "error": "insufficient approval scope",
        "approval_id": "trade-scope-denied",
        "required_scope": "approve:trade",
    }
    assert jsonl.read_all(cfg.paths.approvals_pending)[0]["state"] == "pending"


def test_trade_scope_cannot_resolve_tool_approval_even_when_actor_owns_it(tmp_path):
    cfg = _config(tmp_path)
    _pending_tool(cfg, "tool-scope-denied", owner="operator")

    outcome = routes_approvals._callback(
        SimpleNamespace(config=cfg),
        _http_callback("tool-scope-denied", scope="approve:trade"),
    )

    assert outcome == {
        "ok": False,
        "error": "insufficient approval scope",
        "approval_id": "tool-scope-denied",
        "required_scope": "approve:tool",
    }
    assert jsonl.read_all(cfg.paths.approvals_pending)[0]["state"] == "pending"


def test_api_all_remains_authorized_for_trade_and_tool_records(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _pending_trade(cfg, "trade-admin")
    _pending_tool(cfg, "tool-admin")
    monkeypatch.setattr(
        routes_approvals,
        "_publish_approval_resolution",
        lambda *_args, **_kwargs: None,
    )

    trade = routes_approvals._callback(
        SimpleNamespace(config=cfg),
        _http_callback("trade-admin", scope="api:all", actor="admin"),
    )
    tool = routes_approvals._callback(
        SimpleNamespace(config=cfg),
        _http_callback("tool-admin", scope="api:all", actor="admin"),
    )

    assert trade["ok"] is True
    assert tool["ok"] is True
    assert {row["approval_id"] for row in jsonl.read_all(cfg.paths.approvals_approved)} == {
        "trade-admin",
        "tool-admin",
    }
