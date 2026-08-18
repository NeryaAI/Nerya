from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import local_server, route_scopes, routes_strategy, routes_trading
from nerya.api.auth import AuthResult
from nerya.connectors.registry import ConnectorRegistry
from nerya.core import jsonl, yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.sdk.trading_api import TradingAPI
from nerya.trading.access_control import guard_http_trade_scope
from nerya.trading.position_book import PositionBook


pytestmark = pytest.mark.smoke


def _account(
    account_id: str,
    mode: str,
    *,
    cancel_order: bool = True,
) -> dict:
    return {
        "id": account_id,
        "exchange": "mock",
        "venue": "mock",
        "kind": "cex",
        "mode": mode,
        "status": "active",
        "live_trading_enabled": mode in {"canary", "live"},
        "initial_balance_usd": 10_000,
        "permissions": {
            "read_balances": True,
            "place_order": True,
            "cancel_order": cancel_order,
            "withdraw": False,
        },
    }


def _config(tmp_path, *, live_cancel: bool = True) -> Config:
    cfg = Config(
        paths=WorkspacePaths(root=tmp_path),
        data=deepcopy(DEFAULT_CONFIG),
    )
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                _account("paper_main", "paper"),
                _account("canary_main", "canary"),
                _account("live_main", "live", cancel_order=live_cancel),
            ]
        },
    )
    return cfg


def _stamped(payload: dict, *scopes: str, actor: str = "token:operator") -> dict:
    auth = AuthResult(
        ok=True,
        actor=actor,
        scope=scopes[0] if scopes else "",
        scopes=frozenset(scopes),
    )
    return local_server._stamp_trusted_auth(payload, auth)


def _route(path: str):
    return next(
        handler
        for method, route_path, handler in routes_trading.routes()
        if method == "POST" and route_path == path
    )


class _CapturingTrading:
    def __init__(self) -> None:
        self.submit_calls: list[dict] = []
        self.cancel_calls: list[dict] = []

    def submit_intent(self, **payload):
        self.submit_calls.append(dict(payload))
        return {"status": "pending_approval", "approval_id": "apr_scope"}

    def cancel_order(self, **payload):
        self.cancel_calls.append(dict(payload))
        return {"ok": True, "result": {"status": "canceled"}}


def _submit_payload(account_id: str) -> dict:
    return {
        "strategy_id": "manual_agent",
        "account_id": account_id,
        "market": "mock:BTC/USDT",
        "side": "buy",
        "size": 100.0,
        "size_unit": "usd",
        "order_type": "market",
        "source": "strategy_runtime",
    }


def test_trade_route_matrix_accepts_either_scope_then_handlers_partition():
    for path in (
        "/trading/submit",
        "/trading/cancel",
        "/strategy/close_positions",
    ):
        assert route_scopes.authorize({"trade:paper"}, "POST", path) == (
            True,
            None,
        )
        assert route_scopes.authorize({"trade:live"}, "POST", path) == (
            True,
            None,
        )
        allowed, reason = route_scopes.authorize(
            {"read:runtime"}, "POST", path
        )
        assert allowed is False
        assert reason == "insufficient_scope:needed_any=trade:paper|trade:live"

    assert route_scopes.authorize(
        {"trade:live"}, "POST", "/wallet/swap"
    ) == (True, None)
    assert route_scopes.authorize(
        {"trade:paper"}, "POST", "/wallet/swap"
    ) == (False, "insufficient_scope:needed=trade:live")


def test_account_scope_guard_maps_canary_and_live_to_trade_live(tmp_path):
    cfg = _config(tmp_path)

    assert guard_http_trade_scope(
        cfg,
        _stamped({}, "trade:paper"),
        account_id="paper_main",
        action="submit_order",
    ) is None
    for account_id, mode in (("canary_main", "canary"), ("live_main", "live")):
        denied = guard_http_trade_scope(
            cfg,
            _stamped({}, "trade:paper"),
            account_id=account_id,
            action="submit_order",
        )
        assert denied is not None
        assert denied["error"] == "insufficient_trade_scope"
        assert denied["required_scope"] == "trade:live"
        assert denied["account_mode"] == mode
        assert guard_http_trade_scope(
            cfg,
            _stamped({}, "trade:live"),
            account_id=account_id,
            action="submit_order",
        ) is None

    assert guard_http_trade_scope(
        cfg,
        _stamped({}, "api:all"),
        account_id="live_main",
        action="submit_order",
    ) is None
    # No dispatcher stamp means a trusted in-process caller, not public HTTP.
    assert guard_http_trade_scope(
        cfg,
        {"_auth_scopes": ["trade:paper"]},
        account_id="live_main",
        action="submit_order",
    ) is None


def test_paper_token_cannot_submit_to_live_account(tmp_path):
    cfg = _config(tmp_path)
    trading = _CapturingTrading()
    client = SimpleNamespace(config=cfg, trading=trading)

    result = _route("/trading/submit")(
        client,
        _stamped(_submit_payload("live_main"), "trade:paper"),
    )

    assert result["status"] == "rejected"
    assert result["error"] == "insufficient_trade_scope"
    assert result["required_scope"] == "trade:live"
    assert trading.submit_calls == []


def test_live_token_can_submit_live_account_but_still_enters_human_approval(tmp_path):
    cfg = _config(tmp_path)
    trading = _CapturingTrading()
    client = SimpleNamespace(config=cfg, trading=trading)

    result = _route("/trading/submit")(
        client,
        _stamped(
            {
                **_submit_payload("live_main"),
                "meta": {"actor_id": "spoofed"},
            },
            "trade:live",
        ),
    )

    assert result["status"] == "pending_approval"
    assert len(trading.submit_calls) == 1
    submitted = trading.submit_calls[0]
    assert submitted["source"] == "agent:native"
    assert submitted["meta"]["actor_id"] == "token:operator"
    assert submitted["meta"]["requested_source"] == "strategy_runtime"
    assert submitted["meta"]["requested_actor_id"] == "spoofed"


def test_cancel_route_forwards_only_dispatcher_stamped_auth_context(tmp_path):
    cfg = _config(tmp_path)
    trading = _CapturingTrading()
    client = SimpleNamespace(config=cfg, trading=trading)
    payload = _stamped(
        {"strategy_id": "alpha", "order_id": "ord-1"},
        "trade:live",
    )

    result = _route("/trading/cancel")(client, payload)

    assert result["ok"] is True
    assert trading.cancel_calls == [
        {
            "strategy_id": "alpha",
            "order_id": "ord-1",
            "auth_context": payload,
        }
    ]


def _write_open_order(cfg: Config, *, account_id: str = "live_main") -> None:
    history = cfg.paths.strategy_history("alpha")
    jsonl.append(
        history / "orders.jsonl",
        {
            "session_id": "session-1",
            "payload": {
                "order_id": "ord-live",
                "intent_id": "intent-live",
                "status": "accepted",
            },
        },
    )
    jsonl.append(
        history / "intents.jsonl",
        {
            "session_id": "session-1",
            "intent": {
                "intent_id": "intent-live",
                "account_id": account_id,
                "market": "mock:BTC/USDT",
            },
        },
    )


class _CancelConnector:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def cancel_order(self, **payload):
        self.calls.append(dict(payload))
        return {"status": "canceled", "order_id": payload["order_id"]}


def test_sdk_cancel_rechecks_live_scope_permission_and_connector_mode(
    tmp_path,
    monkeypatch,
):
    cfg = _config(tmp_path)
    _write_open_order(cfg)
    connector = _CancelConnector()
    connector_cfgs: list[dict] = []

    def fake_get(_self, _account_id, account_cfg):
        connector_cfgs.append(dict(account_cfg))
        return connector

    monkeypatch.setattr(ConnectorRegistry, "get", fake_get)
    api = TradingAPI(config=cfg, skills=SimpleNamespace())

    denied = api.cancel_order(
        strategy_id="alpha",
        order_id="ord-live",
        auth_context=_stamped({}, "trade:paper"),
    )
    assert denied["error"] == "insufficient_trade_scope"
    assert denied["required_scope"] == "trade:live"
    assert connector.calls == []

    allowed = api.cancel_order(
        strategy_id="alpha",
        order_id="ord-live",
        auth_context=_stamped({}, "trade:live"),
    )
    assert allowed["ok"] is True
    assert connector.calls == [
        {"market": "mock:BTC/USDT", "order_id": "ord-live"}
    ]
    assert connector_cfgs[0]["live"] is True


def test_sdk_cancel_honors_account_cancel_permission(tmp_path, monkeypatch):
    cfg = _config(tmp_path, live_cancel=False)
    _write_open_order(cfg)
    calls: list[dict] = []
    monkeypatch.setattr(
        ConnectorRegistry,
        "get",
        lambda _self, _account_id, account_cfg: calls.append(account_cfg),
    )
    api = TradingAPI(config=cfg, skills=SimpleNamespace())

    result = api.cancel_order(
        strategy_id="alpha",
        order_id="ord-live",
        auth_context=_stamped({}, "trade:live"),
    )

    assert result["error"] == "cancel_order_disabled"
    assert calls == []


def test_live_close_batch_is_rejected_before_any_submit_for_paper_token(tmp_path):
    cfg = _config(tmp_path)
    book = PositionBook(cfg.paths)
    book.apply_fill(
        account_id="live_main",
        strategy_id="alpha",
        market="mock:BTC/USDT",
        side="buy",
        price=50_000,
        size_base=0.01,
        source="live",
    )
    client = SimpleNamespace(config=cfg)

    result = routes_strategy._close_strategy_positions(
        client,
        _stamped(
            {"strategy_id": "alpha", "operator": "spoofed-operator"},
            "trade:paper",
        ),
    )

    assert result["error"] == "insufficient_trade_scope"
    assert result["required_scope"] == "trade:live"
    assert len(book.open_positions(strategy_id="alpha")) == 1


def test_close_batch_binds_operator_to_dispatcher_identity(
    tmp_path,
    monkeypatch,
):
    cfg = _config(tmp_path)
    book = PositionBook(cfg.paths)
    book.apply_fill(
        account_id="paper_main",
        strategy_id="alpha",
        market="mock:BTC/USDT",
        side="buy",
        price=50_000,
        size_base=0.01,
        source="paper",
    )
    plans = []

    def fake_submit(_cfg, plan, **_kwargs):
        plans.append(plan)
        return {"status": "filled", "order_id": "ord-close"}

    monkeypatch.setattr(routes_strategy, "submit_trade_plan", fake_submit)

    result = routes_strategy._close_strategy_positions(
        SimpleNamespace(config=cfg),
        _stamped(
            {"strategy_id": "alpha", "operator": "spoofed-operator"},
            "trade:paper",
            actor="token:verified-operator",
        ),
    )

    assert result["ok"] is True
    assert len(plans) == 1
    assert plans[0].meta["actor_id"] == "token:verified-operator"
    assert plans[0].meta["operator"] == "token:verified-operator"
    assert plans[0].meta["requested_operator"] == "spoofed-operator"
    assert plans[0].meta["order_origin"] == "operator_http"


def test_dynamic_trade_routes_are_registered_for_auth_stamping():
    assert {
        "/strategy/close_positions",
        "/trading/cancel",
        "/trading/submit",
    }.issubset(local_server._TRUSTED_AUTH_PAYLOAD_PATHS)
