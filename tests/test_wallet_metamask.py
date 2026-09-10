"""Regression coverage for the MetaMask agent wallet provider.

- BIP-44 derivation must match what MetaMask shows (well-known public
  test vectors for the Foundry/Hardhat mnemonic — not a real wallet).
- seed / private key live only in the vault (vault:// refs in config);
  plaintext refs are rejected for live signing.
- readiness fails loudly when the seed no longer derives the pinned
  address (wrong-wallet protection).
- EVM-only: Solana is refused on every method with a clear message.
- live BSC swap signs with the seed-derived key end-to-end (scripted
  fake transport, no network).
- `nerya wallet create metamask` mints a seed into the vault, writes
  the wallet.providers binding, and prints the address (+ seed once).

No test in this file touches the network.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

import nerya.agent  # noqa: F401  (break benign circular import)
from nerya.connectors.bsc_native import _pad_uint
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.security.secrets import SecretVault
from nerya.wallet.errors import (
    WalletError,
    WalletPolicyDenied,
)
from nerya.wallet.providers.metamask import (
    MetaMaskWallet,
    derive_address,
    generate_seed,
)
from nerya.wallet.registry import PROVIDERS, build_provider

pytestmark = pytest.mark.smoke

# Public Hardhat/Foundry test mnemonic — safe to embed, funded only on
# local dev chains. Addresses are the canonical BIP-44 vectors.
TEST_SEED = "test test test test test test test test test test test junk"
ADDR_0 = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
ADDR_1 = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"

_BSC_RPC = "http://bsc-fake"
_WBNB = "0xbb4cdb9cbdb36b01bd1cbaebf2de08d9173bc095c"


class FakeRpcTransport:
    """Scripted JSON-RPC/HTTP transport (same semantics as the pipeline
    test's fake: callables, pop-lists or constants; GET/POST keys return
    raw bodies)."""

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
            raise AssertionError(f"unexpected rpc call: {name}")
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
            return 200, result
        return 200, {"jsonrpc": "2.0", "id": 1, "result": result}


def _make_config(tmp_path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    return Config(paths=WorkspacePaths(root=tmp_path), data=data)


def _seed_vault(tmp_path, name: str, value: str) -> None:
    vp = tmp_path / "vault" / "secrets.enc"
    vp.parent.mkdir(parents=True, exist_ok=True)
    v = SecretVault.open(vp, passphrase="test-pp")
    v.put(name=name, value=value, kind="mnemonic", scope=["wallet"])


# ---------------------------------------------------------------------------
# registry / framework adaptation


def test_registry_lists_metamask() -> None:
    entry = PROVIDERS["metamask"]
    names = [f["name"] for f in entry["credential_fields"]]
    for field in ("seed", "private_key", "address", "address_index",
                  "rpc_urls.bsc", "rpc_urls.ethereum"):
        assert field in names
    # Secrets must be marked sensitive so the dashboard vaultifies them.
    by_name = {f["name"]: f for f in entry["credential_fields"]}
    assert by_name["seed"]["sensitive"] is True
    assert by_name["private_key"]["sensitive"] is True
    assert by_name["address"]["sensitive"] is False


def test_build_provider_returns_metamask(tmp_path) -> None:
    p = build_provider("metamask", {"address_index": 2}, workspace=tmp_path)
    assert isinstance(p, MetaMaskWallet)
    assert p.address_index == 2
    assert "solana" not in p.chains


def test_cli_valid_providers_includes_metamask() -> None:
    from nerya.cli.commands.wallet import _valid_providers

    assert "metamask" in _valid_providers()


# ---------------------------------------------------------------------------
# BIP-44 derivation


def test_bip44_vectors_match_metamask() -> None:
    assert derive_address(TEST_SEED, 0) == ADDR_0
    assert derive_address(TEST_SEED, 1) == ADDR_1
    assert derive_address(TEST_SEED, 2) == "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"


def test_generate_seed_lengths_and_entropy() -> None:
    a = generate_seed(12)
    b = generate_seed(24)
    assert len(a.split()) == 12
    assert len(b.split()) == 24
    assert a != b
    with pytest.raises(ValueError):
        generate_seed(13)


def test_derive_address_rejects_bad_phrase() -> None:
    with pytest.raises(WalletError):
        derive_address("not a real seed phrase at all", 0)


# ---------------------------------------------------------------------------
# readiness / wrong-wallet protection


def test_readiness_fails_on_address_mismatch(tmp_path) -> None:
    _seed_vault(tmp_path, "mm-seed", TEST_SEED)
    p = build_provider(
        "metamask",
        {"seed_ref": "vault://mm-seed", "address_index": 0,
         "address": ADDR_1},  # seed derives ADDR_0, operator pinned ADDR_1
        workspace=tmp_path, vault_passphrase="test-pp",
    )
    r = p.readiness()
    assert r.ready is False
    assert "does not match" in r.reason


def test_readiness_ready_on_match_or_unpinned(tmp_path) -> None:
    _seed_vault(tmp_path, "mm-seed", TEST_SEED)
    pinned = build_provider(
        "metamask",
        {"seed_ref": "vault://mm-seed", "address_index": 0,
         "address": ADDR_0},
        workspace=tmp_path, vault_passphrase="test-pp",
    )
    assert pinned.readiness().ready is True
    assert pinned.derived_address() == ADDR_0

    unpinned = build_provider(
        "metamask", {"seed_ref": "vault://mm-seed"},
        workspace=tmp_path, vault_passphrase="test-pp",
    )
    r = unpinned.readiness()
    assert r.ready is True
    # No address pinned, so no verification happened — reason stays empty.
    assert r.reason == ""


def test_readiness_read_only_note_without_refs(tmp_path) -> None:
    p = build_provider("metamask", {}, workspace=tmp_path)
    r = p.readiness()
    assert r.ready is True
    assert "read-only" in r.reason


# ---------------------------------------------------------------------------
# EVM-only refusals


def test_solana_refused_on_every_method(tmp_path) -> None:
    p = build_provider("metamask", {"seed_ref": "vault://nope"},
                       workspace=tmp_path)
    with pytest.raises(WalletPolicyDenied, match="EVM"):
        p.get_balance(chain="solana", address="0x" + "a" * 20, token="SOL")
    with pytest.raises(WalletPolicyDenied, match="EVM"):
        p.quote(chain="solana", token_in="SOL", token_out="USDC",
                amount_in=1.0)
    with pytest.raises(WalletPolicyDenied, match="EVM"):
        p.swap(chain="solana", token_in="SOL", token_out="USDC",
               amount_in=1.0, live=True)


def test_plaintext_seed_ref_rejected_on_live_swap(tmp_path) -> None:
    p = build_provider(
        "metamask", {"seed": TEST_SEED},  # NOT vaultified
        workspace=tmp_path,
    )
    with pytest.raises(WalletPolicyDenied, match="vault://"):
        p.swap(chain="bsc", token_in="BNB", token_out="USDT",
               amount_in=1.0, live=True)


def test_live_swap_without_any_ref_refused(tmp_path) -> None:
    p = build_provider("metamask", {}, workspace=tmp_path)
    with pytest.raises(WalletPolicyDenied):
        p.swap(chain="bsc", token_in="BNB", token_out="USDT",
               amount_in=1.0, live=True)


# ---------------------------------------------------------------------------
# live BSC swap signing with the seed-derived key


def _bsc_script() -> dict[str, Any]:
    def eth_call(body: dict) -> str:
        to = body["params"][0]["to"].lower()
        data = body["params"][0]["data"]
        if data.startswith("0x313ce567"):  # decimals()
            return hex(18) if to == _WBNB else hex(6)
        if data.startswith("0xd06ca61f"):  # getAmountsOut -> [in, out]
            return ("0x" + _pad_uint(0x20) + _pad_uint(2)
                    + _pad_uint(10 ** 18) + _pad_uint(4_000_000))
        raise AssertionError(f"unexpected selector {data[:10]}")

    return {
        "eth_call": eth_call,
        "eth_chainId": hex(56),
        "eth_gasPrice": hex(3_000_000_000),
        "eth_getTransactionCount": hex(1),
        "eth_sendRawTransaction": "0x" + "f00d" * 16,
        "eth_getTransactionReceipt": {"status": "0x1",
                                      "blockHash": "0x" + "c" * 64,
                                      "blockNumber": hex(100),
                                      "gasUsed": hex(150000)},
    }


def test_bsc_swap_signs_with_seed_derived_key(tmp_path) -> None:
    _seed_vault(tmp_path, "mm-seed", TEST_SEED)
    t = FakeRpcTransport(_bsc_script())
    p = build_provider(
        "metamask",
        {"seed_ref": "vault://mm-seed", "address_index": 0,
         "rpc_urls": {"bsc": _BSC_RPC}},
        workspace=tmp_path, vault_passphrase="test-pp",
    )
    p.transport = t
    result = p.swap(chain="bsc", token_in="BNB", token_out="USDT",
                    amount_in=1.0, slippage_bps=50, live=True, min_out=3.5)
    assert result.ok is True
    assert result.tx_hash == "0x" + "f00d" * 16
    assert result.amount_out == pytest.approx(4.0)
    assert result.extra["confirmed"] is True
    # The swap's recipient defaults to the seed-derived MetaMask address.
    assert result.extra["recipient"].lower() == ADDR_0.lower()


def test_bsc_swap_with_private_key_ref(tmp_path) -> None:
    from eth_account import Account

    key = Account.create().key.hex()
    _seed_vault(tmp_path, "mm-key", key)
    p = build_provider(
        "metamask",
        {"private_key_ref": "vault://mm-key",
         "rpc_urls": {"bsc": _BSC_RPC}},
        workspace=tmp_path, vault_passphrase="test-pp",
    )
    p.transport = FakeRpcTransport(_bsc_script())
    result = p.swap(chain="bsc", token_in="BNB", token_out="USDT",
                    amount_in=1.0, slippage_bps=50, live=True)
    assert result.ok is True
    # Raw-key mode has no seed to verify the pinned address against.
    assert p.derived_address() is None


# ---------------------------------------------------------------------------
# CLI: nerya wallet create metamask


class _PrintCapture:
    def __init__(self):
        self.rows: list[Any] = []

    def __call__(self, data):
        self.rows.append(data)


def _fake_cli_client(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    client = SimpleNamespace(config=cfg)
    monkeypatch.setattr("nerya.cli.commands.wallet._client",
                        lambda ws, profile=None: client)
    capture = _PrintCapture()
    monkeypatch.setattr("nerya.cli.commands.wallet._print", capture)
    return capture


def test_cli_create_metamask_full_flow(tmp_path, monkeypatch) -> None:
    capture = _fake_cli_client(tmp_path, monkeypatch)
    monkeypatch.setenv("NERYA_VAULT_PASSPHRASE", "test-pp")

    from nerya.cli.commands import wallet as wallet_cli

    args = argparse.Namespace(provider="metamask", words=12, index=1,
                              wallet_id="mm_main", no_reveal=False,
                              workspace=None, profile=None)
    rc = wallet_cli.cmd_wallet_create(args)
    assert rc == 0
    out = capture.rows[-1]
    assert out["ok"] is True
    assert out["address_index"] == 1
    # The printed seed derives exactly the printed address (MetaMask
    # parity), and it is printed exactly once by default.
    assert derive_address(out["seed"], 1) == out["address"]
    assert out["seed_ref"].startswith("vault://")

    # Seed is really in the vault, wallet-scoped.
    v = SecretVault.open(tmp_path / "vault" / "secrets.enc",
                         passphrase="test-pp")
    assert v.resolve(out["seed_ref"].split("vault://", 1)[-1],
                     required_scope="wallet") == out["seed"]

    # Binding written to nerya.yml and resolvable through the registry.
    from nerya.core import yaml_io

    conf = yaml_io.load(tmp_path / "nerya.yml", default={})
    binding = conf["wallet"]["providers"]["mm_main"]
    assert binding["provider"] == "metamask"
    assert binding["config"]["seed_ref"] == out["seed_ref"]
    p = build_provider("metamask", binding["config"],
                       workspace=tmp_path, vault_passphrase="test-pp")
    assert p.readiness().ready is True
    assert p.derived_address() == out["address"]


def test_cli_create_no_reveal_keeps_seed_hidden(tmp_path, monkeypatch) -> None:
    capture = _fake_cli_client(tmp_path, monkeypatch)
    monkeypatch.setenv("NERYA_VAULT_PASSPHRASE", "test-pp")

    from nerya.cli.commands import wallet as wallet_cli

    args = argparse.Namespace(provider="metamask", words=24, index=0,
                              wallet_id=None, no_reveal=True,
                              workspace=None, profile=None)
    rc = wallet_cli.cmd_wallet_create(args)
    assert rc == 0
    out = capture.rows[-1]
    assert "seed" not in out
    # Still recoverable from the vault.
    v = SecretVault.open(tmp_path / "vault" / "secrets.enc",
                         passphrase="test-pp")
    words = v.resolve(out["seed_ref"].split("vault://", 1)[-1])
    assert len(words.split()) == 24


def test_argparse_accepts_create_metamask() -> None:
    from nerya.cli.commands.wallet import register

    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="cmd", required=True))
    args = parser.parse_args(["wallet", "create", "metamask",
                              "--words", "12", "--index", "0"])
    assert args.provider == "metamask"
    assert args.words == 12
    with pytest.raises(SystemExit):
        parser.parse_args(["wallet", "create", "okx_os"])  # metamask-only


def test_auth_start_routes_metamask_to_no_login(tmp_path) -> None:
    from nerya.api.routes_wallet import _auth_start_args

    args, action, required = _auth_start_args("metamask", {})
    assert args is None
    assert action == "no_login_required"
    assert required == []
