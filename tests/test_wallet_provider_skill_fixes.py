"""Regression tests for the wallet-provider skill-dispatch audit fixes.

Covers:

- bitget: balance/quote/swap must dispatch to the official PYTHON skill
  entry (``--action`` flags), never through ``node``; swap results
  default ``ok=False``; kline timestamps are normalized to seconds on
  both the python-skill and direct-API paths; optional
  ``bitget_token``/``bitget_api_url`` config reaches the skill env.
- binance_agentic: swap defaults ``ok=False``; unparseable quotes raise
  ``WalletQuoteError``; the repo field is a clean clone URL.
- coinbase: readiness no longer claims ready from ``agentic_session_path``
  alone; node-skill preferred while the python SDK is partial; min_out
  uses the slippage_bps formula (not a hardcoded 1%); the CDP python
  path FETCHES the operator's existing wallet and never creates one.

No test in this module touches the network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from nerya.wallet.errors import (
    WalletDependencyError,
    WalletPolicyDenied,
    WalletQuoteError,
)
from nerya.wallet.providers._node_skill import NodeSkillRef
from nerya.wallet.providers.binance_agentic import BinanceAgenticWallet
from nerya.wallet.providers.bitget import (
    BitgetWalletSkill,
    _normalize_kline_ts,
)
from nerya.wallet.providers.coinbase import CoinbaseWallet, _slippage_floor


pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_BITGET_ENTRY = "scripts/bitget-wallet-agent-api.py"


def _make_bitget_py_provider(tmp_path, config: dict | None = None) -> BitgetWalletSkill:
    """A bitget provider whose python skill entry exists on disk."""
    skill_dir = tmp_path / "bitget-wallet-skill"
    script = skill_dir / "scripts" / "bitget-wallet-agent-api.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('{}')\n", encoding="utf-8")
    return BitgetWalletSkill(skill_path=str(skill_dir), config=dict(config or {}))


def _make_bitget_node_provider(tmp_path, config: dict | None = None) -> BitgetWalletSkill:
    skill_dir = tmp_path / "bitget-node-skill"
    entry = skill_dir / "dist" / "index.js"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("// noop\n", encoding="utf-8")
    return BitgetWalletSkill(
        skill_path=str(skill_dir), entry="dist/index.js",
        config=dict(config or {}),
    )


def _make_coinbase_node_skill(tmp_path) -> str:
    """A coinbase skill checkout that passes NodeSkillRef.skill_ready()."""
    skill_dir = tmp_path / "coinbase-cdp-skill"
    entry = skill_dir / "dist" / "index.js"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("// noop\n", encoding="utf-8")
    (skill_dir / "node_modules").mkdir(exist_ok=True)
    return str(skill_dir)


def _no_node_invocation(self, command, payload, *, timeout_s: float = 25.0):
    raise AssertionError(
        f"node skill must not be invoked (command={command!r})"
    )


def _no_python_skill(self, args, *, timeout_s: float = 30.0):
    raise AssertionError(f"python skill must not be invoked (args={args!r})")


def _fake_cdp_module(monkeypatch, wallets: list) -> dict[str, Any]:
    """Inject a fake legacy ``cdp`` module exposing Cdp + Wallet."""
    mod = ModuleType("cdp")
    calls: dict[str, Any] = {"fetch": [], "list": 0, "create": 0}

    class FakeAddress:
        def __init__(self, address_id: str) -> None:
            self.address_id = address_id

    class FakeWallet:
        def __init__(self, wallet_id: str, network_id: str, address: str) -> None:
            self.id = wallet_id
            self.network_id = network_id
            self.default_address = FakeAddress(address)

        @classmethod
        def fetch(cls, wallet_id: str) -> "FakeWallet":
            calls["fetch"].append(wallet_id)
            for w in wallets:
                if w.id == wallet_id:
                    return w
            raise RuntimeError(f"no wallet {wallet_id!r}")

        @classmethod
        def list(cls):
            calls["list"] += 1
            yield from wallets

        @classmethod
        def create(cls, **_kw):
            calls["create"] += 1
            raise AssertionError("Wallet.create must never be called")

    class FakeCdp:
        @staticmethod
        def configure(**_kw) -> None:
            return None

    mod.Cdp = FakeCdp
    mod.Wallet = FakeWallet
    monkeypatch.setitem(sys.modules, "cdp", mod)
    return calls


# ---------------------------------------------------------------------------
# bitget — python-skill dispatch for balance / quote / swap
# ---------------------------------------------------------------------------


def test_bitget_balance_routes_to_python_skill(tmp_path, monkeypatch) -> None:
    provider = _make_bitget_py_provider(tmp_path)
    seen: dict[str, Any] = {}

    def fake_run(self, args, *, timeout_s=30.0):
        seen["args"] = list(args)
        return {"balance": 1.25, "symbol": "BNB", "decimals": 18}

    monkeypatch.setattr(BitgetWalletSkill, "_run_python_skill", fake_run)
    monkeypatch.setattr(NodeSkillRef, "invoke", _no_node_invocation)

    bal = provider.get_balance(chain="bsc", address="0xabc", token="")

    assert bal.provider == "bitget"
    assert bal.balance == pytest.approx(1.25)
    assert bal.symbol == "BNB"
    assert bal.decimals == 18
    assert seen["args"] == [
        "--action", "balance", "--chain", "bsc", "--address", "0xabc",
    ]


def test_bitget_quote_routes_to_python_skill(tmp_path, monkeypatch) -> None:
    provider = _make_bitget_py_provider(tmp_path)
    seen: dict[str, Any] = {}

    def fake_run(self, args, *, timeout_s=30.0):
        seen["args"] = list(args)
        return {"expected_out": 2.0, "price_impact_bps": 15}

    monkeypatch.setattr(BitgetWalletSkill, "_run_python_skill", fake_run)
    monkeypatch.setattr(NodeSkillRef, "invoke", _no_node_invocation)

    q = provider.quote(
        chain="bsc", token_in="AAA", token_out="BBB",
        amount_in=1.0, slippage_bps=50,
    )

    assert q.expected_out == pytest.approx(2.0)
    assert q.min_out == pytest.approx(2.0 * (1 - 50 / 10_000))
    assert seen["args"][:2] == ["--action", "quote"]
    assert "--slippage-bps" in seen["args"]
    assert "--token-in" in seen["args"]


def test_bitget_swap_routes_to_python_skill_and_defaults_not_ok(
    tmp_path, monkeypatch,
) -> None:
    provider = _make_bitget_py_provider(tmp_path)
    seen: dict[str, Any] = {}
    docs = iter([
        {  # skill doc WITHOUT "ok" — must not be reported as success
            "tx_hash": "0xfail", "reason": "swap reverted",
        },
        {"ok": True, "tx_hash": "0x1", "amount_out": 2.0},
        {"ok": False, "reason": "insufficient allowance"},
    ])

    def fake_run(self, args, *, timeout_s=30.0):
        seen["args"] = list(args)
        seen["timeout"] = timeout_s
        return next(docs)

    monkeypatch.setattr(BitgetWalletSkill, "_run_python_skill", fake_run)
    monkeypatch.setattr(NodeSkillRef, "invoke", _no_node_invocation)

    res = provider.swap(
        chain="bsc", token_in="AAA", token_out="BBB",
        amount_in=1.0, live=True,
    )
    assert res.ok is False
    assert res.reason == "swap reverted"

    res_ok = provider.swap(
        chain="bsc", token_in="AAA", token_out="BBB",
        amount_in=1.0, live=True,
    )
    assert res_ok.ok is True
    assert res_ok.tx_hash == "0x1"

    res_fail = provider.swap(
        chain="bsc", token_in="AAA", token_out="BBB",
        amount_in=1.0, receiver="0xrecv", live=True,
    )
    assert res_fail.ok is False

    assert seen["args"][:2] == ["--action", "swap"]
    assert seen["args"][-2:] == ["--receiver", "0xrecv"]
    assert seen["timeout"] == 60.0


def test_bitget_dry_swap_never_invokes_any_skill(tmp_path, monkeypatch) -> None:
    provider = _make_bitget_py_provider(tmp_path)
    monkeypatch.setattr(BitgetWalletSkill, "_run_python_skill", _no_python_skill)
    monkeypatch.setattr(NodeSkillRef, "invoke", _no_node_invocation)

    res = provider.swap(
        chain="bsc", token_in="AAA", token_out="BBB",
        amount_in=1.0, live=False,
    )
    assert res.ok is False
    assert "live=False" in res.reason


def test_bitget_node_entry_still_uses_node_ref(tmp_path, monkeypatch) -> None:
    provider = _make_bitget_node_provider(tmp_path)

    def fake_invoke(self, command, payload, *, timeout_s=25.0):
        assert command == "balance"
        return {"balance": 3.0, "symbol": "BNB"}

    monkeypatch.setattr(BitgetWalletSkill, "_run_python_skill", _no_python_skill)
    monkeypatch.setattr(NodeSkillRef, "invoke", fake_invoke)

    bal = provider.get_balance(chain="bsc", address="0xa", token="")
    assert bal.balance == pytest.approx(3.0)


def test_bitget_python_branch_requires_skill_path(monkeypatch) -> None:
    # entry defaults to the python script but no skill_path configured:
    # dispatch must fall through to the node ref which reports the
    # missing dependency — not silently "run" anything.
    provider = BitgetWalletSkill()
    monkeypatch.setattr(BitgetWalletSkill, "_run_python_skill", _no_python_skill)

    with pytest.raises(WalletDependencyError):
        provider.get_balance(chain="bsc", address="0xa", token="")


# ---------------------------------------------------------------------------
# bitget — operator credential passthrough (bitget_token / bitget_api_url)
# ---------------------------------------------------------------------------


def test_bitget_optional_creds_reach_python_skill_env(tmp_path, monkeypatch) -> None:
    provider = _make_bitget_py_provider(tmp_path, config={
        "bitget_token": "tok-1",
        "bitget_api_url": "https://api.example/v1",
    })
    captured: dict[str, Any] = {}

    class FakeProc:
        returncode = 0
        stdout = json.dumps({"balance": 1.0})
        stderr = ""

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return FakeProc()

    monkeypatch.setattr(subprocess, "run", fake_run)

    provider.get_balance(chain="bsc", address="0xa", token="")

    assert captured["env"]["BITGET_TOKEN"] == "tok-1"
    assert captured["env"]["BITGET_API_URL"] == "https://api.example/v1"
    # the child must still inherit the parent environment
    assert captured["env"].get("PATH")


def test_bitget_optional_creds_reach_node_skill_env(tmp_path, monkeypatch) -> None:
    provider = _make_bitget_node_provider(tmp_path, config={"bitget_token": "tok-node"})
    seen: dict[str, Any] = {}

    def fake_invoke(self, command, payload, *, timeout_s=25.0):
        seen["token"] = os.environ.get("BITGET_TOKEN")
        return {"balance": 1.0}

    monkeypatch.setattr(NodeSkillRef, "invoke", fake_invoke)

    provider.get_balance(chain="bsc", address="0xa", token="")

    assert seen["token"] == "tok-node"
    # restored after the call
    assert "BITGET_TOKEN" not in os.environ


def test_bitget_readiness_does_not_require_optional_creds(tmp_path) -> None:
    provider = _make_bitget_py_provider(tmp_path)
    assert provider.readiness().ready is True
    provider = _make_bitget_py_provider(tmp_path, config={"bitget_token": "t"})
    assert provider.readiness().ready is True


# ---------------------------------------------------------------------------
# bitget — kline timestamps normalized to seconds on BOTH paths
# ---------------------------------------------------------------------------


def test_normalize_kline_ts_matches_sibling_providers() -> None:
    assert _normalize_kline_ts(1778562000) == 1778562000          # already s
    assert _normalize_kline_ts(1778562000000) == 1778562000       # ms → s
    assert _normalize_kline_ts(0) == 0


def test_bitget_klines_python_path_emits_seconds(tmp_path, monkeypatch) -> None:
    provider = _make_bitget_py_provider(tmp_path)

    def fake_run(self, args, *, timeout_s=30.0):
        return {
            "data": {"list": [{
                "ts": 1778562000000, "open": 1, "high": 2,
                "low": 0.5, "close": 1.5, "turnover": 12,
            }]},
        }

    monkeypatch.setattr(BitgetWalletSkill, "_run_python_skill", fake_run)

    rows = provider.get_token_klines(chain="eth", token="0xt", interval="1h", limit=1)
    assert rows[0]["ts"] == 1778562000


def test_bitget_klines_direct_api_emits_seconds(monkeypatch) -> None:
    provider = BitgetWalletSkill(market_api_key="k", market_api_secret="s")

    class FakeHttp:
        def request(self, method, url, *, headers=None, body=None, timeout=None):
            return 200, {
                "status": 0,
                "data": {"list": [{
                    "ts": 1778562000000, "open": 1, "high": 2,
                    "low": 0.5, "close": 1.5, "turnover": 4,
                }]},
            }

    monkeypatch.setattr("nerya.connectors.http.UrllibHttp", FakeHttp)

    rows = provider.get_token_klines(chain="eth", token="0xabc", interval="1h", limit=2)
    assert rows[0]["ts"] == 1778562000


# ---------------------------------------------------------------------------
# binance_agentic
# ---------------------------------------------------------------------------


def test_binance_swap_defaults_to_not_ok(monkeypatch) -> None:
    provider = BinanceAgenticWallet()
    docs = iter([{"tx_hash": "0x2"}, {"ok": True, "tx_hash": "0x3"}])

    def fake_invoke(self, command, payload, *, timeout_s=25.0):
        return next(docs)

    monkeypatch.setattr(NodeSkillRef, "invoke", fake_invoke)

    res = provider.swap(
        chain="bsc", token_in="A", token_out="B", amount_in=1.0, live=True,
    )
    assert res.ok is False  # doc without "ok" is NOT a success

    res_ok = provider.swap(
        chain="bsc", token_in="A", token_out="B", amount_in=1.0, live=True,
    )
    assert res_ok.ok is True


def test_binance_quote_unparseable_raises_quote_error(monkeypatch) -> None:
    provider = BinanceAgenticWallet()
    docs = iter([{}, {"expected_out": "not-a-number"}, {"expected_out": 4.0}])

    def fake_invoke(self, command, payload, *, timeout_s=25.0):
        return next(docs)

    monkeypatch.setattr(NodeSkillRef, "invoke", fake_invoke)

    with pytest.raises(WalletQuoteError):
        provider.quote(
            chain="bsc", token_in="A", token_out="B", amount_in=1.0,
        )
    with pytest.raises(WalletQuoteError):
        provider.quote(
            chain="bsc", token_in="A", token_out="B", amount_in=1.0,
        )

    q = provider.quote(
        chain="bsc", token_in="A", token_out="B",
        amount_in=1.0, slippage_bps=100,
    )
    assert q.expected_out == pytest.approx(4.0)
    assert q.min_out == pytest.approx(4.0 * (1 - 100 / 10_000))


def test_binance_repo_is_clean_clone_url() -> None:
    provider = BinanceAgenticWallet()
    assert " " not in provider.repo
    assert "#" not in provider.repo
    assert provider.repo.startswith("https://github.com/")
    assert "binance-agentic-wallet" in provider.subdir

    # the not-ready install hint must embed a valid git clone line
    hint = provider.readiness().install_hint
    assert provider.repo in hint
    assert provider.subdir in hint
    assert f"git clone {provider.repo}" in hint


# ---------------------------------------------------------------------------
# coinbase — readiness
# ---------------------------------------------------------------------------


def test_coinbase_readiness_not_ready_with_only_agentic_session(monkeypatch) -> None:
    monkeypatch.setattr(CoinbaseWallet, "_probe_py", lambda self: None)
    provider = CoinbaseWallet(
        skill_path="",
        config={"agentic_session_path": "/tmp/does-not-matter.json"},
    )

    ready = provider.readiness()

    assert ready.ready is False
    assert any("api_key_name" in m for m in ready.missing)
    assert any("pip:cdp-sdk" in m for m in ready.missing)


def test_coinbase_readiness_needs_more_than_creds(monkeypatch) -> None:
    monkeypatch.setattr(CoinbaseWallet, "_probe_py", lambda self: None)
    provider = CoinbaseWallet(api_key_name="kid", api_private_key="priv", skill_path="")

    ready = provider.readiness()

    assert ready.ready is False
    assert "no python cdp-sdk" in ready.reason


def test_coinbase_readiness_ready_with_python_and_creds(monkeypatch) -> None:
    monkeypatch.setattr(CoinbaseWallet, "_probe_py", lambda self: "cdp")
    provider = CoinbaseWallet(api_key_name="kid", api_private_key="priv", skill_path="")
    assert provider.readiness().ready is True


# ---------------------------------------------------------------------------
# coinbase — min_out slippage formula + routing precedence
# ---------------------------------------------------------------------------


def test_slippage_floor_pure_helper() -> None:
    assert _slippage_floor(2.0, 30) == pytest.approx(2.0 * (1 - 30 / 10_000))
    assert _slippage_floor(1.0, 50) == pytest.approx(0.995)


def test_coinbase_node_doc_min_out_uses_slippage_formula(
    tmp_path, monkeypatch,
) -> None:
    skill_dir = _make_coinbase_node_skill(tmp_path)
    provider = CoinbaseWallet(
        api_key_name="kid", api_private_key="priv", skill_path=skill_dir,
    )
    # no python SDK installed → node skill is the only real path
    monkeypatch.setattr(CoinbaseWallet, "_probe_py", lambda self: None)
    monkeypatch.setattr(NodeSkillRef, "node_available", lambda self: True)
    monkeypatch.setattr(
        NodeSkillRef, "invoke",
        lambda self, command, payload, *, timeout_s=25.0: {"expected_out": 2.0},
    )

    q = provider.quote(
        chain="base", token_in="ETH", token_out="USDC",
        amount_in=2.0, slippage_bps=30,
    )

    # 30 bps floor is 1.994 — the old hardcoded 1% fallback said 1.98
    assert q.min_out == pytest.approx(2.0 * (1 - 30 / 10_000))
    assert q.min_out != pytest.approx(1.98)


def test_coinbase_prefers_node_skill_while_python_is_partial(
    tmp_path, monkeypatch,
) -> None:
    skill_dir = _make_coinbase_node_skill(tmp_path)
    provider = CoinbaseWallet(
        api_key_name="kid", api_private_key="priv", skill_path=skill_dir,
    )
    monkeypatch.setattr(CoinbaseWallet, "_probe_py", lambda self: "coinbase_agentkit")
    monkeypatch.setattr(NodeSkillRef, "node_available", lambda self: True)

    def _no_python(self):
        raise AssertionError("partial python path must not shadow the node skill")

    monkeypatch.setattr(CoinbaseWallet, "_py_wallet", _no_python)
    monkeypatch.setattr(
        NodeSkillRef, "invoke",
        lambda self, command, payload, *, timeout_s=25.0: {"balance": 7.0},
    )

    bal = provider.get_balance(chain="base", address="0xa", token="")
    assert bal.balance == pytest.approx(7.0)


def test_coinbase_full_python_sdk_keeps_python_path(tmp_path, monkeypatch) -> None:
    skill_dir = _make_coinbase_node_skill(tmp_path)
    provider = CoinbaseWallet(
        api_key_name="kid", api_private_key="priv", skill_path=skill_dir,
    )
    monkeypatch.setattr(CoinbaseWallet, "_probe_py", lambda self: "cdp")

    fake_wallet = SimpleNamespace(balance=lambda asset_id: 5.0)
    monkeypatch.setattr(
        CoinbaseWallet, "_py_wallet", lambda self: (fake_wallet, "cdp"),
    )
    monkeypatch.setattr(NodeSkillRef, "invoke", _no_node_invocation)

    bal = provider.get_balance(chain="base", address="0xa", token="eth")
    assert bal.balance == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# coinbase — fetch the operator's EXISTING wallet (never create)
# ---------------------------------------------------------------------------


def test_coinbase_cdp_fetches_existing_default_wallet(monkeypatch) -> None:
    wallets = [
        SimpleNamespace(
            id="w-eth", network_id="ethereum", default_address=SimpleNamespace(address_id="0xETH"),
        ),
        SimpleNamespace(
            id="w-base", network_id="base-mainnet", default_address=SimpleNamespace(address_id="0xBASE"),
        ),
    ]
    calls = _fake_cdp_module(monkeypatch, wallets)
    monkeypatch.setattr(CoinbaseWallet, "_probe_py", lambda self: "cdp")

    provider = CoinbaseWallet(api_key_name="kid", api_private_key="priv")
    wallet, kind = provider._py_wallet()

    assert kind == "cdp"
    assert wallet.id == "w-base"          # first wallet on our network
    assert calls["create"] == 0
    assert calls["fetch"] == []


def test_coinbase_cdp_prefers_configured_wallet_id(monkeypatch) -> None:
    wallets = [
        SimpleNamespace(
            id="w-base", network_id="base-mainnet", default_address=SimpleNamespace(address_id="0xBASE"),
        ),
    ]
    calls = _fake_cdp_module(monkeypatch, wallets)
    monkeypatch.setattr(CoinbaseWallet, "_probe_py", lambda self: "cdp")

    provider = CoinbaseWallet(
        api_key_name="kid", api_private_key="priv",
        config={"wallet_id": "w-base", "address": "0xbasE"},  # case-insensitive
    )
    wallet, kind = provider._py_wallet()

    assert kind == "cdp"
    assert wallet.id == "w-base"
    assert calls["fetch"] == ["w-base"]
    assert calls["create"] == 0


def test_coinbase_cdp_missing_wallet_id_is_policy_denied(monkeypatch) -> None:
    _fake_cdp_module(monkeypatch, [])
    monkeypatch.setattr(CoinbaseWallet, "_probe_py", lambda self: "cdp")

    provider = CoinbaseWallet(
        api_key_name="kid", api_private_key="priv",
        config={"wallet_id": "w-missing"},
    )

    with pytest.raises(WalletPolicyDenied):
        provider._py_wallet()


def test_coinbase_cdp_no_wallet_on_network_is_policy_denied(monkeypatch) -> None:
    wallets = [
        SimpleNamespace(
            id="w-eth", network_id="ethereum", default_address=SimpleNamespace(address_id="0xETH"),
        ),
    ]
    _fake_cdp_module(monkeypatch, wallets)
    monkeypatch.setattr(CoinbaseWallet, "_probe_py", lambda self: "cdp")

    provider = CoinbaseWallet(api_key_name="kid", api_private_key="priv")

    with pytest.raises(WalletPolicyDenied):
        provider._py_wallet()


def test_coinbase_cdp_address_mismatch_is_policy_denied(monkeypatch) -> None:
    wallets = [
        SimpleNamespace(
            id="w-base", network_id="base-mainnet", default_address=SimpleNamespace(address_id="0xACTUAL"),
        ),
    ]
    _fake_cdp_module(monkeypatch, wallets)
    monkeypatch.setattr(CoinbaseWallet, "_probe_py", lambda self: "cdp")

    provider = CoinbaseWallet(
        api_key_name="kid", api_private_key="priv",
        config={"address": "0xEXPECTED"},
    )

    with pytest.raises(WalletPolicyDenied):
        provider._py_wallet()


# ---------------------------------------------------------------------------
# coinbase — agentkit balance API reality
# ---------------------------------------------------------------------------


def test_coinbase_agentkit_native_balance_divides_wei(monkeypatch) -> None:
    monkeypatch.setattr(CoinbaseWallet, "_probe_py", lambda self: "coinbase_agentkit")
    fake_provider = SimpleNamespace(get_balance=lambda: 2_000_000_000_000_000_000)
    monkeypatch.setattr(
        CoinbaseWallet, "_py_wallet", lambda self: (fake_provider, "agentkit"),
    )

    provider = CoinbaseWallet(api_key_name="kid", api_private_key="priv")

    bal = provider.get_balance(chain="base", address="0xa", token="")
    assert bal.balance == pytest.approx(2.0)
    assert bal.symbol == "ETH"

    # token balances genuinely cannot be read on this path — no silent
    # mis-parse, and never get_balance(token) on the provider object.
    with pytest.raises(WalletPolicyDenied):
        provider.get_balance(chain="base", address="0xa", token="USDC")


def test_coinbase_agentkit_modern_provider_forwards_address_and_secret(
    monkeypatch,
) -> None:
    mod = ModuleType("coinbase_agentkit")
    captured: dict[str, Any] = {}

    class FakeConfig:
        def __init__(self, **kw) -> None:
            captured["config"] = kw

    class FakeProvider:
        def __init__(self, cfg) -> None:
            captured["provider"] = cfg

    mod.CdpEvmWalletProvider = FakeProvider
    mod.CdpEvmWalletProviderConfig = FakeConfig
    monkeypatch.setitem(sys.modules, "coinbase_agentkit", mod)
    monkeypatch.setattr(CoinbaseWallet, "_probe_py", lambda self: "coinbase_agentkit")

    provider = CoinbaseWallet(
        api_key_name="kid", api_private_key="priv",
        config={"address": "0xABC", "wallet_secret": "ws-1"},
    )
    wp, kind = provider._py_wallet()

    assert kind == "agentkit"
    assert captured["config"]["address"] == "0xABC"
    assert captured["config"]["wallet_secret"] == "ws-1"
    assert captured["config"]["network_id"] == "base-mainnet"
    assert wp is not None


def test_coinbase_agentkit_modern_requires_wallet_secret(monkeypatch) -> None:
    mod = ModuleType("coinbase_agentkit")
    mod.CdpEvmWalletProvider = object  # modern naming present
    mod.CdpEvmWalletProviderConfig = object
    monkeypatch.setitem(sys.modules, "coinbase_agentkit", mod)
    monkeypatch.setattr(CoinbaseWallet, "_probe_py", lambda self: "coinbase_agentkit")

    provider = CoinbaseWallet(api_key_name="kid", api_private_key="priv")

    with pytest.raises(WalletDependencyError) as excinfo:
        provider._py_wallet()
    assert any("wallet_secret" in m for m in excinfo.value.missing)
