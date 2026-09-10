"""Regression tests for the on-chain wallet pipeline fixes.

Covers (2026-09 wallet audit):
- dex_base._rpc fails loudly instead of collapsing every error into None
- EVMNative: chain-id verification pre-sign, receipt wait, decimals
- provider_spec: venue rows default chain_id from the canonical map
- self_custody: per-chain native detection, on-chain decimals, live-swap
  wiring (BSC PancakeSwap / Solana Jupiter) with vault-resolved keys
- swap_approval: degenerate-quote rejection, expiry enforcement, zero
  floor refusal
- SecretVault: default-passphrase warning + corrupt-vault surfacing
- wallet.registry: config-aware cache + loud vault failures

No test in this file touches the network: every HTTP call goes through
scripted fake transports injected via ``SelfCustodyWallet(transport=...)``.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

import nerya.agent  # noqa: F401  (break benign circular import)
from nerya.connectors.bsc_native import _pad_uint
from nerya.connectors.evm_native import EVMNative, EVM_CHAIN_IDS
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.errors import TradingError
from nerya.core.paths import WorkspacePaths
from nerya.wallet.errors import (
    WalletPolicyDenied,
    WalletQuoteError,
)
from nerya.wallet.protocol import WalletQuote, WalletSwapResult

pytestmark = pytest.mark.smoke

_ETH_RPC = "http://localhost:1"
_BSC_RPC = "http://bsc-fake"
_SOL_RPC = "http://sol-fake"
_JUP = "https://quote-api.jup.ag/v6"

_USDT_BSC = "0x55d398326f99059fF775485246999027B3197955"
_WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
_USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


# ---------------------------------------------------------------------------
# Fakes


class FakeRpcTransport:
    """Scripted JSON-RPC + HTTP transport.

    Keys are JSON-RPC method names or ``"GET <url>"`` / ``"POST <url>"``.
    Values: callable ``(body) -> result``, a list of canned results
    (popped per call; the last value repeats when exhausted), or any
    constant. Results wrapped in a ``(status, doc)`` tuple are returned
    verbatim; anything else is wrapped as a JSON-RPC ``result``.
    """

    def __init__(self, script: dict[str, Any] | None = None):
        self.script = dict(script or {})
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method, url, *, headers=None, params=None,
                body=None, timeout=15.0):
        self.calls.append((method, url, body))
        if isinstance(body, dict) and body.get("jsonrpc"):
            name = body.get("method")
        else:
            name = f"{method} {url}"
        if name not in self.script:
            raise AssertionError(
                f"unexpected rpc call: {name} (known: {sorted(self.script)})"
            )
        entry = self.script[name]
        if callable(entry):
            result = entry(body)
        elif isinstance(entry, list):
            result = entry.pop(0) if len(entry) > 1 else entry[0]
        else:
            result = entry
        if isinstance(result, tuple):
            return result
        if name.startswith(("GET ", "POST ")):
            # Raw HTTP endpoint (aggregator APIs): response body verbatim.
            return 200, result
        return 200, {"jsonrpc": "2.0", "id": 1, "result": result}


def _make_config(tmp_path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    return Config(paths=WorkspacePaths(root=tmp_path), data=data)


def _self_custody(signer_ref: str = "", rpc_urls: dict | None = None,
                  transport: Any = None):
    from nerya.wallet.providers.self_custody import SelfCustodyWallet

    return SelfCustodyWallet(
        signer_ref=signer_ref, rpc_urls=rpc_urls or {}, transport=transport,
        config={"signer_ref": signer_ref},
    )


def _seed_vault(tmp_path, key_hex: str) -> str:
    from nerya.security.secrets import SecretVault

    vault_path = tmp_path / "vault" / "secrets.enc"
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    v = SecretVault.open(vault_path, passphrase="test-pp")
    v.put(name="signer-key", value=key_hex, kind="opaque",
          scope=["wallet", "exchange"])
    return str(tmp_path)


# ---------------------------------------------------------------------------
# dex_base._rpc


def test_rpc_raises_when_no_rpc_url() -> None:
    conn = EVMNative(chain="ethereum", chain_id=1, rpc_url="")
    with pytest.raises(TradingError, match="rpc_url is not configured"):
        conn._rpc("eth_chainId", [])


def test_rpc_raises_on_http_error_and_jsonrpc_error() -> None:
    t = FakeRpcTransport({"eth_chainId": (500, {"error": "boom"})})
    conn = EVMNative(chain="ethereum", chain_id=1, rpc_url=_ETH_RPC,
                     transport=t)
    with pytest.raises(TradingError, match="http 500"):
        conn._rpc("eth_chainId", [])

    t2 = FakeRpcTransport({
        "eth_getBalance": (200, {"jsonrpc": "2.0", "id": 1,
                                 "error": {"code": -32000, "message": "no"}}),
    })
    conn2 = EVMNative(chain="ethereum", chain_id=1, rpc_url=_ETH_RPC,
                      transport=t2)
    with pytest.raises(TradingError, match="json-rpc error"):
        conn2._rpc("eth_getBalance", ["0xabc", "latest"])


def test_rpc_returns_none_for_null_result() -> None:
    t = FakeRpcTransport({"eth_getTransactionReceipt": None})
    conn = EVMNative(chain="ethereum", chain_id=1, rpc_url=_ETH_RPC,
                     transport=t)
    assert conn._rpc("eth_getTransactionReceipt", ["0xhash"]) is None


# ---------------------------------------------------------------------------
# EVMNative writes: chain-id verification + receipt confirmation


def _eth_script(tx_hash: str) -> dict[str, Any]:
    return {
        "eth_chainId": hex(1),
        "eth_gasPrice": hex(2_000_000_000),
        "eth_getTransactionCount": hex(7),
        "eth_sendRawTransaction": tx_hash,
        "eth_getTransactionReceipt": {"status": "0x1", "blockNumber": hex(99),
                                      "blockHash": "0x" + "a" * 64,
                                      "gasUsed": hex(21000)},
    }


def test_send_raw_transaction_confirms_receipt() -> None:
    from eth_account import Account

    acct = Account.create()
    t = FakeRpcTransport(_eth_script("0x" + "beef" * 16))
    conn = EVMNative(chain="ethereum", chain_id=1, rpc_url=_ETH_RPC,
                     live=True, transport=t)
    out = conn.send_raw_transaction(
        to=acct.address, value=1, signer_private_key=acct.key.hex(),
    )
    assert out["tx_hash"] == "0x" + "beef" * 16
    assert out["confirmed"] is True
    assert out["nonce"] == 7
    assert out["gas_price_gwei"] == 2.0


def test_send_raw_transaction_refuses_chain_id_mismatch() -> None:
    from eth_account import Account

    acct = Account.create()
    script = _eth_script("0x" + "ee" * 32)
    script["eth_chainId"] = hex(56)  # RPC serves BSC, connector says mainnet
    t = FakeRpcTransport(script)
    conn = EVMNative(chain="ethereum", chain_id=1, rpc_url=_ETH_RPC,
                     live=True, transport=t)
    with pytest.raises(TradingError, match="chain-id mismatch"):
        conn.send_raw_transaction(to=acct.address, value=1,
                                  signer_private_key=acct.key.hex())
    # Refusal happens BEFORE any signature or broadcast.
    assert not any(c[0] == "eth_sendRawTransaction" for c in t.calls)


def test_wait_for_receipt_raises_on_revert_and_timeout() -> None:
    t = FakeRpcTransport({
        "eth_getTransactionReceipt": {"status": "0x0",
                                      "blockHash": "0x" + "b" * 64},
    })
    conn = EVMNative(chain="ethereum", chain_id=1, rpc_url=_ETH_RPC,
                     transport=t)
    with pytest.raises(TradingError, match="reverted"):
        conn.wait_for_receipt("0xh", timeout_s=1.0, poll_s=0.01)

    t2 = FakeRpcTransport({"eth_getTransactionReceipt": None})  # always null
    conn2 = EVMNative(chain="ethereum", chain_id=1, rpc_url=_ETH_RPC,
                      transport=t2)
    with pytest.raises(TradingError, match="not mined"):
        conn2.wait_for_receipt("0xh", timeout_s=0.05, poll_s=0.01)


def test_erc20_balance_uses_onchain_decimals() -> None:
    # balanceOf is called first, decimals second. A USDC-style 6-decimal
    # token with raw 1_500_000 must read as 1.5, not 1.5e-12.
    t = FakeRpcTransport({
        "eth_call": [
            "0x" + hex(1_500_000)[2:].zfill(64),  # balanceOf()
            hex(6),                               # decimals()
        ],
    })
    conn = EVMNative(chain="ethereum", chain_id=1, rpc_url=_ETH_RPC,
                     transport=t)
    bal = conn.get_erc20_balance("0x" + "c0" * 20, "0x" + "ab" * 20)
    assert bal == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# provider_spec chain-id defaults


def test_provider_spec_defaults_chain_id_from_map() -> None:
    from nerya.connectors.provider_spec import get_registry

    spec = get_registry().find("arbitrum")
    assert spec is not None and spec.factory is not None, \
        "arbitrum alias must resolve to a spec with a factory"
    conn = spec.factory({"venue": "arbitrum", "rpc_url": "http://x"})
    assert conn.chain_id == EVM_CHAIN_IDS["arbitrum"] == 42161


# ---------------------------------------------------------------------------
# self_custody


def test_native_detection_is_per_chain() -> None:
    # "ETH" on bsc is an ERC-20 there, NOT the native asset: the read must
    # go through eth_call (balanceOf/decimals). If the old blanket native
    # set were still used, eth_getBalance would be called and the fake
    # would raise "unexpected rpc call".
    def _bsc_erc20(body: dict) -> str:
        data = body["params"][0]["data"]
        if data.startswith("0x313ce567"):  # decimals()
            return hex(18)
        if data.startswith("0x70a08231"):  # balanceOf()
            return "0x" + hex(4 * 10 ** 18)[2:].zfill(64)
        raise AssertionError(f"unexpected selector {data[:10]}")

    sc = _self_custody(
        rpc_urls={"bsc": _BSC_RPC},
        transport=FakeRpcTransport({"eth_call": _bsc_erc20}),
    )
    bal = sc.get_balance(chain="bsc", address="0x" + "aa" * 20, token="ETH")
    assert bal.balance == 4.0
    assert bal.decimals == 18

    # "BNB" on bsc IS native -> eth_getBalance path.
    sc2 = _self_custody(
        rpc_urls={"bsc": _BSC_RPC},
        transport=FakeRpcTransport({"eth_getBalance": hex(10 ** 18)}),
    )
    bal2 = sc2.get_balance(chain="bsc", address="0x" + "aa" * 20, token="BNB")
    assert bal2.balance == 1.0
    assert bal2.symbol == "BNB"


def test_swap_refusals() -> None:
    r = _self_custody().swap(chain="bsc", token_in="BNB", token_out="USDT",
                             amount_in=1.0, live=False)
    assert r.ok is False and "live=False" in r.reason

    with pytest.raises(WalletPolicyDenied, match="signer_ref"):
        _self_custody().swap(chain="bsc", token_in="BNB", token_out="USDT",
                             amount_in=1.0, live=True)

    with pytest.raises(WalletPolicyDenied, match="vault://"):
        _self_custody(signer_ref="deadbeef").swap(  # plaintext ref rejected
            chain="bsc", token_in="BNB", token_out="USDT",
            amount_in=1.0, live=True)

    with pytest.raises(WalletPolicyDenied, match="no router configured"):
        _self_custody(signer_ref="vault://k",
                      rpc_urls={"ethereum": "http://x"}).swap(
            chain="ethereum", token_in="0x" + "1" * 40,
            token_out="0x" + "2" * 40, amount_in=1.0, live=True)


def test_bsc_swap_end_to_end_with_fake_rpc(tmp_path) -> None:
    from eth_account import Account

    acct = Account.create()
    ws = _seed_vault(tmp_path, acct.key.hex())
    router = "0x10ed43c718714eb63d5aA57B78B54704E256024E"

    def eth_call(body: dict) -> str:
        to = body["params"][0]["to"].lower()
        data = body["params"][0]["data"]
        if data.startswith("0x313ce567"):  # decimals()
            return hex(18) if to == _WBNB.lower() else hex(6)
        if data.startswith("0xd06ca61f"):  # getAmountsOut -> [in, out]
            return ("0x" + _pad_uint(0x20) + _pad_uint(2)
                    + _pad_uint(10 ** 18) + _pad_uint(4_000_000))
        raise AssertionError(f"unexpected selector {data[:10]} to {to}")

    t = FakeRpcTransport({
        "eth_call": eth_call,
        "eth_chainId": hex(56),
        "eth_gasPrice": hex(3_000_000_000),
        "eth_getTransactionCount": hex(1),
        "eth_sendRawTransaction": "0x" + "f00d" * 16,
        "eth_getTransactionReceipt": {"status": "0x1",
                                      "blockHash": "0x" + "c" * 64,
                                      "blockNumber": hex(100),
                                      "gasUsed": hex(150000)},
    })
    sc = _self_custody(signer_ref="vault://signer-key",
                       rpc_urls={"bsc": _BSC_RPC}, transport=t)
    sc.workspace = ws
    sc.vault_passphrase = "test-pp"

    result = sc.swap(chain="bsc", token_in="BNB", token_out="USDT",
                     amount_in=1.0, slippage_bps=50, live=True, min_out=3.5)

    assert result.ok is True
    assert result.tx_hash == "0x" + "f00d" * 16
    assert result.amount_out == pytest.approx(4.0)
    assert result.extra["confirmed"] is True
    assert any(
        isinstance(c[2], dict) and c[2].get("method") == "eth_sendRawTransaction"
        for c in t.calls
    ), "no raw transaction broadcast"
    # Native input -> no approve: the swap must have gone to the router.
    router_calls = [
        c for c in t.calls
        if isinstance(c[2], dict) and c[2].get("params")
        and isinstance(c[2]["params"][0], dict)
        and str(c[2]["params"][0].get("to", "")).lower() == router.lower()
    ]
    assert router_calls, "swap calldata was not aimed at the router"


def test_solana_swap_end_to_end_with_fake_rpc(tmp_path) -> None:
    from nacl.signing import SigningKey

    sk = SigningKey.generate()
    seed_hex = bytes(sk).hex()
    ws = _seed_vault(tmp_path, seed_hex)

    import base64

    # Minimal well-formed v0 message (F12: the connector verifies the
    # first required signer equals our wallet before signing, so the
    # fake tx must carry the real account list, not an opaque blob).
    pubkey = bytes(sk.verify_key)
    message = (
        bytes([1, 0, 1])           # header: 1 required signer
        + b"\x02"                  # 2 account keys
        + pubkey                   # signer 0 == our wallet (fee payer)
        + bytes(range(32))         # second (readonly) account
        + bytes(32)                # recent blockhash
        + b"\x00"                  # 0 address-table lookups
        + b"\x00"                  # 0 instructions
    )
    tx_b64 = base64.b64encode(b"\x01" + bytes(64) + message).decode()
    t = FakeRpcTransport({
        f"GET {_JUP}/quote": {"outAmount": str(4 * 10 ** 6),
                              "inAmount": str(10 ** 9),
                              "priceImpactPct": "0.01", "slippageBps": 50},
        f"POST {_JUP}/swap": {"swapTransaction": tx_b64},
        "getTokenSupply": {"value": {"decimals": 6, "uiAmount": 1.0}},
        "sendTransaction": "SIG" + "1" * 20,
        "getSignatureStatuses": {"value": [{"confirmationStatus": "finalized",
                                            "slot": 42, "err": None}]},
    })
    sc = _self_custody(signer_ref="vault://signer-key",
                       rpc_urls={"solana": _SOL_RPC}, transport=t)
    sc.workspace = ws
    sc.vault_passphrase = "test-pp"

    result = sc.swap(chain="solana", token_in="SOL", token_out=_USDC_SOL,
                     amount_in=1.0, slippage_bps=50, live=True)

    assert result.ok is True
    assert result.tx_hash.startswith("SIG")
    assert result.extra["confirmed"] is True
    assert result.amount_out == pytest.approx(4.0)


def test_solana_quote_refuses_zero_output() -> None:
    t = FakeRpcTransport({
        f"GET {_JUP}/quote": {"outAmount": "0"},
        "getTokenSupply": {"value": {"decimals": 6, "uiAmount": 1.0}},
    })
    sc = _self_custody(rpc_urls={"solana": _SOL_RPC}, transport=t)
    with pytest.raises(WalletQuoteError):
        sc.quote(chain="solana", token_in="SOL", token_out=_USDC_SOL,
                 amount_in=1.0, slippage_bps=50)


def test_bsc_token_resolution() -> None:
    from nerya.wallet.providers.self_custody import SelfCustodyWallet

    resolve = SelfCustodyWallet._resolve_bsc_token
    assert resolve("USDT").lower().startswith("0x55d3")
    assert resolve("bnb").lower().startswith("0xbb4c")
    assert resolve("0x" + "ab" * 20) == "0x" + "ab" * 20
    with pytest.raises(WalletPolicyDenied):
        resolve("SOMERANDOMTICKER")


# ---------------------------------------------------------------------------
# swap_approval hardening


def _patch_provider(monkeypatch, quote: dict):
    from nerya.wallet import swap_approval as sa

    class _P:
        def quote(self, **kw):
            return WalletQuote(
                provider="self_custody", chain="bsc", token_in="BNB",
                token_out="USDT", amount_in=1.0,
                expected_out=quote["expected_out"], min_out=quote["min_out"],
                slippage_bps=50,
            )

        def swap(self, **kw):
            return WalletSwapResult(
                provider="self_custody", chain="bsc", ok=True,
                tx_hash="0x" + "aa" * 32, amount_in=1.0, amount_out=4.0,
                extra=dict(kw),
            )

    monkeypatch.setattr(sa, "_provider", lambda config, request: _P())


def test_request_approval_rejects_degenerate_quotes(tmp_path, monkeypatch) -> None:
    from nerya.wallet.swap_approval import request_approval

    cfg = _make_config(tmp_path)
    request = {"provider": "self_custody", "chain": "bsc",
               "token_in": "BNB", "token_out": "USDT", "amount_in": 1.0,
               "slippage_bps": 50}
    for bad in ({"expected_out": 0, "min_out": 0},
                {"expected_out": 4.0, "min_out": 0},
                {"expected_out": 4.0, "min_out": -1},
                {"expected_out": 4.0, "min_out": 9.0}):
        with pytest.raises(ValueError, match="refusing to freeze"):
            request_approval(cfg, request=request, quote=bad,
                             actor_id="tester")


def test_execute_refuses_zero_floor_and_expiry(tmp_path, monkeypatch) -> None:
    from nerya.wallet.swap_approval import execute_frozen_swap

    cfg = _make_config(tmp_path)
    cfg.data.setdefault("runtime", {})["live_trading_enabled"] = True
    request = {"provider": "self_custody", "chain": "bsc",
               "token_in": "BNB", "token_out": "USDT", "amount_in": 1.0,
               "slippage_bps": 50}

    _patch_provider(monkeypatch, {"expected_out": 4.0, "min_out": 0.0})
    out = execute_frozen_swap(cfg, request=request,
                              approved_quote={"expected_out": 4.0,
                                              "min_out": 0.0},
                              approval_id_value="a1")
    assert out["ok"] is False
    assert out["error"] == "approval_quote_floor_missing"

    _patch_provider(monkeypatch, {"expected_out": 4.0, "min_out": 3.5})
    import time as _time

    expired = execute_frozen_swap(
        cfg, request=request,
        approved_quote={"expected_out": 4.0, "min_out": 3.5},
        approval_id_value="a2",
        expires_at=_time.time() - 10,
    )
    assert expired["ok"] is False and expired["error"] == "approval_expired"


def test_execute_happy_path_carries_floor(tmp_path, monkeypatch) -> None:
    from nerya.wallet import swap_approval as sa
    from nerya.wallet.swap_approval import execute_frozen_swap

    cfg = _make_config(tmp_path)
    cfg.data.setdefault("runtime", {})["live_trading_enabled"] = True
    request = {"provider": "self_custody", "chain": "bsc",
               "token_in": "BNB", "token_out": "USDT", "amount_in": 1.0,
               "slippage_bps": 50}
    captured: dict[str, Any] = {}

    class _P:
        def quote(self, **kw):
            return WalletQuote(provider="self_custody", chain="bsc",
                               token_in="BNB", token_out="USDT",
                               amount_in=1.0, expected_out=4.1, min_out=3.8,
                               slippage_bps=50)

        def swap(self, **kw):
            captured.update(kw)
            return WalletSwapResult(provider="self_custody", chain="bsc",
                                    ok=True, tx_hash="0xh", amount_in=1.0,
                                    amount_out=4.0)

    monkeypatch.setattr(sa, "_provider", lambda config, request: _P())
    out = execute_frozen_swap(cfg, request=request,
                              approved_quote={"expected_out": 4.0,
                                              "min_out": 3.5},
                              approval_id_value="a3")
    assert out["ok"] is True
    assert captured.get("min_out") == 3.5
    assert captured.get("live") is True


# ---------------------------------------------------------------------------
# SecretVault + registry


def test_vault_warns_on_default_passphrase(tmp_path, monkeypatch, caplog) -> None:
    import nerya.security.secrets as sec

    monkeypatch.setattr(sec, "_default_pp_warned", False)
    monkeypatch.delenv("NERYA_VAULT_PASSPHRASE", raising=False)
    with caplog.at_level("WARNING", logger="nerya.security.secrets"):
        v = sec.SecretVault.open(tmp_path / "secrets.enc")
        assert v.passphrase == sec._DEFAULT_PASSPHRASE
    assert any("NERYA_VAULT_PASSPHRASE" in r.message for r in caplog.records)


def test_vault_corrupt_file_sets_load_error(tmp_path, caplog) -> None:
    import nerya.security.secrets as sec

    p = tmp_path / "secrets.enc"
    p.write_bytes(b"not-json-at-all")
    with caplog.at_level("ERROR", logger="nerya.security.secrets"):
        v = sec.SecretVault.open(p, passphrase="whatever")
    assert v.load_error, "corrupt vault must surface load_error"
    assert any("could not be loaded" in r.message for r in caplog.records)


def test_registry_resolve_raises_on_unreadable_vault(tmp_path) -> None:
    from nerya.wallet.errors import WalletError
    from nerya.wallet.registry import _resolve

    p = tmp_path / "vault" / "secrets.enc"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"garbage")
    with pytest.raises(WalletError, match="could not be"):
        _resolve("vault://x", tmp_path, "pp")


def test_wallet_registry_cache_is_config_aware() -> None:
    from nerya.wallet.registry import WalletRegistry

    reg = WalletRegistry()
    a = reg.get("self_custody", {"signer_ref": "vault://a"})
    b = reg.get("self_custody", {"signer_ref": "vault://b"})
    assert a is not b
    assert reg.get("self_custody", {"signer_ref": "vault://a"}) is a
    reg.invalidate("self_custody")
    c = reg.get("self_custody", {"signer_ref": "vault://a"})
    assert c is not a


# ---------------------------------------------------------------------------
# Second-pass review fixes (vault destruction guard + floor enforcement)


def test_vault_put_refuses_to_destroy_unreadable_vault(tmp_path) -> None:
    import nerya.security.secrets as sec
    from nerya.core.errors import SecretAccessDenied

    vp = tmp_path / "secrets.enc"
    good = sec.SecretVault.open(vp, passphrase="right-pp")
    good.put(name="keeper", value="keep-me", kind="opaque", scope=["wallet"])

    wrong = sec.SecretVault.open(vp, passphrase="WRONG-pp")
    assert wrong.load_error, "wrong passphrase must surface load_error"
    with pytest.raises(SecretAccessDenied, match="refusing to overwrite"):
        wrong.put(name="new", value="x", kind="opaque", scope=["wallet"])

    # The original credential must still be intact and readable.
    again = sec.SecretVault.open(vp, passphrase="right-pp")
    assert again.resolve("keeper") == "keep-me"


def test_solana_swap_refuses_below_approved_floor_before_broadcast(tmp_path) -> None:
    sk = bytes.fromhex("11" * 32)
    ws = _seed_vault(tmp_path, sk.hex())
    t = FakeRpcTransport({
        f"GET {_JUP}/quote": {"outAmount": str(4 * 10 ** 6),
                              "inAmount": str(10 ** 9)},
        "getTokenSupply": {"value": {"decimals": 6, "uiAmount": 1.0}},
    })
    sc = _self_custody(signer_ref="vault://signer-key",
                       rpc_urls={"solana": _SOL_RPC}, transport=t)
    sc.workspace = ws
    sc.vault_passphrase = "test-pp"
    with pytest.raises(WalletPolicyDenied, match="below the approved floor"):
        sc.swap(chain="solana", token_in="SOL", token_out=_USDC_SOL,
                amount_in=1.0, slippage_bps=50, live=True, min_out=5.0)
    # Refusal must happen BEFORE the swap assembly / broadcast.
    assert not any("sendTransaction" in c[1] or "POST" == c[0] and
                   "/swap" in c[1] for c in t.calls)


def test_bsc_swap_encodes_approved_floor_as_wei(tmp_path, monkeypatch) -> None:
    from eth_account import Account

    acct = Account.create()
    ws = _seed_vault(tmp_path, acct.key.hex())
    captured: dict[str, Any] = {}
    import nerya.connectors.bsc_native as bsc_mod

    real_swap = bsc_mod.BSCNative.swap

    def spy_swap(self, **kw):
        captured.update(kw)
        return {"tx_hash": "0x" + "aa" * 16, "confirmed": True,
                "nonce": 1, "quote": {"amount_out": 4.0, "path": []}}

    monkeypatch.setattr(bsc_mod.BSCNative, "swap", spy_swap)

    def eth_call(body: dict) -> str:
        to = body["params"][0]["to"].lower()
        data = body["params"][0]["data"]
        if data.startswith("0x313ce567"):
            return hex(18) if to == _WBNB.lower() else hex(6)
        if data.startswith("0xd06ca61f"):
            return ("0x" + _pad_uint(0x20) + _pad_uint(2)
                    + _pad_uint(10 ** 18) + _pad_uint(4_000_000))
        raise AssertionError(f"unexpected selector {data[:10]}")

    t = FakeRpcTransport({
        "eth_call": eth_call,
        "eth_chainId": hex(56),
        "eth_gasPrice": hex(3_000_000_000),
        "eth_getTransactionCount": hex(1),
    })
    sc = _self_custody(signer_ref="vault://signer-key",
                       rpc_urls={"bsc": _BSC_RPC}, transport=t)
    sc.workspace = ws
    sc.vault_passphrase = "test-pp"
    sc.swap(chain="bsc", token_in="BNB", token_out="USDT",
            amount_in=1.0, slippage_bps=50, live=True, min_out=3.99)
    # slippage floor = 4_000_000 * 9950 // 10_000 = 3_980_000;
    # approved floor = ceil(3.99 * 1e6) = 3_990_000 -> the STRICTER wins.
    assert captured["amount_out_min_wei"] == 3_990_000

    sc.swap(chain="bsc", token_in="BNB", token_out="USDT",
            amount_in=1.0, slippage_bps=50, live=True, min_out=3.5)
    # approved 3_500_000 is looser than the slippage floor 3_980_000.
    assert captured["amount_out_min_wei"] == 3_980_000
    assert real_swap is not None


def test_meaningful_wallet_cfg_ignores_default_chains() -> None:
    from nerya.api.routes_wallet import _meaningful_wallet_cfg as m_routes
    from nerya.wallet.swap_approval import _meaningful_wallet_cfg as m_swap
    from nerya.cli.commands.wallet import _meaningful_provider_cfg as m_cli

    default_only = {"chains": ["bsc", "solana"]}
    for fn in (m_routes, m_swap, m_cli):
        assert fn(default_only) is False, (
            "the shipped `chains` default must not count as a configured "
            "legacy block (it defeats the wallet.providers binding fallback)"
        )
        assert fn({"chains": ["bsc"], "signer_ref": "vault://k"}) is True
