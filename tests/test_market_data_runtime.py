from __future__ import annotations

from copy import deepcopy
import os
from types import SimpleNamespace

import pytest

from nerya.agent.kernel import AgentKernel as _AgentKernel  # noqa: F401
from nerya.connectors.mock_exchange import MockExchange
from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.data.candles import discover_market_data_sources, fetch_candles, fetch_public_ticker
from nerya.skills.kernel import SkillKernel
from nerya.strategies.context import StrategyMarket, StrategyPnL, StrategyPortfolio
from nerya.strategies.runner import StrategyRunner
from nerya.subagents.runtime import SubAgentRuntime
from nerya.tools.native.connectors import market_data_handler
from nerya.tools.registry import ToolRegistry, make_native_descriptor
from nerya.tools.types import PermissionScope, RiskLevel, ToolCall
from nerya.trading.virtual_ledger import open_ledger


pytestmark = pytest.mark.smoke
REAL_MARKET_TESTS = os.environ.get("NERYA_REAL_MARKET_TESTS", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
REAL_MARKET_CANDIDATES = (
    "BINANCE:BTCUSDT",
    "OKX:BTCUSDT",
    "KRAKEN:BTCUSD",
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
                    "permissions": {"read_balances": True},
                }
            ]
        },
    )
    return cfg


def _json_payload(result) -> dict:
    body = result.asdict()
    assert body["is_error"] is False
    parts = body["content"]
    assert parts and parts[0]["type"] == "json"
    return parts[0]["data"]


def _first_live_candles(cfg: Config, *, count: int) -> tuple[str, list[dict]]:
    for market in REAL_MARKET_CANDIDATES:
        rows = fetch_candles(
            market,
            count=count,
            interval="1m",
            allow_mock=False,
            config_like=cfg,
        )
        if rows and rows[0].get("_envelope", {}).get("mode") == "live":
            return market, rows
    return REAL_MARKET_CANDIDATES[0], []


def test_mock_exchange_provides_paper_ohlcv_klines() -> None:
    rows = MockExchange().get_klines("mock:BTC/USDT", interval="5m", limit=12)

    assert len(rows) == 12
    assert all(len(row) == 6 for row in rows)
    assert rows[-1][0] > rows[0][0]
    assert rows[-1][4] > 0


def test_strategy_market_features_include_indicators(tmp_path) -> None:
    cfg = _config(tmp_path)
    market = StrategyMarket(
        paths=cfg.paths,
        accounts=("paper_main",),
        _registry_factory=lambda: __import__(
            "nerya.connectors.registry",
            fromlist=["ConnectorRegistry"],
        ).ConnectorRegistry(cfg.paths.root),
    )

    candles = market.candles("mock:BTC/USDT", timeframe="1m", limit=40)
    klines = market.klines("mock:BTC/USDT", timeframe="1m", limit=40)
    features = market.features("mock:BTC/USDT", timeframe="1m", lookback=40)

    assert len(candles) == 40
    assert len(klines) == 40
    assert features["rows"] == 40
    assert features["rsi_14"] is not None
    assert features["macd"]["hist"] is not None
    assert features["indicator_backend"] in {"pure_python", "talib"}


def test_strategy_market_ticker_uses_live_public_path_for_prefixed_markets(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = _config(tmp_path)

    def fake_public_ticker(market, *, allow_mock=None, config_like=None):
        assert market == "yahoo:AAPL"
        assert allow_mock is False
        return {
            "price": 293.32,
            "last": 293.32,
            "mid": 293.32,
            "bid": 293.31,
            "ask": 293.33,
            "spread_bps": 0.68,
            "ts_ms": 1778247000000,
            "source": "yahoo_rest",
            "_envelope": {
                "mode": "live",
                "source": "yahoo_rest",
                "venue": "yahoo",
            },
        }

    class BadRegistry:
        def get(self, *_args, **_kwargs):
            raise AssertionError("non-mock ticker should not use account connector")

    monkeypatch.setattr("nerya.strategies.context.fetch_public_ticker", fake_public_ticker)
    market = StrategyMarket(
        paths=cfg.paths,
        accounts=("paper_main",),
        _registry_factory=lambda: BadRegistry(),
    )

    ticker = market.ticker("yahoo:AAPL")

    assert ticker["last"] == 293.32
    assert ticker["venue"] == "yahoo"
    assert ticker["_envelope"]["mode"] == "live"


def test_native_market_data_returns_candles_and_features(tmp_path) -> None:
    cfg = _config(tmp_path)

    candles = _json_payload(
        market_data_handler(
            ToolCall(
                name="market_data",
                arguments={
                    "action": "get_candles",
                    "market": "mock:BTC/USDT",
                    "interval": "1m",
                    "count": 20,
                },
            ),
            config_like=cfg,
        )
    )
    features = _json_payload(
        market_data_handler(
            ToolCall(
                name="market_data",
                arguments={
                    "action": "calculate_features",
                    "market": "mock:BTC/USDT",
                    "interval": "1m",
                    "count": 40,
                },
            ),
            config_like=cfg,
        )
    )

    assert candles["count"] == 20
    assert len(candles["candles"]) == 20
    assert features["count"] == 40
    assert features["features"]["rsi_14"] is not None
    assert features["features"]["macd"]["hist"] is not None


def test_native_market_data_requires_explicit_market(tmp_path) -> None:
    cfg = _config(tmp_path)

    result = market_data_handler(
        ToolCall(
            name="market_data",
            arguments={"action": "get_ticker"},
        ),
        config_like=cfg,
    )

    assert result.is_error
    assert "market or symbol is required" in result.asdict()["content"][0]["text"]


def test_market_data_sources_are_discovered_from_workspace_config(tmp_path) -> None:
    cfg = _config(tmp_path)
    cfg.data.setdefault("workspace_preferences", {})["market_defaults"] = {
        "venue": "okx",
        "preferred_venues": ["okx", "bybit"],
    }
    yaml_io.dump(
        cfg.paths.exchanges_file,
        {"version": 1, "exchanges": {"kraken": {"venue": "kraken"}}},
    )

    sources = discover_market_data_sources(cfg)
    venues = [row["canonical"] for row in sources]

    assert venues[:2] == ["OKX", "BYBIT"]
    assert "KRAKEN" in venues
    assert "BINANCE" in venues


def test_wallet_market_data_source_is_discovered_from_okx_binding(tmp_path) -> None:
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "providers": {
            "okx_main": {
                "provider": "okx_os",
                "label": "OKX Web3",
                "config": {},
            }
        }
    }

    sources = discover_market_data_sources(cfg)
    venues = [row["canonical"] for row in sources]

    assert "OKX_ONCHAIN" in venues


def test_wallet_market_data_sources_are_discovered_for_all_supported_wallets(tmp_path) -> None:
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "providers": {
            "okx_main": {"provider": "okx_os", "config": {}},
            "bitget_main": {"provider": "bitget", "config": {}},
            "binance_web3_main": {"provider": "binance_agentic", "config": {}},
            "coinbase_main": {"provider": "coinbase", "config": {}},
            "xagt_main": {"provider": "xagt_agent_plugin", "config": {}},
            "self_main": {"provider": "self_custody", "config": {}},
        }
    }

    sources = discover_market_data_sources(cfg)
    venues = {row["canonical"] for row in sources}

    assert {
        "OKX_ONCHAIN",
        "BITGET_ONCHAIN",
        "BINANCE_ALPHA",
        "COINBASE_WALLET",
        "XAGT_ONCHAIN",
    } <= venues
    assert "SELF_CUSTODY_ONCHAIN" not in venues


def test_wallet_provider_catalog_declares_verified_login_flows() -> None:
    from nerya.wallet import list_providers

    providers = {row["id"]: row for row in list_providers()}

    okx_flows = {row["id"]: row for row in providers["okx_os"]["auth_flows"]}
    assert okx_flows["okx_email_otp"]["kind"] == "email_otp"
    assert all(row["kind"] != "advanced_api_key" for row in providers["okx_os"]["auth_flows"])
    assert providers["okx_os"]["install_command"].startswith("github-release-bin:")
    assert "onchainos wallet login <email>" in okx_flows["okx_email_otp"]["commands"]
    assert "onchainos wallet verify <code>" in okx_flows["okx_email_otp"]["commands"]
    okx_fields = {row["name"]: row for row in providers["okx_os"]["credential_fields"]}
    assert okx_fields["account_id"]["required"] is False
    assert "api_key" not in okx_fields
    okx_advanced_fields = {
        row["name"]: row for row in providers["okx_os"]["advanced_credential_fields"]
    }
    assert okx_advanced_fields["api_key"]["required"] is False

    bitget_flows = {row["id"]: row for row in providers["bitget"]["auth_flows"]}
    assert bitget_flows["bitget_wallet_skill"]["kind"] == "skill_builtin_token"
    assert all(row["kind"] != "advanced_api_key" for row in providers["bitget"]["auth_flows"])
    assert "https://github.com/bitget-wallet-ai-lab/bitget-wallet-skill" in providers["bitget"]["links"]["docs"]
    bitget_default_fields = {row["name"] for row in providers["bitget"]["credential_fields"]}
    assert "market_api_key" not in bitget_default_fields

    binance_flows = {row["id"]: row for row in providers["binance_agentic"]["auth_flows"]}
    assert binance_flows["binance_app_qr"]["kind"] == "app_qr"
    assert providers["binance_agentic"]["install_command"].startswith("npm:@binance/agentic-wallet")
    assert "baw auth signin --json" in binance_flows["binance_app_qr"]["commands"]
    assert any("baw auth verify --qrCodeId" in cmd for cmd in binance_flows["binance_app_qr"]["commands"])

    coinbase_flows = {row["id"]: row for row in providers["coinbase"]["auth_flows"]}
    assert coinbase_flows["coinbase_email_otp"]["kind"] == "email_otp"
    assert all(row["kind"] != "advanced_api_key" for row in providers["coinbase"]["auth_flows"])
    assert providers["coinbase"]["install_command"].startswith("npm:awal#version=2.10.0")
    assert "npx awal@2.10.0 auth login <email> --json" in coinbase_flows["coinbase_email_otp"]["commands"]
    assert "npx awal@2.10.0 auth verify <otp> --json" in coinbase_flows["coinbase_email_otp"]["commands"]

    xagt_flows = {row["id"]: row for row in providers["xagt_agent_plugin"]["auth_flows"]}
    assert xagt_flows["xagt_device_login"]["kind"] == "device_code"
    assert providers["xagt_agent_plugin"]["install_command"] == (
        "npm:@xagt/agent-plugin#version=0.4.0&entry=dist/cli.js"
    )
    assert "xagt-plugin login --no-browser" in xagt_flows["xagt_device_login"]["commands"]
    xagt_sources = {
        row["canonical"] for row in providers["xagt_agent_plugin"]["market_data_sources"]
    }
    assert "XAGT_ONCHAIN" in xagt_sources


def test_wallet_credential_schema_returns_auth_flows(tmp_path) -> None:
    from nerya.api.routes_wallet import routes as wallet_routes

    cfg = _config(tmp_path)
    handler = dict(((method, path), fn) for method, path, fn in wallet_routes())[
        ("POST", "/wallet/credential_schema")
    ]
    client = SimpleNamespace(config=cfg)

    res = handler(client, {"provider": "okx_os"})

    assert res["ok"] is True
    assert res["auth_flows"][0]["id"] == "okx_email_otp"
    assert "onchainos wallet login <email>" in res["auth_flows"][0]["commands"]
    assert {row["name"] for row in res["credential_fields"]} == {
        "account_id",
        "api_project_id",
    }
    assert "api_key" in {row["name"] for row in res["advanced_credential_fields"]}


def test_wallet_auth_start_installs_and_returns_binance_qr(
    tmp_path,
    monkeypatch,
) -> None:
    from nerya.api import routes_wallet
    from nerya.api.routes_wallet import routes as wallet_routes
    from nerya.install.dep_installer import InstallResult

    cfg = _config(tmp_path)
    handler = dict(((method, path), fn) for method, path, fn in wallet_routes())[
        ("POST", "/wallet/auth/start")
    ]
    client = SimpleNamespace(config=cfg)

    def fake_install(paths, command, *, config_data=None, approve=False):
        assert command.startswith("npm:@binance/agentic-wallet")
        assert approve is True
        return InstallResult(
            ok=True,
            kind="npm",
            target="@binance/agentic-wallet@1.0.9",
            command="npm install",
            duration_s=0.01,
            install_path=str(tmp_path / "skills" / "_node" / "binance"),
            extra={"entry": "dist/index.js"},
        )

    def fake_run_cli(client_arg, provider, args, *, timeout_s=180.0):
        assert provider == "binance_agentic"
        assert args == ["auth", "signin", "--json"]
        return {
            "ok": True,
            "provider": provider,
            "return_code": 0,
            "json": {
                "success": True,
                "data": {
                    "qrCodeId": "qr-123",
                    "urlForWeb": "https://www.binance.com/web3/auth/qr-123",
                    "pairingCode": "ABCD",
                },
            },
        }

    monkeypatch.setattr(routes_wallet, "run_install", fake_install)
    monkeypatch.setattr(routes_wallet, "_run_wallet_cli", fake_run_cli)

    res = handler(client, {"provider": "binance_agentic", "approve": True})

    assert res["ok"] is True
    assert res["next_action"] == "qr_approval"
    assert res["auth"]["json"]["data"]["qrCodeId"] == "qr-123"
    assert res["install"]["kind"] == "npm"


def test_wallet_auth_start_binance_qr_creates_binding_for_market_data(
    tmp_path,
    monkeypatch,
) -> None:
    from nerya.api import routes_wallet
    from nerya.api.routes_wallet import routes as wallet_routes

    cfg = _config(tmp_path)
    handler = dict(((method, path), fn) for method, path, fn in wallet_routes())[
        ("POST", "/wallet/auth/start")
    ]
    client = SimpleNamespace(config=cfg)

    def fake_run_cli(client_arg, provider, args, *, timeout_s=180.0):
        assert provider == "binance_agentic"
        return {
            "ok": True,
            "provider": provider,
            "return_code": 0,
            "json": {
                "success": True,
                "data": {
                    "qrCodeId": "qr-123",
                    "urlForWeb": "https://www.binance.com/web3/auth/qr-123",
                },
            },
        }

    monkeypatch.setattr(routes_wallet, "_run_wallet_cli", fake_run_cli)

    res = handler(
        client,
        {
            "provider": "binance_agentic",
            "install": False,
            "wallet_id": "binance_agentic_main",
            "label": "Binance Agentic Wallet",
            "create_binding": True,
        },
    )

    assert res["ok"] is True
    assert res["binding"]["ok"] is True
    binding = cfg.data["wallet"]["providers"]["binance_agentic_main"]
    assert binding["provider"] == "binance_agentic"


def test_wallet_auth_start_xagt_device_login_returns_url_and_pending_binding(
    tmp_path,
    monkeypatch,
) -> None:
    from nerya.api import routes_wallet
    from nerya.api.routes_wallet import routes as wallet_routes

    cfg = _config(tmp_path)
    handler = dict(((method, path), fn) for method, path, fn in wallet_routes())[
        ("POST", "/wallet/auth/start")
    ]
    client = SimpleNamespace(config=cfg)
    pkg_root = tmp_path / "skills" / "_node" / "xagt__agent-plugin" / "node_modules" / "@xagt" / "agent-plugin"

    def fake_install_for_auth(client_arg, provider, *, approve):
        assert provider == "xagt_agent_plugin"
        assert approve is True
        return {
            "ok": True,
            "kind": "npm",
            "target": "@xagt/agent-plugin@0.4.0",
            "command": "npm install @xagt/agent-plugin@0.4.0",
            "duration_s": 0.01,
            "install_path": str(pkg_root),
            "extra": {"entry": "dist/cli.js", "package": "@xagt/agent-plugin", "version": "0.4.0"},
        }

    def fake_http_post_json(url, payload, **_kwargs):
        assert url == "https://api.xerpaai.com/xagent/plugin/cli/auth/device"
        assert payload["clientName"] == "xagt-plugin"
        return {
            "data": {
                "deviceCode": "dev-123",
                "userCode": "USER-123",
                "expiresIn": 600,
                "interval": 5,
            }
        }

    monkeypatch.setattr(routes_wallet, "_install_for_auth", fake_install_for_auth)
    monkeypatch.setattr(routes_wallet, "_http_post_json", fake_http_post_json)

    res = handler(
        client,
        {
            "provider": "xagt_agent_plugin",
            "approve": True,
            "wallet_id": "xagt_main",
            "label": "XAgent OKX",
            "create_binding": True,
        },
    )

    assert res["ok"] is True
    assert res["next_action"] == "device_approval"
    assert res["required_inputs"] == ["deviceCode"]
    assert res["auth"]["json"]["deviceCode"] == "dev-123"
    assert "userAuth" in res["auth"]["json"]["verificationUrl"]
    binding = cfg.data["wallet"]["providers"]["xagt_main"]
    assert binding["provider"] == "xagt_agent_plugin"
    assert binding["config"]["plugin_path"] == str(pkg_root)
    assert binding["config"]["login_pending"] == "true"


def test_wallet_auth_verify_xagt_vaults_tokens_and_creates_binding(
    tmp_path,
    monkeypatch,
) -> None:
    from nerya.api import routes_wallet
    from nerya.api.routes_wallet import routes as wallet_routes
    from nerya.security.secrets import SecretVault

    cfg = _config(tmp_path)
    handler = dict(((method, path), fn) for method, path, fn in wallet_routes())[
        ("POST", "/wallet/auth/verify")
    ]
    client = SimpleNamespace(config=cfg)

    def fake_http_post_json(url, payload, **_kwargs):
        assert url == "https://api.xerpaai.com/xagent/plugin/cli/auth/token"
        assert payload == {"deviceCode": "dev-123"}
        return {
            "data": {
                "accessToken": "access-secret",
                "refreshToken": "refresh-secret",
                "userId": "user-1",
                "accessExpire": 1778700000,
                "scope": "wallet market",
            }
        }

    monkeypatch.setattr(routes_wallet, "_http_post_json", fake_http_post_json)

    res = handler(
        client,
        {
            "provider": "xagt_agent_plugin",
            "deviceCode": "dev-123",
            "wallet_id": "xagt_main",
            "label": "XAgent OKX",
            "create_binding": True,
        },
    )

    assert res["ok"] is True
    assert res["account"]["access_token"] == "***"
    assert res["account"]["refresh_token"] == "***"
    binding = cfg.data["wallet"]["providers"]["xagt_main"]
    saved_cfg = binding["config"]
    assert saved_cfg["user_id"] == "user-1"
    assert saved_cfg["access_token_ref"] == "vault://wallet_xagt_main_access_token"
    assert saved_cfg["refresh_token_ref"] == "vault://wallet_xagt_main_refresh_token"
    assert "access_token" not in saved_cfg
    assert "refresh_token" not in saved_cfg
    vault = SecretVault.open(cfg.paths.vault_enc)
    assert vault.resolve("wallet_xagt_main_access_token", required_scope="wallet") == "access-secret"
    assert vault.resolve("wallet_xagt_main_refresh_token", required_scope="wallet") == "refresh-secret"


def test_wallet_auth_verify_uses_coinbase_awal_otp(
    tmp_path,
    monkeypatch,
) -> None:
    from nerya.api import routes_wallet
    from nerya.api.routes_wallet import routes as wallet_routes

    cfg = _config(tmp_path)
    handler = dict(((method, path), fn) for method, path, fn in wallet_routes())[
        ("POST", "/wallet/auth/verify")
    ]
    client = SimpleNamespace(config=cfg)

    def fake_run_cli(client_arg, provider, args, *, timeout_s=300.0):
        assert provider == "coinbase"
        assert args == ["auth", "verify", "123456", "--json"]
        return {"ok": True, "provider": provider, "return_code": 0, "json": {"success": True}}

    monkeypatch.setattr(routes_wallet, "_run_wallet_cli", fake_run_cli)

    res = handler(client, {"provider": "coinbase", "otp": "123456"})

    assert res["ok"] is True
    assert res["auth"]["json"]["success"] is True


def test_wallet_auth_start_coinbase_returns_popup_when_window_runs(
    tmp_path,
    monkeypatch,
) -> None:
    from nerya.api import routes_wallet
    from nerya.api.routes_wallet import routes as wallet_routes

    cfg = _config(tmp_path)
    handler = dict(((method, path), fn) for method, path, fn in wallet_routes())[
        ("POST", "/wallet/auth/start")
    ]
    client = SimpleNamespace(config=cfg)

    def fake_start_background(client_arg, provider, args, *, timeout_s=25.0):
        assert provider == "coinbase"
        assert args == ["auth", "login", "user@example.com", "--json"]
        assert timeout_s == 25.0
        return {
            "ok": True,
            "provider": provider,
            "return_code": None,
            "json": {
                "loginMethod": "email_popup",
                "walletWindow": {
                    "running": True,
                    "pid": 1234,
                    "lock_file": "C:\\tmp\\payments-mcp-ui.lock",
                },
            },
            "note": "coinbase_wallet_popup_started",
        }

    monkeypatch.setattr(routes_wallet, "_start_wallet_cli_background", fake_start_background)

    res = handler(
        client,
        {
            "provider": "coinbase",
            "install": False,
            "email": "user@example.com",
        },
    )

    assert res["ok"] is True
    assert res["next_action"] == "wallet_popup_login"
    assert res["required_inputs"] == ["email_link"]
    assert res["auth"]["json"]["loginMethod"] == "email_popup"
    assert res["auth"]["json"]["walletWindow"]["running"] is True


def test_wallet_auth_status_coinbase_uses_window_state_and_creates_binding(
    tmp_path,
    monkeypatch,
) -> None:
    from nerya.api import routes_wallet
    from nerya.api.routes_wallet import routes as wallet_routes

    cfg = _config(tmp_path)
    handler = dict(((method, path), fn) for method, path, fn in wallet_routes())[
        ("POST", "/wallet/auth/status")
    ]
    client = SimpleNamespace(config=cfg)

    monkeypatch.setattr(
        routes_wallet,
        "_coinbase_status_result",
        lambda: {
            "ok": True,
            "provider": "coinbase",
            "return_code": None,
            "json": {
                "walletWindow": {"running": True, "pid": 1234},
                "agenticSessionPath": str(tmp_path / "Electron"),
                "agenticSessionExists": True,
                "loginMethod": "email_popup",
            },
        },
    )
    monkeypatch.setattr(
        routes_wallet,
        "_coinbase_awal_session_path",
        lambda: tmp_path / "Electron",
    )

    res = handler(
        client,
        {
            "provider": "coinbase",
            "wallet_id": "coinbase_main",
            "label": "Coinbase Agentic Wallet",
            "create_binding": True,
        },
    )

    assert res["ok"] is True
    assert res["binding"]["ok"] is True
    binding = cfg.data["wallet"]["providers"]["coinbase_main"]
    assert binding["provider"] == "coinbase"
    assert binding["config"]["agentic_session_path"] == str(tmp_path / "Electron")


def test_coinbase_awal_windows_patch_adds_proxy_shell_and_visible_popup(
    tmp_path,
    monkeypatch,
) -> None:
    from nerya.api import routes_wallet

    cfg = _config(tmp_path)
    root = cfg.paths.root / "skills" / "_node" / "awal" / "node_modules" / "awal"
    manager = root / "dist" / "utils" / "serverManager.js"
    manager.parent.mkdir(parents=True)
    manager.write_text(
        "const child = spawn(electronBin, [bundleElectron], {\n"
        "        detached: true,\n"
        "        stdio: 'ignore',\n"
        "        env: {\n"
        "            ...process.env,\n"
        "        },\n"
        "    });\n",
        encoding="utf-8",
    )
    bundle = root / "server-bundle" / "bundle-electron.js"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("new sm({show:!1,width:500,height:600});", encoding="utf-8")
    client = SimpleNamespace(config=cfg)

    monkeypatch.setattr(routes_wallet.os, "name", "nt", raising=False)

    routes_wallet._patch_coinbase_awal_windows(client)

    patched = manager.read_text(encoding="utf-8")
    assert "AWAL_ELECTRON_PROXY_SERVER" in patched
    assert "const electronArgs = proxyServer" in patched
    assert "shell: process.platform === 'win32'" in patched
    assert "windowsHide: true" in patched
    assert "proxy-bypass-list" not in patched
    assert "new sm({show:!0,width:500" in bundle.read_text(encoding="utf-8")


def test_wallet_auth_verify_creates_okx_account_and_binding(
    tmp_path,
    monkeypatch,
) -> None:
    from nerya.api import routes_wallet
    from nerya.api.routes_wallet import routes as wallet_routes

    cfg = _config(tmp_path)
    handler = dict(((method, path), fn) for method, path, fn in wallet_routes())[
        ("POST", "/wallet/auth/verify")
    ]
    client = SimpleNamespace(config=cfg)
    calls: list[list[str]] = []

    def fake_run_cli(client_arg, provider, args, *, timeout_s=300.0):
        assert provider == "okx_os"
        calls.append(args)
        if args == ["wallet", "verify", "123456"]:
            return {"ok": True, "provider": provider, "return_code": 0, "json": {"ok": True, "data": {}}}
        if args == ["wallet", "status"] and len(calls) == 2:
            return {
                "ok": True,
                "provider": provider,
                "return_code": 0,
                "json": {"ok": True, "data": {"loggedIn": True, "accountCount": 0}},
            }
        if args == ["wallet", "add"]:
            return {
                "ok": True,
                "provider": provider,
                "return_code": 0,
                "json": {"ok": True, "data": {"accountId": "okx-account-1", "accountName": "Wallet 1"}},
            }
        if args == ["wallet", "status"]:
            return {
                "ok": True,
                "provider": provider,
                "return_code": 0,
                "json": {
                    "ok": True,
                    "data": {
                        "loggedIn": True,
                        "currentAccountId": "okx-account-1",
                        "currentAccountName": "Wallet 1",
                        "accountCount": 1,
                    },
                },
            }
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(routes_wallet, "_run_wallet_cli", fake_run_cli)

    res = handler(
        client,
        {
            "provider": "okx_os",
            "otp": "123456",
            "wallet_id": "okx_os_main",
            "label": "OKX Wallet",
            "create_binding": True,
        },
    )

    assert res["ok"] is True
    assert res["account"]["created"] is True
    assert res["binding"]["ok"] is True
    binding = cfg.data["wallet"]["providers"]["okx_os_main"]
    assert binding["provider"] == "okx_os"
    assert binding["config"]["account_id"] == "okx-account-1"
    assert calls == [
        ["wallet", "verify", "123456"],
        ["wallet", "status"],
        ["wallet", "add"],
        ["wallet", "status"],
    ]


def test_wallet_auth_start_no_login_provider_creates_binding(
    tmp_path,
    monkeypatch,
) -> None:
    from nerya.api import routes_wallet
    from nerya.api.routes_wallet import routes as wallet_routes

    cfg = _config(tmp_path)
    handler = dict(((method, path), fn) for method, path, fn in wallet_routes())[
        ("POST", "/wallet/auth/start")
    ]
    client = SimpleNamespace(config=cfg)

    def fake_install_for_auth(client_arg, provider, *, approve):
        assert provider == "bitget"
        assert approve is True
        return {
            "ok": True,
            "kind": "git-repo",
            "target": "https://github.com/bitget-wallet-ai-lab/bitget-wallet-skill",
            "install_path": str(tmp_path / "skills" / "_node" / "bitget-wallet-skill"),
            "extra": {"entry": "scripts/bitget-wallet-agent-api.py"},
        }

    monkeypatch.setattr(routes_wallet, "_install_for_auth", fake_install_for_auth)

    res = handler(
        client,
        {
            "provider": "bitget",
            "approve": True,
            "wallet_id": "bitget_main",
            "label": "Bitget Wallet",
            "create_binding": True,
        },
    )

    assert res["ok"] is True
    assert res["next_action"] == "no_login_required"
    binding = cfg.data["wallet"]["providers"]["bitget_main"]
    assert binding["provider"] == "bitget"
    assert binding["config"]["skill_path"].endswith("bitget-wallet-skill")
    assert binding["config"]["entry"] == "scripts/bitget-wallet-agent-api.py"


def test_wallet_auth_start_does_not_overwrite_other_provider_binding(
    tmp_path,
    monkeypatch,
) -> None:
    from nerya.api import routes_wallet
    from nerya.api.routes_wallet import routes as wallet_routes

    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "providers": {
            "okx_os_main": {
                "provider": "okx_os",
                "label": "OKX Wallet",
                "config": {"account_id": "okx-account-1"},
            }
        }
    }
    handler = dict(((method, path), fn) for method, path, fn in wallet_routes())[
        ("POST", "/wallet/auth/start")
    ]
    client = SimpleNamespace(config=cfg)

    def fake_install_for_auth(client_arg, provider, *, approve):
        return {
            "ok": True,
            "kind": "already-installed",
            "target": "https://github.com/bitget-wallet-ai-lab/bitget-wallet-skill",
            "install_path": str(tmp_path / "skills" / "_node" / "bitget-wallet-skill"),
            "extra": {
                "install_state": {
                    "install_command": (
                        "git-repo:https://github.com/bitget-wallet-ai-lab/bitget-wallet-skill"
                        "#entry=scripts/bitget-wallet-agent-api.py"
                    )
                }
            },
        }

    monkeypatch.setattr(routes_wallet, "_install_for_auth", fake_install_for_auth)

    res = handler(
        client,
        {
            "provider": "bitget",
            "approve": True,
            "wallet_id": "okx_os_main",
            "label": "Stale OKX Label",
            "create_binding": True,
        },
    )

    assert res["ok"] is True
    assert cfg.data["wallet"]["providers"]["okx_os_main"]["provider"] == "okx_os"
    assert cfg.data["wallet"]["providers"]["bitget_main"]["provider"] == "bitget"
    assert cfg.data["wallet"]["providers"]["bitget_main"]["label"].startswith("Bitget")


def test_bitget_python_skill_readiness_and_klines(tmp_path, monkeypatch) -> None:
    from nerya.wallet.providers.bitget import BitgetWalletSkill

    skill_dir = tmp_path / "bitget-wallet-skill"
    script = skill_dir / "scripts" / "bitget-wallet-agent-api.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('{}')\n", encoding="utf-8")
    provider = BitgetWalletSkill(skill_path=str(skill_dir))

    ready = provider.readiness()
    assert ready.ready is True

    def fake_run(self, args, *, timeout_s=30.0):
        assert args[:5] == [
            "kline",
            "--chain",
            "eth",
            "--contract",
            "0xtoken",
        ]
        return {
            "status": 0,
            "data": {
                "list": [
                    {
                        "ts": 1778562000,
                        "open": 1,
                        "high": 2,
                        "low": 0.5,
                        "close": 1.5,
                        "turnover": 12,
                    }
                ]
            },
        }

    monkeypatch.setattr(BitgetWalletSkill, "_run_python_skill", fake_run)
    candles = provider.get_token_klines(
        chain="eth", token="0xtoken", interval="1h", limit=1,
    )

    assert candles == [
        {
            "ts": 1778562000000,
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 12.0,
        }
    ]


def test_wallet_readiness_report_uses_binding_over_placeholder_legacy_config(
    tmp_path,
) -> None:
    from nerya.wallet.registry import readiness_report

    skill_dir = tmp_path / "bitget-wallet-skill"
    script = skill_dir / "scripts" / "bitget-wallet-agent-api.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('{}')\n", encoding="utf-8")
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "bitget": {"skill_path": "", "entry": "dist/nerya.js"},
        "providers": {
            "bitget_main": {
                "provider": "bitget",
                "label": "Bitget Wallet",
                "config": {
                    "skill_path": str(skill_dir),
                    "entry": "scripts/bitget-wallet-agent-api.py",
                },
            }
        },
    }

    rows = readiness_report(cfg.data, workspace=tmp_path)
    bitget = next(row for row in rows if row["id"] == "bitget")

    assert bitget["configured_wallet_id"] == "bitget_main"
    assert bitget["readiness"]["ready"] is True


def test_binance_readiness_detects_installed_npm_package(
    tmp_path,
    monkeypatch,
) -> None:
    from nerya.wallet.providers._node_skill import NodeSkillRef
    from nerya.wallet.registry import readiness_report

    package_entry = (
        tmp_path
        / "skills"
        / "_node"
        / "binance__agentic-wallet"
        / "node_modules"
        / "@binance"
        / "agentic-wallet"
        / "dist"
        / "index.js"
    )
    package_entry.parent.mkdir(parents=True)
    package_entry.write_text("console.log('{}')\n", encoding="utf-8")
    cfg = _config(tmp_path)
    monkeypatch.setattr(NodeSkillRef, "node_available", lambda self: True)

    rows = readiness_report(cfg.data, workspace=tmp_path)
    binance = next(row for row in rows if row["id"] == "binance_agentic")

    assert binance["readiness"]["ready"] is True


def test_coinbase_readiness_prefers_agentic_wallet_binding_over_legacy_defaults(
    tmp_path,
) -> None:
    from nerya.wallet.registry import readiness_report

    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "coinbase": {"network_id": "base-mainnet"},
        "providers": {
            "coinbase_main": {
                "provider": "coinbase",
                "label": "Coinbase Agentic Wallet",
                "config": {"agentic_session_path": str(tmp_path / "Electron")},
            }
        },
    }

    rows = readiness_report(cfg.data, workspace=tmp_path)
    coinbase = next(row for row in rows if row["id"] == "coinbase")

    assert coinbase["configured_wallet_id"] == "coinbase_main"
    assert coinbase["readiness"]["ready"] is True
    assert coinbase["readiness"]["missing"] == []


def test_okx_wallet_market_data_routes_through_fetch_candles(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "providers": {
            "okx_main": {
                "provider": "okx_os",
                "label": "OKX Web3",
                "config": {
                    "api_key": "key",
                    "api_secret": "secret",
                    "api_passphrase": "pass",
                    "api_project_id": "project",
                },
            }
        }
    }

    def fake_klines(self, *, chain, token, interval="1h", limit=100, **_kw):
        assert chain == "ethereum"
        assert token == "0xtoken"
        assert interval == "1h"
        assert limit == 2
        assert self.api_key == "key"
        return [
            {"ts": 1, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0},
            {"ts": 2, "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0, "volume": 11.0},
        ]

    monkeypatch.setattr(
        "nerya.wallet.providers.okx_os.OkxOsWallet.get_token_klines",
        fake_klines,
    )

    rows = fetch_candles(
        "OKX_ONCHAIN:ethereum:0xtoken",
        count=2,
        interval="1h",
        allow_mock=False,
        config_like=cfg,
    )

    assert len(rows) == 2
    assert rows[0]["_envelope"]["source"] == "okx_os"
    assert rows[0]["_envelope"]["venue"] == "okx_onchain"
    assert rows[0]["_envelope"]["connector_id"] == "okx_main"


def test_xagt_wallet_market_data_routes_through_okx_delegate(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "providers": {
            "xagt_main": {
                "provider": "xagt_agent_plugin",
                "label": "XAgent OKX",
                "config": {"user_id": "user-1"},
            }
        }
    }

    def fake_klines(self, *, chain, token, interval="1h", limit=100, **_kw):
        assert chain == "ethereum"
        assert token == "0xtoken"
        assert interval == "1h"
        assert limit == 2
        return [
            {"ts": 1, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0},
            {"ts": 2, "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0, "volume": 11.0},
        ]

    monkeypatch.setattr(
        "nerya.wallet.providers.okx_os.OkxOsWallet.get_token_klines",
        fake_klines,
    )

    rows = fetch_candles(
        "XAGT_ONCHAIN:ethereum:0xtoken",
        count=2,
        interval="1h",
        allow_mock=False,
        config_like=cfg,
    )

    assert len(rows) == 2
    assert rows[0]["_envelope"]["source"] == "xagt_agent_plugin"
    assert rows[0]["_envelope"]["venue"] == "xagt_onchain"
    assert rows[0]["_envelope"]["connector_id"] == "xagt_main"


def test_okx_wallet_market_data_can_use_onchainos_cli_without_api_keys(
    tmp_path,
    monkeypatch,
) -> None:
    from nerya.wallet.providers.okx_os import OkxOsWallet

    wallet = OkxOsWallet(workspace=str(tmp_path), config={})
    monkeypatch.setattr(wallet, "_onchainos_bin", lambda: "onchainos")

    def fake_run(args, *, timeout_s=30.0):
        assert args == [
            "market",
            "kline",
            "--address",
            "0xtoken",
            "--chain",
            "ethereum",
            "--bar",
            "1H",
            "--limit",
            "2",
        ]
        return [
            {"ts": "2000", "o": "1", "h": "2", "l": "0.5", "c": "1.5", "vol": "10"},
            {"ts": "3000", "o": "1.5", "h": "2.5", "l": "1", "c": "2", "vol": "11"},
        ]

    monkeypatch.setattr(wallet, "_run_onchainos", fake_run)

    rows = wallet.get_token_klines(
        chain="ethereum",
        token="0xtoken",
        interval="1h",
        limit=2,
    )

    assert rows == [
        {"ts": 2000, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0},
        {"ts": 3000, "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0, "volume": 11.0},
    ]


def test_bitget_wallet_market_data_routes_through_fetch_candles(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "providers": {
            "bitget_main": {
                "provider": "bitget",
                "label": "Bitget Wallet",
                "config": {
                    "market_api_key": "key",
                    "market_api_secret": "secret",
                },
            }
        }
    }

    def fake_klines(self, *, chain, token, interval="1h", limit=100, **_kw):
        assert chain == "base"
        assert token == "0xtoken"
        assert interval == "5m"
        assert limit == 1
        assert self.market_api_key == "key"
        return [
            {"ts": 2, "open": 2.0, "high": 3.0, "low": 1.0, "close": 2.5, "volume": 12.0},
        ]

    monkeypatch.setattr(
        "nerya.wallet.providers.bitget.BitgetWalletSkill.get_token_klines",
        fake_klines,
        raising=False,
    )

    rows = fetch_candles(
        "BITGET_ONCHAIN:base:0xtoken",
        count=1,
        interval="5m",
        allow_mock=False,
        config_like=cfg,
    )

    assert len(rows) == 1
    assert rows[0]["_envelope"]["source"] == "bitget"
    assert rows[0]["_envelope"]["venue"] == "bitget_onchain"
    assert rows[0]["_envelope"]["connector_id"] == "bitget_main"


def test_binance_alpha_wallet_market_data_routes_symbol_markets(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "providers": {
            "binance_web3_main": {
                "provider": "binance_agentic",
                "label": "Binance Web3",
                "config": {},
            }
        }
    }

    def fake_klines(self, *, market, interval="1h", limit=100, **_kw):
        assert market == "ALPHA_175USDT"
        assert interval == "15m"
        assert limit == 1
        return [
            {"ts": 3, "open": 3.0, "high": 4.0, "low": 2.0, "close": 3.5, "volume": 13.0},
        ]

    monkeypatch.setattr(
        "nerya.wallet.providers.binance_agentic.BinanceAgenticWallet.get_market_klines",
        fake_klines,
        raising=False,
    )

    rows = fetch_candles(
        "BINANCE_ALPHA:ALPHA_175USDT",
        count=1,
        interval="15m",
        allow_mock=False,
        config_like=cfg,
    )

    assert len(rows) == 1
    assert rows[0]["_envelope"]["source"] == "binance_agentic"
    assert rows[0]["_envelope"]["venue"] == "binance_alpha"
    assert rows[0]["_envelope"]["connector_id"] == "binance_web3_main"


def test_coinbase_wallet_market_data_routes_product_markets(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "providers": {
            "coinbase_main": {
                "provider": "coinbase",
                "label": "Coinbase CDP",
                "config": {},
            }
        }
    }

    def fake_klines(self, *, market, interval="1h", limit=100, **_kw):
        assert market == "BTC-USD"
        assert interval == "1h"
        assert limit == 1
        return [
            {"ts": 4, "open": 4.0, "high": 5.0, "low": 3.0, "close": 4.5, "volume": 14.0},
        ]

    monkeypatch.setattr(
        "nerya.wallet.providers.coinbase.CoinbaseWallet.get_market_klines",
        fake_klines,
        raising=False,
    )

    rows = fetch_candles(
        "COINBASE_WALLET:BTC-USD",
        count=1,
        interval="1h",
        allow_mock=False,
        config_like=cfg,
    )

    assert len(rows) == 1
    assert rows[0]["_envelope"]["source"] == "coinbase"
    assert rows[0]["_envelope"]["venue"] == "coinbase_wallet"
    assert rows[0]["_envelope"]["connector_id"] == "coinbase_main"


def test_wallet_configure_binding_vaultifies_plaintext(tmp_path) -> None:
    from nerya.api.routes_wallet import routes as wallet_routes
    from nerya.security.secrets import SecretVault

    cfg = _config(tmp_path)
    handler = dict(((method, path), fn) for method, path, fn in wallet_routes())[
        ("POST", "/wallet/configure")
    ]
    client = SimpleNamespace(config=cfg)

    res = handler(
        client,
        {
            "provider": "okx_os",
            "wallet_id": "okx_main",
            "label": "OKX Web3",
            "config": {
                "api_key": "plain-key",
                "api_secret": "plain-secret",
                "api_passphrase": "plain-pass",
                "api_project_id": "project-id",
            },
            "operator": "test",
        },
    )

    assert res["ok"] is True
    saved = yaml_io.load(cfg.paths.config)
    binding = saved["wallet"]["providers"]["okx_main"]
    assert binding["provider"] == "okx_os"
    assert binding["config"]["api_key_ref"] == "vault://wallet_okx_main_api_key"
    assert binding["config"]["api_project_id"] == "project-id"
    assert "api_key" not in binding["config"]
    vault = SecretVault.open(cfg.paths.vault_enc)
    assert vault.resolve("wallet_okx_main_api_key", required_scope="wallet") == "plain-key"


def test_subagent_legacy_market_data_skill_call_falls_through_to_native_tool(tmp_path) -> None:
    cfg = _config(tmp_path)
    skills = SkillKernel.boot(cfg)
    registry = ToolRegistry()
    registry.register(
        make_native_descriptor(
            name="market_data",
            description="test market data",
            input_schema={"type": "object"},
            handler=lambda call: market_data_handler(call, config_like=cfg),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NETWORK,
            read_only=True,
            auto_approve=True,
        )
    )
    runtime = SubAgentRuntime(
        config=cfg,
        skills=skills,
        llm=None,  # type: ignore[arg-type]
        tool_registry=registry,
    )

    result = runtime._dispatch_one(
        {
            "skill": "market_data",
            "action": "calculate_features",
            "payload": {"market": "mock:BTC/USDT", "interval": "1m", "count": 40},
        },
        spec_name="technical_analyst",
        allowed=[],
        allowed_native_tools=["market_data"],
        trigger_event_id=None,
        strategy_id=None,
        session_id=None,
    )

    assert result is not None
    assert result["ok"] is True
    data = result["result"]["data"]
    assert data["count"] == 40
    assert data["features"]["rsi_14"] is not None


def test_strategy_context_legacy_portfolio_pnl_and_dict_return_compat(tmp_path) -> None:
    cfg = _config(tmp_path)
    ledger = open_ledger(cfg.paths, "paper_main", 10_000)
    ledger.apply_fill(
        market="binance:BTCUSDT",
        side="buy",
        price=50_000,
        size=0.1,
        fee_usd=0,
    )

    portfolio = StrategyPortfolio(paths=cfg.paths)
    positions = portfolio.positions("BTC/USDT")
    assert portfolio.equity_usd == pytest.approx(10_000)
    assert len(positions) == 1
    assert positions[0].quantity == pytest.approx(0.1)
    assert positions[0].market_value_usd == pytest.approx(5_000)
    assert StrategyPnL(paths=cfg.paths, strategy_id="dict_return").summary()["drawdown_pct"] == 0

    root = cfg.paths.strategy("dict_return")
    root.mkdir(parents=True, exist_ok=True)
    yaml_io.dump(
        root / "strategy.yml",
        {
            "version": 1,
            "strategy_id": "dict_return",
            "title": "Dict Return Compatibility",
            "mode": "paper",
            "entrypoint": "main.py:run",
            "markets": ["BINANCE:BTCUSDT"],
            "accounts": ["paper_main"],
            "schedule": {"type": "interval", "every_seconds": 60},
            "policy": {
                "max_single_order_usd": 100,
                "max_daily_notional_usd": 500,
            "max_open_positions": 1,
            "min_confidence": 0,
            "max_run_seconds": 10,
            },
        },
    )
    (root / "main.py").write_text(
        "\n".join(
            [
                "def run(ctx):",
                "    assert ctx.portfolio.equity_usd > 0",
                "    assert ctx.pnl.summary()['drawdown_pct'] >= 0",
                "    ctx.log.info('compat path')",
                "    assert ctx.market_data is ctx.market",
                "    assert ctx.now().tzinfo is not None",
                "    return {'decision': 'HOLD', 'reason': 'legacy dict hold', 'market': ctx.config.markets[0]}",
            ]
        ),
        encoding="utf-8",
    )

    record = StrategyRunner(config=cfg).run_tick("dict_return", mode_override="paper")
    assert record.status == "hold"
    assert record.reason == "legacy dict hold"
    result = record.outputs["result"]
    assert result["metadata"]["decision"] == "HOLD"
    assert result["metadata"]["return_summary"]["market"] == "BINANCE:BTCUSDT"


@pytest.mark.integration
@pytest.mark.skipif(
    not REAL_MARKET_TESTS,
    reason="set NERYA_REAL_MARKET_TESTS=1 to hit real public market-data APIs",
)
def test_real_public_market_data_loads_for_native_and_strategy_paths(tmp_path) -> None:
    cfg = _config(tmp_path)
    cfg.data.setdefault("workspace_preferences", {})["market_defaults"] = {
        "venue": "binance",
        "preferred_venues": ["binance", "okx", "bybit"],
    }
    market_id, rows = _first_live_candles(cfg, count=40)
    assert len(rows) == 40
    env = rows[0]["_envelope"]
    assert env["mode"] == "live"
    assert env["source"] != "mock"
    assert env["fallback_used"] is False

    perp_rows = fetch_candles(
        "binance_perpetual:ETHUSDT",
        count=20,
        interval="1m",
        allow_mock=False,
        config_like=cfg,
    )
    assert len(perp_rows) == 20
    perp_env = perp_rows[0]["_envelope"]
    assert perp_env["mode"] == "live"
    assert perp_env["source"] != "mock"

    dynamic_rows = fetch_candles(
        "BTCUSDT",
        count=20,
        interval="1m",
        allow_mock=False,
        config_like=cfg,
    )
    assert len(dynamic_rows) == 20
    dynamic_env = dynamic_rows[0]["_envelope"]
    assert dynamic_env["mode"] == "live"
    assert dynamic_env["source"] != "mock"

    ticker = fetch_public_ticker("binance:BTCUSDT", allow_mock=False, config_like=cfg)
    assert ticker["price"] > 0
    assert ticker["age_s"] == 0
    assert ticker["_envelope"]["mode"] == "live"
    assert ticker["_envelope"]["source"] != "mock"

    native = _json_payload(
        market_data_handler(
            ToolCall(
                name="market_data",
                arguments={
                    "action": "calculate_features",
                    "market": market_id,
                    "interval": "1m",
                    "count": 40,
                },
            ),
            config_like=cfg,
        )
    )
    assert native["count"] == 40
    assert native["_envelope"]["mode"] == "live"
    assert native["_envelope"]["source"] != "mock"
    assert native["features"]["rsi_14"] is not None
    assert native["features"]["macd"]["hist"] is not None

    market = StrategyMarket(
        paths=cfg.paths,
        accounts=("paper_main",),
        _registry_factory=lambda: __import__(
            "nerya.connectors.registry",
            fromlist=["ConnectorRegistry"],
        ).ConnectorRegistry(cfg.paths.root),
    )
    strategy_features = market.features(
        market_id,
        timeframe="1m",
        lookback=40,
    )
    assert strategy_features["rows"] == 40
    assert strategy_features["first"]["_envelope"]["mode"] == "live"
    assert strategy_features["first"]["_envelope"]["source"] != "mock"
    assert strategy_features["rsi_14"] is not None
