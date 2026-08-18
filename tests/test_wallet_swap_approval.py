from __future__ import annotations

from copy import deepcopy
import json
import time
from types import SimpleNamespace

import pytest

from nerya.api import local_server, routes_approvals
from nerya.api.auth import AuthResult
from nerya.api.routes_wallet import routes as wallet_routes
from nerya.core import jsonl
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.db.repositories import ApprovalRepository
from nerya.db.sqlite import connect
from nerya.wallet import swap_approval
from nerya.wallet.protocol import WalletQuote, WalletSwapResult


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    data["runtime"]["live_trading_enabled"] = True
    data["runtime"]["kill_switch"] = False
    return Config(paths=WorkspacePaths(root=tmp_path), data=data)


def _handler():
    return next(
        handler
        for method, path, handler in wallet_routes()
        if method == "POST" and path == "/wallet/swap"
    )


def _stamped(
    payload: dict,
    *scopes: str,
    actor: str = "token:wallet-operator",
) -> dict:
    auth = AuthResult(
        ok=True,
        actor=actor,
        scope=scopes[0] if scopes else "",
        scopes=frozenset(scopes),
    )
    return local_server._stamp_trusted_auth(payload, auth)


def _payload() -> dict:
    return {
        "provider": "byreal",
        "chain": "solana",
        "token_in": "SOL",
        "token_out": "USDC",
        "amount_in": 1.25,
        "slippage_bps": 30,
        "receiver": "receiver-1",
        "session_id": "session-wallet",
        "turn_id": "turn-wallet",
        "tool_call_id": "tool-wallet",
    }


class FakeProvider:
    def __init__(self, expected_out: list[float] | None = None) -> None:
        self.expected_out = list(expected_out or [100.0, 100.0])
        self.quote_calls: list[dict] = []
        self.swap_calls: list[dict] = []

    def quote(self, **kwargs):  # noqa: ANN001
        self.quote_calls.append(dict(kwargs))
        idx = min(len(self.quote_calls) - 1, len(self.expected_out) - 1)
        expected = self.expected_out[idx]
        return WalletQuote(
            provider="byreal",
            chain=kwargs["chain"],
            token_in=kwargs["token_in"],
            token_out=kwargs["token_out"],
            amount_in=kwargs["amount_in"],
            expected_out=expected,
            min_out=expected * 0.99,
            slippage_bps=kwargs["slippage_bps"],
            price_impact_bps=12,
            gas_cost_usd=0.08,
        )

    def swap(self, **kwargs):  # noqa: ANN001
        self.swap_calls.append(dict(kwargs))
        return WalletSwapResult(
            provider="byreal",
            chain=kwargs["chain"],
            ok=True,
            tx_hash="tx-wallet-1",
            amount_in=kwargs["amount_in"],
            amount_out=99.5,
        )


def _request(cfg: Config, monkeypatch, provider: FakeProvider) -> dict:
    monkeypatch.setattr(
        swap_approval,
        "build_provider",
        lambda *_args, **_kwargs: provider,
    )
    return _handler()(
        SimpleNamespace(config=cfg),
        _stamped(_payload(), "trade:live"),
    )


def _approve(cfg: Config, approval_id: str, *, scope: str = "approve:trade") -> dict:
    return routes_approvals._callback(
        SimpleNamespace(config=cfg),
        {
            "callback_data": f"approve:{approval_id}",
            "_auth_actor_id": "token:wallet-operator",
            "_auth_scopes": [scope],
        },
    )


def _db_approval(cfg: Config, approval_id: str) -> dict:
    con = connect(cfg.paths.db)
    try:
        row = ApprovalRepository(con).get(approval_id)
    finally:
        con.close()
    assert row is not None
    return row


def test_wallet_swap_request_is_frozen_with_full_quote_and_no_side_effect(
    tmp_path,
    monkeypatch,
):
    cfg = _config(tmp_path)
    provider = FakeProvider()

    result = _request(cfg, monkeypatch, provider)

    assert result["status"] == "pending_approval"
    assert result["execution_mode"] == "live"
    assert result["quote"]["expected_out"] == 100.0
    assert result["quote"]["min_out"] == 99.0
    assert len(provider.quote_calls) == 1
    assert provider.swap_calls == []

    pending = jsonl.read_all(cfg.paths.approvals_pending)
    assert len(pending) == 1
    record = pending[0]
    assert record["kind"] == "wallet_swap"
    assert record["actor_id"] == "token:wallet-operator"
    assert record["approval_actor_id"] == "token:wallet-operator"
    assert record["execution_mode"] == "live"
    assert record["wallet_swap"] == {
        "provider": "byreal",
        "chain": "solana",
        "token_in": "SOL",
        "token_out": "USDC",
        "amount_in": 1.25,
        "slippage_bps": 30,
        "receiver": "receiver-1",
    }
    assert record["quote"]["gas_cost_usd"] == pytest.approx(0.08)
    assert record["session_id"] == "session-wallet"
    assert record["turn_id"] == "turn-wallet"
    assert record["tool_call_id"] == "tool-wallet"

    cards = routes_approvals._pending(SimpleNamespace(config=cfg), {})
    assert cards["count"] == 1
    prompt = cards["approvals"][0]["prompt"]
    assert prompt["metadata"]["kind"] == "wallet_swap"
    assert prompt["metadata"]["wallet_swap"]["token_in"] == "SOL"
    assert prompt["metadata"]["quote"]["min_out"] == 99.0
    assert "Wallet swap approval requested" in prompt["text"]


def test_wallet_swap_requires_financial_approval_scope_even_for_owner(
    tmp_path,
    monkeypatch,
):
    cfg = _config(tmp_path)
    provider = FakeProvider()
    requested = _request(cfg, monkeypatch, provider)

    outcome = _approve(cfg, requested["approval_id"], scope="approve:tool")

    assert outcome == {
        "ok": False,
        "error": "insufficient approval scope",
        "approval_id": requested["approval_id"],
        "required_scope": "approve:trade",
    }
    assert provider.swap_calls == []
    assert jsonl.read_all(cfg.paths.approvals_pending)[0]["state"] == "pending"


def test_approved_wallet_swap_executes_exactly_once(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    provider = FakeProvider()
    requested = _request(cfg, monkeypatch, provider)
    approval_id = requested["approval_id"]

    approved = _approve(cfg, approval_id)

    assert approved["ok"] is True
    assert approved["state"] == "approved"
    assert approved["approval_kind"] == "wallet_swap"
    assert approved["resume"]["ok"] is True
    response = approved["resume"]["resume_response"]
    assert response["ok"] is True
    assert response["result"]["tx_hash"] == "tx-wallet-1"
    assert len(provider.quote_calls) == 2
    assert len(provider.swap_calls) == 1
    assert provider.swap_calls[0]["live"] is True
    assert provider.swap_calls[0]["min_out"] == pytest.approx(99.0)

    duplicate = swap_approval.resume_approved(cfg, approval_id)
    assert duplicate["ok"] is True
    assert duplicate["already_resumed"] is True
    assert len(provider.swap_calls) == 1
    assert _db_approval(cfg, approval_id)["state"] == "resumed"


def test_quote_move_below_approved_floor_requires_fresh_approval(
    tmp_path,
    monkeypatch,
):
    cfg = _config(tmp_path)
    provider = FakeProvider(expected_out=[100.0, 98.0])
    requested = _request(cfg, monkeypatch, provider)

    approved = _approve(cfg, requested["approval_id"])

    assert approved["ok"] is True
    resume = approved["resume"]
    assert resume["ok"] is False
    assert resume["resume_response"]["error"] == "quote_moved_requires_reapproval"
    assert resume["resume_response"]["approved_min_out"] == pytest.approx(99.0)
    assert provider.swap_calls == []
    assert _db_approval(cfg, requested["approval_id"])["state"] == "resumed"
    duplicate = swap_approval.resume_approved(cfg, requested["approval_id"])
    assert duplicate["already_resumed"] is True
    assert provider.swap_calls == []


def test_kill_switch_rechecked_after_operator_approval(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    provider = FakeProvider()
    requested = _request(cfg, monkeypatch, provider)
    cfg.data["runtime"]["kill_switch"] = True

    approved = _approve(cfg, requested["approval_id"])

    assert approved["ok"] is True
    assert approved["resume"]["ok"] is False
    assert approved["resume"]["resume_response"]["error"] == "kill_switch_enabled"
    assert len(provider.quote_calls) == 1
    assert provider.swap_calls == []
    assert _db_approval(cfg, requested["approval_id"])["state"] == "resumed"


def test_expired_wallet_swap_cannot_be_approved(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    provider = FakeProvider()
    requested = _request(cfg, monkeypatch, provider)
    rows = jsonl.read_all(cfg.paths.approvals_pending)
    rows[0]["expires_at"] = time.time() - 1
    jsonl.write_all(cfg.paths.approvals_pending, rows)

    outcome = _approve(cfg, requested["approval_id"])

    assert outcome == {
        "ok": False,
        "error": "approval not found",
        "approval_id": requested["approval_id"],
    }
    assert provider.swap_calls == []


def test_wallet_swap_validation_fails_before_provider_construction(
    tmp_path,
    monkeypatch,
):
    cfg = _config(tmp_path)
    calls: list[bool] = []
    monkeypatch.setattr(
        swap_approval,
        "build_provider",
        lambda *_args, **_kwargs: calls.append(True),
    )

    result = _handler()(
        SimpleNamespace(config=cfg),
        _stamped(
            {
                **_payload(),
                "token_out": "SOL",
                "amount_in": -1,
            },
            "trade:live",
        ),
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_request"
    assert calls == []


def test_wallet_swap_is_registered_for_trusted_auth_stamping():
    assert "/wallet/swap" in local_server._TRUSTED_AUTH_PAYLOAD_PATHS


def test_wallet_swap_resume_payload_is_auditable_json(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    provider = FakeProvider()
    requested = _request(cfg, monkeypatch, provider)
    _approve(cfg, requested["approval_id"])

    row = _db_approval(cfg, requested["approval_id"])
    payload = json.loads(row["payload"])
    assert payload["resume_attempts"] == 1
    assert payload["resume_status"] == "executed"
    assert payload["resume_error"] is None
