from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.ops import preflight


pytestmark = pytest.mark.smoke


def _config(tmp_path, *, live: bool = False) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    data["runtime"]["live_trading_enabled"] = live
    return Config(paths=WorkspacePaths(root=tmp_path), data=data)


def test_llm_key_check_uses_route_provider_key_ref(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.data["llm"] = {
        "tiers": {
            "light": {
                "routes": [
                    {
                        "provider": "custom-provider",
                        "model": "model-1",
                        "provider_key_ref": "vault://llm_custom",
                    }
                ],
                "allowed_tasks": ["classify"],
            }
        }
    }
    monkeypatch.setattr(
        "nerya.llm.ops.provider_readiness",
        lambda _cfg: {
            "providers": [
                {
                    "provider": "custom-provider",
                    "ready": True,
                    "has_key_ref": True,
                }
            ]
        },
    )

    checks = preflight._check_llm_keys(cfg, "full_live")

    assert len(checks) == 1
    assert checks[0].status == "pass"
    assert "ready_routes=1/1" in checks[0].detail


def test_llm_smoke_uses_task_allowed_by_tier(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.data["llm"] = {
        "tiers": {
            "medium": {
                "provider": "custom-provider",
                "model": "model-1",
                "provider_key_ref": "vault://llm_custom",
                "allowed_tasks": ["strategy_review"],
            }
        }
    }
    calls = []

    class FakeGateway:
        def __init__(self, config):
            assert config is cfg

        def call(self, **kwargs):
            calls.append(dict(kwargs))
            return SimpleNamespace(
                raw="OK",
                parsed=None,
                provider="custom-provider",
            )

    monkeypatch.setattr("nerya.llm.gateway.LLMGateway", FakeGateway)

    checks = preflight._check_llm_provider_smoke(cfg, "full_live")

    assert checks[0].status == "pass"
    assert calls[0]["task"] == "strategy_review"
    assert calls[0]["tier"] == "medium"


def test_full_live_requires_global_switch_and_ready_account(tmp_path):
    cfg = _config(tmp_path, live=False)
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "live_main",
                    "exchange": "fake",
                    "venue": "fake",
                    "mode": "live",
                    "status": "active",
                    "live_trading_enabled": False,
                    "permissions": {
                        "read_balances": True,
                        "place_order": False,
                        "cancel_order": False,
                    },
                    "credentials": {
                        "api_key_ref": "vault://key",
                        "api_secret_ref": "vault://secret",
                    },
                }
            ]
        },
    )

    assert preflight._check_live_enabled(cfg, "full_live").status == "fail"
    account_check = preflight._check_live_accounts(cfg, "full_live")
    assert account_check.status == "fail"
    assert "place_order=false" in account_check.detail


def test_wallet_account_uses_snapshot_probe_not_cex_registry(tmp_path, monkeypatch):
    cfg = _config(tmp_path, live=True)
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "wallet_live",
                    "exchange": "byreal",
                    "venue": "byreal",
                    "provider_spec": "byreal",
                    "kind": "chain",
                    "wallet_id": "byreal_main",
                    "mode": "live",
                    "status": "active",
                    "live_trading_enabled": True,
                    "permissions": {
                        "read_balances": True,
                        "place_order": True,
                        "cancel_order": False,
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(
        "nerya.trading.account_snapshots.capture_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(
            health="ok",
            source="wallet",
            nav_usd=123.0,
            meta={},
        ),
    )
    monkeypatch.setattr(
        "nerya.connectors.registry.build_connector",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("wallet account must not use CEX connector registry")
        ),
    )

    checks = preflight._check_connector_reachability(cfg, "full_live")

    assert len(checks) == 1
    assert checks[0].status == "pass"
    assert "wallet snapshot ok" in checks[0].detail


def test_paper_connector_failure_is_warning_in_full_live(tmp_path, monkeypatch):
    cfg = _config(tmp_path, live=True)
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "yahoo_paper",
                    "exchange": "yahoo",
                    "venue": "yahoo",
                    "mode": "paper",
                    "status": "active",
                    "permissions": {"read_balances": True},
                }
            ]
        },
    )

    class BrokenConnector:
        def get_ticker(self, _market):
            raise RuntimeError("offline")

    monkeypatch.setattr(
        "nerya.connectors.registry.build_connector",
        lambda *_args, **_kwargs: BrokenConnector(),
    )

    checks = preflight._check_connector_reachability(cfg, "full_live")

    assert checks[0].status == "warn"
    assert "BTC-USD" in checks[0].detail


def test_chain_probe_prefers_get_slot_over_generic_ticker():
    class SolanaLike:
        def get_slot(self):
            return 123

        def get_ticker(self, _market):
            raise AssertionError("generic ticker must not be used for chain liveness")

    ok, detail = preflight._probe_connector_public(
        SolanaLike(),
        SimpleNamespace(venue="solana", provider_spec="solana", raw={}),
    )

    assert ok is True
    assert "get_slot() ok" in detail
