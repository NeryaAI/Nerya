"""Audit-fix regression tests for the okx_os and byreal wallet providers.

Covers: OKX per-token balance routing, honest portfolio-USD labelling,
unsigned-swap ok=False, WalletTransportError for HTTP failures, decimals
handling; Byreal native-SOL-only balances, WalletQuoteError on unparseable
quotes, receiver honesty, and non-zero CLI exit handling. All transports
are stubbed — no network, no real CLI.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.wallet.errors import (
    WalletPolicyDenied,
    WalletQuoteError,
    WalletTransportError,
)
from nerya.wallet.providers.byreal import ByrealWallet
from nerya.wallet.providers.okx_os import OkxOsWallet


pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# OKX OS
# ---------------------------------------------------------------------------

_TOKEN_BALANCES_DOC = {
    "code": "0",
    "data": [
        {
            "tokenAssets": [
                {
                    "tokenSymbol": "USDC",
                    "tokenContractAddress": "0xa0b86991usdc",
                    "balance": "2500000",
                    "decimals": 6,
                },
                {
                    "tokenSymbol": "WETH",
                    "tokenContractAddress": "0xc02aaaaaweth",
                    "balance": "1500000000000000000",
                    "decimals": 18,
                },
            ]
        }
    ],
}

_TOTAL_VALUE_DOC = {"code": "0", "data": [{"totalValue": "1234.56"}]}

_UNSIGNED_SWAP_DOC = {
    "code": "0",
    "data": [
        {
            "tx": {
                "data": "0xdeadbeef",
                "to": "0xrouter",
                "gasPrice": "1000000000",
            },
            "toTokenAmount": "2000000",
        }
    ],
}


def _okx_provider(monkeypatch, docs: dict) -> tuple[OkxOsWallet, list[dict]]:
    """Build an OKX provider with `_signed_get` stubbed from `docs`.

    `docs` maps request path -> response doc. Every call is recorded as
    ``{"path": ..., "params": ...}`` in the returned list.
    """
    calls: list[dict] = []

    def fake_signed_get(self, path, params):
        calls.append({"path": path, "params": dict(params)})
        if path not in docs:
            raise AssertionError(f"unexpected signed GET {path}")
        return docs[path]

    monkeypatch.setattr(OkxOsWallet, "_signed_get", fake_signed_get)
    provider = OkxOsWallet(
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
        api_project_id="proj",
    )
    return provider, calls


def test_okx_get_balance_token_request_routes_to_token_balances(monkeypatch):
    provider, calls = _okx_provider(
        monkeypatch, {"/api/v5/wallet/asset/token-balances": _TOKEN_BALANCES_DOC}
    )

    bal = provider.get_balance(
        chain="ethereum", address="0xwallet", token="0xA0B86991USDC"
    )

    assert bal.token == "0xA0B86991USDC"
    assert bal.symbol == "USDC"
    assert bal.decimals == 6
    assert bal.balance == pytest.approx(2.5)
    paths = [c["path"] for c in calls]
    assert paths == ["/api/v5/wallet/asset/token-balances"]
    assert calls[0]["params"]["address"] == "0xwallet"
    assert calls[0]["params"]["chainIndex"] == "1"


def test_okx_get_balance_symbol_match_uses_row_decimals(monkeypatch):
    provider, _ = _okx_provider(
        monkeypatch, {"/api/v5/wallet/asset/token-balances": _TOKEN_BALANCES_DOC}
    )

    bal = provider.get_balance(
        chain="ethereum", address="0xwallet", token="weth"
    )

    assert bal.symbol == "WETH"
    assert bal.decimals == 18
    assert bal.balance == pytest.approx(1.5)


@pytest.mark.parametrize("token", ["", "usd", "total"])
def test_okx_get_balance_portfolio_request_only_for_empty_usd_total(
    monkeypatch, token
):
    provider, calls = _okx_provider(
        monkeypatch, {"/api/v5/wallet/asset/total-value-by-address": _TOTAL_VALUE_DOC}
    )

    bal = provider.get_balance(chain="ethereum", address="0xwallet", token=token)

    assert [c["path"] for c in calls] == [
        "/api/v5/wallet/asset/total-value-by-address"
    ]
    assert bal.balance == pytest.approx(1234.56)
    assert bal.symbol == "USD"
    assert bal.token == ""  # honestly labelled, never echoes requested token
    assert bal.decimals == 2


def test_okx_get_balance_token_request_requires_address(monkeypatch):
    provider, calls = _okx_provider(
        monkeypatch, {"/api/v5/wallet/asset/token-balances": _TOKEN_BALANCES_DOC}
    )

    with pytest.raises(WalletPolicyDenied, match="address"):
        provider.get_balance(chain="ethereum", address="", token="USDC")
    assert calls == []


def test_okx_get_balance_unknown_token_raises(monkeypatch):
    provider, _ = _okx_provider(
        monkeypatch, {"/api/v5/wallet/asset/token-balances": _TOKEN_BALANCES_DOC}
    )

    from nerya.wallet.errors import WalletError

    with pytest.raises(WalletError, match="not found"):
        provider.get_balance(chain="ethereum", address="0xwallet", token="0xnope")


def test_okx_quote_records_assumed_and_explicit_decimals(monkeypatch):
    quote_doc = {"data": [{"toTokenAmount": "2000000"}]}
    provider, calls = _okx_provider(
        monkeypatch, {"/api/v5/dex/aggregator/quote": quote_doc}
    )

    # No kwargs: 18 assumed on both sides, recorded in extra.
    q1 = provider.quote(
        chain="ethereum",
        token_in="0xweth",
        token_out="0xusdc",
        amount_in=1.0,
    )
    assert q1.expected_out == pytest.approx(2_000_000 / 10 ** 18)
    assert q1.extra["decimals_assumed"] == 18

    # Explicit decimals: honoured (including 0) and not flagged as assumed.
    q2 = provider.quote(
        chain="ethereum",
        token_in="0xweth",
        token_out="0xusdc",
        amount_in=2.0,
        decimals_in=0,
        decimals_out=6,
    )
    assert q2.expected_out == pytest.approx(2.0)
    assert "decimals_assumed" not in q2.extra
    amount_param = calls[-1]["params"]["amount"]
    assert amount_param == "2"  # 2.0 * 10**0

    assert calls[0]["params"]["amount"] == str(int(1.0 * 10 ** 18))


def test_okx_quote_without_positive_output_raises_quote_error(monkeypatch):
    provider, _ = _okx_provider(
        monkeypatch, {"/api/v5/dex/aggregator/quote": {"data": [{}]}}
    )

    with pytest.raises(WalletQuoteError):
        provider.quote(
            chain="ethereum",
            token_in="0xweth",
            token_out="0xusdc",
            amount_in=1.0,
        )


def test_okx_swap_unsigned_tx_is_not_ok_and_keeps_calldata(monkeypatch):
    provider, calls = _okx_provider(
        monkeypatch, {"/api/v5/dex/aggregator/swap": _UNSIGNED_SWAP_DOC}
    )

    res = provider.swap(
        chain="ethereum",
        token_in="0xweth",
        token_out="0xusdc",
        amount_in=1.0,
        receiver="0xrcpt",
        live=True,
        decimals_out=6,
    )

    assert res.ok is False
    assert res.tx_hash == ""
    assert "signer" in res.reason
    unsigned = res.extra["unsigned_tx"]
    assert unsigned["data"] == "0xdeadbeef"
    assert unsigned["to"] == "0xrouter"
    assert res.amount_out == pytest.approx(2.0)
    assert "decimals_assumed" not in res.extra
    assert calls[0]["params"]["userWalletAddress"] == "0xrcpt"


def test_okx_swap_records_min_out_accounting(monkeypatch):
    provider, _ = _okx_provider(
        monkeypatch, {"/api/v5/dex/aggregator/swap": _UNSIGNED_SWAP_DOC}
    )

    res = provider.swap(
        chain="ethereum",
        token_in="0xweth",
        token_out="0xusdc",
        amount_in=1.0,
        slippage_bps=100,
        receiver="0xrcpt",
        live=True,
        decimals_out=6,
        min_out=1.98,
    )

    # The aggregator API cannot express minOut, so the approved floor is
    # recorded and an honest expected floor is computed from the quote.
    assert res.extra["min_out_requested"] == pytest.approx(1.98)
    assert res.extra["amount_out_min"] == pytest.approx(2.0 * 0.99)


def test_okx_http_failure_raises_transport_error(monkeypatch):
    from nerya.connectors import http as http_mod

    monkeypatch.setattr(
        http_mod.UrllibHttp,
        "request",
        lambda self, *args, **kwargs: (503, {"code": "50013"}),
    )
    provider = OkxOsWallet(
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
        api_project_id="proj",
    )

    with pytest.raises(WalletTransportError, match="503"):
        provider.get_balance(chain="ethereum", address="0xwallet", token="usd")


# ---------------------------------------------------------------------------
# Byreal
# ---------------------------------------------------------------------------

_BALANCES_DATA = {
    "balances": [
        {"symbol": "USDC", "mint": "usdcmint", "uiAmount": 100.0, "decimals": 6},
        {
            "symbol": "SOL",
            "mint": "So11111111111111111111111111111111111111112",
            "uiAmount": 1.25,
            "decimals": 9,
        },
        {"symbol": "BONK", "mint": "bonkmint", "uiAmount": 5_000_000.0, "decimals": 5},
    ]
}


def _byreal_provider(monkeypatch, data) -> tuple[ByrealWallet, list[list[str]]]:
    """Build a Byreal provider with `_run_cli` stubbed to return `data`."""
    calls: list[list[str]] = []

    def fake_run_cli(self, args, *, timeout_s=45.0):
        calls.append(list(args))
        return data

    monkeypatch.setattr(ByrealWallet, "_run_cli", fake_run_cli)
    return ByrealWallet(), calls


def test_byreal_get_balance_empty_token_returns_native_sol_only(monkeypatch):
    provider, calls = _byreal_provider(monkeypatch, _BALANCES_DATA)

    bal = provider.get_balance(chain="solana", address="w1", token="")

    # 100 USDC + 5,000,000 BONK + 1.25 SOL must NOT be summed into "SOL".
    assert bal.balance == pytest.approx(1.25)
    assert bal.symbol == "SOL"
    assert bal.decimals == 9
    assert calls == [["wallet", "balance"]]


def test_byreal_get_balance_usd_total_is_explicit_and_honest(monkeypatch):
    data = {**_BALANCES_DATA, "totalValueUsd": 42.5}
    provider, _ = _byreal_provider(monkeypatch, data)

    bal = provider.get_balance(chain="solana", address="w1", token="usd")

    assert bal.balance == pytest.approx(42.5)
    assert bal.symbol == "USD"
    assert bal.token == ""


def test_byreal_get_balance_specific_token_matches_row(monkeypatch):
    provider, _ = _byreal_provider(monkeypatch, _BALANCES_DATA)

    bal = provider.get_balance(chain="solana", address="w1", token="USDC")

    assert bal.balance == pytest.approx(100.0)
    assert bal.symbol == "USDC"
    assert bal.decimals == 6


def test_byreal_quote_unparseable_output_raises_quote_error(monkeypatch):
    provider, calls = _byreal_provider(
        monkeypatch, {"success": True, "priceImpactBps": 10}
    )

    with pytest.raises(WalletQuoteError):
        provider.quote(
            chain="solana",
            token_in="SOL",
            token_out="USDC",
            amount_in=1.25,
        )
    assert calls[0][0:2] == ["swap", "execute"]
    assert "--dry-run" in calls[0]


def test_byreal_quote_zero_output_raises_quote_error(monkeypatch):
    provider, _ = _byreal_provider(
        monkeypatch, {"expectedOut": "0", "estimatedOut": 0.0}
    )

    with pytest.raises(WalletQuoteError):
        provider.quote(
            chain="solana",
            token_in="SOL",
            token_out="USDC",
            amount_in=1.25,
        )


def test_byreal_quote_parses_positive_output_and_min_out_fallback(monkeypatch):
    provider, _ = _byreal_provider(monkeypatch, {"expectedOut": "42.5"})

    q = provider.quote(
        chain="solana", token_in="SOL", token_out="USDC", amount_in=1.25,
        slippage_bps=100,
    )

    assert q.expected_out == pytest.approx(42.5)
    assert q.min_out == pytest.approx(42.5 * 0.99)
    assert q.extra["decimals_in"] == 9
    assert q.extra["amount_units"] == "ui"


def test_byreal_swap_receiver_ignored_surfaces_in_extra_and_reason(monkeypatch):
    provider, calls = _byreal_provider(
        monkeypatch, {"txid": "tx-1", "outAmount": 99.0}
    )

    res = provider.swap(
        chain="solana",
        token_in="SOL",
        token_out="USDC",
        amount_in=1.25,
        receiver="receiver-1",
        live=True,
    )

    assert res.ok is True
    assert res.tx_hash == "tx-1"
    assert res.extra["receiver_ignored"] == "receiver-1"
    assert "receiver" in res.reason
    assert "keypair" in res.reason
    # No recipient flag may be guessed into the CLI argv.
    flat = " ".join(calls[0]).lower()
    assert "receiver" not in flat
    assert "--to" not in flat


def _byreal_cli_on_disk(tmp_path) -> ByrealWallet:
    cli = tmp_path / "byreal-cli"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    return ByrealWallet(cli_path=str(cli), workspace=str(tmp_path))


def test_byreal_nonzero_exit_with_json_stdout_raises_transport_error(
    tmp_path, monkeypatch
):
    provider = _byreal_cli_on_disk(tmp_path)

    def fake_run(cmd, **kwargs):
        # Parseable JSON on stdout, but the CLI failed — must not pass.
        return SimpleNamespace(
            returncode=1,
            stdout='{"meta": {"partial": true}, "data": null}',
            stderr="boom",
        )

    monkeypatch.setattr(
        "nerya.wallet.providers.byreal.subprocess.run", fake_run
    )

    with pytest.raises(WalletTransportError, match="exited 1"):
        provider.get_balance(chain="solana", address="w1", token="")


def test_byreal_nonzero_exit_with_explicit_success_doc_passes(
    tmp_path, monkeypatch
):
    provider = _byreal_cli_on_disk(tmp_path)

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout='{"success": true, "data": {"balances": []}}',
            stderr="warning: retry succeeded",
        )

    monkeypatch.setattr(
        "nerya.wallet.providers.byreal.subprocess.run", fake_run
    )

    bal = provider.get_balance(chain="solana", address="w1", token="")
    assert bal.balance == 0.0


def test_byreal_cli_success_false_raises_transport_error(tmp_path, monkeypatch):
    provider = _byreal_cli_on_disk(tmp_path)

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout='{"success": false, "error": {"message": "no wallet"}}',
            stderr="",
        )

    monkeypatch.setattr(
        "nerya.wallet.providers.byreal.subprocess.run", fake_run
    )

    with pytest.raises(WalletTransportError, match="no wallet"):
        provider.get_balance(chain="solana", address="w1", token="")


def test_byreal_timeout_raises_transport_error(tmp_path, monkeypatch):
    import subprocess

    provider = _byreal_cli_on_disk(tmp_path)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    monkeypatch.setattr(
        "nerya.wallet.providers.byreal.subprocess.run", fake_run
    )

    with pytest.raises(WalletTransportError, match="timed out"):
        provider.get_balance(chain="solana", address="w1", token="")


def test_byreal_readiness_flags_missing_keypair(tmp_path):
    cli = tmp_path / "byreal-cli"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    provider = ByrealWallet(
        cli_path=str(cli), keypair_path=str(tmp_path / "nope.json")
    )

    r = provider.readiness()

    assert r.ready is False
    assert any(m.startswith("keypair:") for m in r.missing)
    assert "keypair" in r.reason


def test_byreal_readiness_ready_when_keypair_exists(tmp_path):
    cli = tmp_path / "byreal-cli"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    keypair = tmp_path / "id.json"
    keypair.write_text("[]", encoding="utf-8")
    provider = ByrealWallet(cli_path=str(cli), keypair_path=str(keypair))

    r = provider.readiness()

    assert r.ready is True
    assert r.missing == []
