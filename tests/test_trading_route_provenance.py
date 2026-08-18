from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import local_server, routes_trading
from nerya.api.auth import AuthResult
from nerya.core import jsonl, yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.trading.submit import submit_trade_intent


pytestmark = pytest.mark.smoke


def _submit_route():
    return next(
        handler
        for method, path, handler in routes_trading.routes()
        if method == "POST" and path == "/trading/submit"
    )


def _config(tmp_path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "paper_main",
                    "exchange": "mock",
                    "venue": "mock",
                    "mode": "paper",
                    "status": "active",
                    "initial_balance_usd": 10_000,
                    "permissions": {
                        "read_balances": True,
                        "place_order": True,
                        "cancel_order": True,
                    },
                }
            ]
        },
    )
    yaml_io.dump(
        cfg.paths.strategy("alpha") / "strategy.yml",
        {
            "id": "alpha",
            "status": "paper",
            "account_id": "paper_main",
            "markets": ["mock:BTC/USDT"],
            "paper_trading_enabled": True,
            "live_trading_enabled": False,
        },
    )
    yaml_io.dump(
        cfg.paths.strategy("alpha") / "limits.yml",
        {
            "allowed_markets": ["mock:BTC/USDT"],
            "min_confidence": 0,
            "max_stale_seconds": 60,
            "approval_threshold_usd": 10_000,
        },
    )
    return cfg


class _CapturingTrading:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def submit_intent(self, **payload):
        self.calls.append(dict(payload))
        return {"status": "pending_approval", "approval_id": "apr_http"}


class _PipelineTrading:
    def __init__(self, config: Config) -> None:
        self.config = config

    def submit_intent(self, **payload):
        return submit_trade_intent(
            self.config,
            spec=payload,
            market_snapshot={"price": 50_000, "age_s": 0, "source": "test"},
        )


def _stamped_spoofed_strategy_payload() -> dict:
    auth = AuthResult(
        ok=True,
        actor="token:operator",
        scope="trade:paper",
        scopes=frozenset({"trade:paper"}),
    )
    return local_server._stamp_trusted_auth(
        {
            "strategy_id": "alpha",
            "account_id": "paper_main",
            "market": "mock:BTC/USDT",
            "side": "buy",
            "size": 100.0,
            "size_unit": "usd",
            "order_type": "market",
            "confidence": 1.0,
            "source": "strategy_runtime",
            "meta": {
                "actor_id": "spoofed-actor",
                "custom_audit_field": "preserved",
            },
        },
        auth,
    )


def test_http_trade_submit_cannot_claim_strategy_runtime_source():
    payload = _stamped_spoofed_strategy_payload()
    original = deepcopy(payload)
    trading = _CapturingTrading()
    client = SimpleNamespace(trading=trading)

    result = _submit_route()(client, payload)

    assert result["status"] == "pending_approval"
    assert len(trading.calls) == 1
    submitted = trading.calls[0]
    assert submitted["source"] == "agent:native"
    assert submitted["meta"] == {
        "actor_id": "token:operator",
        "custom_audit_field": "preserved",
        "order_origin": "operator_http",
        "requested_actor_id": "spoofed-actor",
        "requested_source": "strategy_runtime",
    }
    assert "_auth_actor_id" not in submitted
    assert "_auth_scope" not in submitted
    assert "_auth_scopes" not in submitted
    assert payload == original


def test_http_trade_spoof_is_frozen_as_operator_approval_end_to_end(tmp_path):
    cfg = _config(tmp_path)
    client = SimpleNamespace(config=cfg, trading=_PipelineTrading(cfg))

    result = _submit_route()(client, _stamped_spoofed_strategy_payload())

    assert result["status"] == "pending_approval"
    assert result["order_id"] is None
    assert result["intent"]["source"] == "agent:native"
    assert "operator_agent_trade_approval_required" in result["risk_decision"]["reasons"]

    pending = jsonl.read_all(cfg.paths.approvals_pending)
    assert len(pending) == 1
    approval = pending[0]
    assert approval["approval_id"] == result["approval_id"]
    assert approval["actor_id"] == "token:operator"
    assert approval["execution_mode"] == "paper"
    assert approval["account_id"] == "paper_main"
    assert approval["strategy_id"] == "alpha"
    assert approval["market"] == "mock:BTC/USDT"
    assert approval["side"] == "buy"
    assert approval["order_type"] == "market"
    assert approval["size"] == pytest.approx(100.0)
    assert approval["intent"]["meta"]["requested_source"] == "strategy_runtime"
    assert jsonl.read_all(cfg.paths.approvals_approved) == []


def test_http_trade_submit_is_registered_for_dispatcher_auth_stamping():
    assert "/trading/submit" in local_server._TRUSTED_AUTH_PAYLOAD_PATHS


def test_direct_route_invocation_still_fails_closed_without_auth_stamp():
    trading = _CapturingTrading()
    client = SimpleNamespace(trading=trading)

    _submit_route()(
        client,
        {
            "strategy_id": "manual_agent",
            "account_id": "paper_main",
            "market": "mock:ETH/USDT",
            "side": "sell",
            "size": 50.0,
            "size_unit": "usd",
            "order_type": "market",
        },
    )

    submitted = trading.calls[0]
    assert submitted["source"] == "agent:native"
    assert submitted["meta"]["actor_id"] == "operator:http"
    assert submitted["meta"]["order_origin"] == "operator_http"
