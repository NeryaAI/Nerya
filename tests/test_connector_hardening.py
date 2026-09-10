"""Hardening tests for the connectors-layer audit fixes (2026-09).

Covers:

* F2 — a failed ``load_markets`` must not be cached as ``{}`` forever;
  ``place_order`` fails closed when the markets map is empty
* F3 — ccxt ``NoChange`` ("leverage not modified" / "margin mode already
  set") is success, not a kill switch for every leveraged order
* F4 — transport-level create_order failures carry ``ambiguous=True`` so
  the executor tracks + polls instead of marking the order rejected
* E3/C3 — symbol normalisation: perp-preferring adapters resolve bare
  BASE/QUOTE to the linear swap; explicit contract suffixes stay verbatim
* E8 — lowercase tif codes map to ccxt's uppercase expectations and
  ``post_only`` becomes the postOnly flag
* F7 — ``binanceusdm`` / ``binancecoinm`` resolve to the perpetual
  provider specs, not the spot binance one
* F9 — polymarket reads ``credentials.extras["signed_order"]`` and the
  spec no longer advertises place_order
* F11 — base-unit conversion is exact (Decimal), not float-truncated
* F12 — the Solana v0 signer refuses a tx whose first required signer is
  not our keypair

No network anywhere: ccxt is replaced by fake client doubles, Solana
fakes reuse the minimal v0 message layout from the wallet pipeline tests.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

import nerya.agent  # noqa: F401  (break benign circular import)
from nerya.connectors.ccxt_adapter import CcxtConnector
from nerya.connectors.cex_base import CEXCredentials
from nerya.core.errors import TradingError

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# Fakes


class _FakeCcxtClient:
    """Configurable ccxt client double for the write path."""

    def __init__(
        self,
        markets: dict[str, Any] | None = None,
        *,
        load_failures: int = 0,
    ) -> None:
        self.markets = dict(markets or {})
        self.load_failures = load_failures
        self.load_calls = 0
        self.create_order_calls: list[tuple] = []
        self.create_order_response: dict[str, Any] = {
            "id": "ex-1",
            "clientOrderId": "coid-1",
            "status": "closed",
            "symbol": "BTC/USDT",
            "side": "buy",
            "amount": 1.0,
            "filled": 1.0,
            "average": 100.0,
        }
        self.raise_on_create: Exception | None = None

    def load_markets(self):
        self.load_calls += 1
        if self.load_failures > 0:
            self.load_failures -= 1
            raise RuntimeError(f"transient load failure #{self.load_calls}")
        return self.markets

    def amount_to_precision(self, symbol, amount):
        return str(float(amount))

    def price_to_precision(self, symbol, price):
        return f"{float(price):.2f}"

    def create_order(self, symbol, otype, side, amount, price, params):
        self.create_order_calls.append(
            (symbol, otype, side, amount, price, dict(params))
        )
        if self.raise_on_create is not None:
            raise self.raise_on_create
        return dict(self.create_order_response)


def _connector(client: _FakeCcxtClient) -> CcxtConnector:
    conn = CcxtConnector(
        exchange_id="bybit",
        credentials=CEXCredentials(api_key="k", api_secret="s"),
        live=True,
    )
    conn._client = client  # bypass real ccxt
    return conn


_SPOT_MARKETS = {"BTC/USDT": {"spot": True, "limits": {}}}
_PERP_MARKETS = {
    "SOL/USDT:USDT": {"swap": True, "contract": True, "contractSize": 1.0},
}


# ---------------------------------------------------------------------------
# F2 — failed load_markets must not poison the cache


def test_failed_load_markets_is_not_cached() -> None:
    client = _FakeCcxtClient(_PERP_MARKETS, load_failures=1)
    conn = _connector(client)
    with pytest.raises(TradingError, match="markets_unavailable"):
        _ = conn.markets
    # The failure was NOT cached: the next read retries and succeeds.
    assert "SOL/USDT:USDT" in conn.markets
    assert client.load_calls == 2
    assert conn._markets is client.markets


def test_place_order_fails_closed_on_empty_markets() -> None:
    client = _FakeCcxtClient({})  # load "succeeds" but returns nothing
    conn = _connector(client)
    with pytest.raises(TradingError, match="markets_unavailable"):
        conn.place_order(
            market="BYBIT:SOLUSDT", side="buy", order_type="market", size=1.0,
        )
    assert client.create_order_calls == []


# ---------------------------------------------------------------------------
# F3 — NoChange is success


def test_no_change_leverage_and_margin_are_success() -> None:
    from ccxt.base.errors import MarginModeAlreadySet, NoChange

    client = _FakeCcxtClient(_PERP_MARKETS)

    def leverage_not_modified(leverage, symbol):
        raise NoChange("leverage not modified")

    def margin_already_set(mode, symbol):
        raise MarginModeAlreadySet("margin mode not modified")

    client.set_leverage = leverage_not_modified
    client.set_margin_mode = margin_already_set
    conn = _connector(client)
    # Must not raise: Bybit answers "not modified" on every subsequent
    # order once the value is active.
    conn._ensure_leverage_and_margin(
        "SOL/USDT:USDT", leverage=10, margin_mode="isolated",
    )


def test_real_leverage_failure_still_fails_closed() -> None:
    client = _FakeCcxtClient(_PERP_MARKETS)

    def boom(leverage, symbol):
        raise RuntimeError("risk limit exceeded")

    client.set_leverage = boom
    conn = _connector(client)
    with pytest.raises(TradingError, match="failed to set leverage"):
        conn._ensure_leverage_and_margin(
            "SOL/USDT:USDT", leverage=10, margin_mode=None,
        )


# ---------------------------------------------------------------------------
# F4 — transport failures are ambiguous, definitive rejects are not


def test_place_order_flags_timeout_as_ambiguous() -> None:
    from ccxt.base.errors import RequestTimeout

    client = _FakeCcxtClient(_SPOT_MARKETS)
    client.raise_on_create = RequestTimeout("request timed out")
    conn = _connector(client)
    with pytest.raises(TradingError) as excinfo:
        conn.place_order(
            market="BTC/USDT", side="buy", order_type="market", size=1.0,
        )
    assert excinfo.value.ambiguous is True


def test_place_order_keeps_definitive_rejects_unambiguous() -> None:
    from ccxt.base.errors import InsufficientFunds

    client = _FakeCcxtClient(_SPOT_MARKETS)
    client.raise_on_create = InsufficientFunds("not enough USDT")
    conn = _connector(client)
    with pytest.raises(TradingError) as excinfo:
        conn.place_order(
            market="BTC/USDT", side="buy", order_type="market", size=1.0,
        )
    assert excinfo.value.ambiguous is False


# ---------------------------------------------------------------------------
# E3/C3 — symbol normalisation


def test_symbol_normalisation_perp_vs_spot() -> None:
    spot = CcxtConnector(exchange_id="bybit")
    perp = CcxtConnector(exchange_id="bybit", prefer_perpetual=True)
    # Spot adapter: bare symbols stay spot.
    assert spot._normalise_symbol("SOLUSDT") == "SOL/USDT"
    assert spot._normalise_symbol("BYBIT:SOLUSDT") == "SOL/USDT"
    assert spot._normalise_symbol("BTC/USDT") == "BTC/USDT"
    # Perp adapter: bare BASE/QUOTE resolves to the linear swap.
    assert perp._normalise_symbol("SOLUSDT") == "SOL/USDT:USDT"
    assert perp._normalise_symbol("BYBIT:SOLUSDT") == "SOL/USDT:USDT"
    assert perp._normalise_symbol("BTC/USDT") == "BTC/USDT:USDT"
    # Nerya tooling's BYBIT_PERPETUAL prefix forces linear even on a
    # spot-capable adapter.
    assert spot._normalise_symbol("BYBIT_PERPETUAL:SOLUSDT") == "SOL/USDT:USDT"
    assert perp._normalise_symbol("BYBIT_PERPETUAL:SOLUSDT") == "SOL/USDT:USDT"
    # Explicit contract suffixes stay verbatim on any adapter.
    assert spot._normalise_symbol("SOL/USDT:USDT") == "SOL/USDT:USDT"
    assert spot._normalise_symbol("BYBIT:SOL/USDT:USDT") == "SOL/USDT:USDT"
    assert perp._normalise_symbol("SOL/USDT:USDT") == "SOL/USDT:USDT"


def test_perp_adapter_keeps_derivatives_params_on_bare_symbol() -> None:
    client = _FakeCcxtClient(_PERP_MARKETS)
    conn = CcxtConnector(
        exchange_id="bybit",
        credentials=CEXCredentials(api_key="k", api_secret="s"),
        live=True,
        prefer_perpetual=True,
    )
    conn._client = client
    conn.place_order(
        market="SOLUSDT", side="sell", order_type="market", size=10.0,
        reduce_only=True,
    )
    sym, _otype, _side, _amount, _price, params = client.create_order_calls[-1]
    assert sym == "SOL/USDT:USDT"
    assert params.get("reduceOnly") is True


def test_force_perp_settle_currency_follows_quote() -> None:
    """R2C2: USD-quoted pairs are the inverse / coin-margined family and
    settle in the BASE coin (bybit's real inverse symbol is BTC/USD:BTC,
    not BTC/USD:USD which hits BadSymbol); USDT/USDC pairs stay linear."""
    perp = CcxtConnector(exchange_id="bybit", prefer_perpetual=True)
    assert perp._normalise_symbol("BTCUSD") == "BTC/USD:BTC"
    assert perp._normalise_symbol("BYBIT_PERPETUAL:BTCUSD") == "BTC/USD:BTC"
    assert perp._normalise_symbol("BTC/USD") == "BTC/USD:BTC"
    # USDT/USDC-quoted pairs keep the linear settle = quote behaviour.
    assert perp._normalise_symbol("ETHUSDT") == "ETH/USDT:USDT"
    assert perp._normalise_symbol("BYBIT_PERPETUAL:ETHUSDT") == "ETH/USDT:USDT"
    assert perp._normalise_symbol("SOL/USDC") == "SOL/USDC:USDC"
    # Explicit contract suffixes still stay verbatim on any adapter.
    assert perp._normalise_symbol("BTC/USD:BTC") == "BTC/USD:BTC"
    spot = CcxtConnector(exchange_id="bybit")
    assert spot._normalise_symbol("BTCUSD") == "BTC/USD"


# ---------------------------------------------------------------------------
# E8 — time-in-force mapping


def test_time_in_force_mapping() -> None:
    client = _FakeCcxtClient(_SPOT_MARKETS)
    conn = _connector(client)
    for tif, expected in (("gtc", "GTC"), ("ioc", "IOC"), ("fok", "FOK")):
        conn.place_order(
            market="BTC/USDT", side="buy", order_type="limit",
            size=1.0, price=100.0, time_in_force=tif,
        )
        _sym, _otype, _side, _amount, _price, params = \
            client.create_order_calls[-1]
        assert params.get("timeInForce") == expected

    conn.place_order(
        market="BTC/USDT", side="buy", order_type="limit",
        size=1.0, price=100.0, time_in_force="post_only",
    )
    _, _, _, _, _, params = client.create_order_calls[-1]
    assert params.get("postOnly") is True
    assert "timeInForce" not in params

    # Unknown values pass through for the venue to validate.
    conn.place_order(
        market="BTC/USDT", side="buy", order_type="limit",
        size=1.0, price=100.0, time_in_force="DAY",
    )
    _, _, _, _, _, params = client.create_order_calls[-1]
    assert params.get("timeInForce") == "DAY"


# ---------------------------------------------------------------------------
# F6 — min notional + ccxt precision


def test_min_notional_enforced() -> None:
    markets = {
        "BTC/USDT": {
            "spot": True,
            "limits": {"amount": {"min": 0.0001}, "cost": {"min": 10.0}},
        },
    }
    client = _FakeCcxtClient(markets)
    conn = _connector(client)
    with pytest.raises(TradingError, match="min_notional"):
        conn.place_order(
            market="BTC/USDT", side="buy", order_type="limit",
            size=0.001, price=100.0,  # notional 0.1 << 10
        )
    assert client.create_order_calls == []


def test_min_notional_respects_contract_size() -> None:
    """R2C1: swap amounts reach the min-notional check in *contracts*
    (place_order already divided by contractSize), so the order value is
    amount * contractSize * price — not amount * price."""
    markets = {
        "BTC/USD:BTC": {
            "swap": True, "contract": True, "contractSize": 0.001,
            "limits": {"cost": {"min": 10.0}},
        },
    }
    client = _FakeCcxtClient(markets)
    conn = _connector(client)
    # 0.01 BTC = 10 contracts; true order value 10 * 0.001 * 100 = 1.0
    # << 10. The old amount*price check saw 10 * 100 = 1000 and passed.
    with pytest.raises(TradingError, match="min_notional"):
        conn.place_order(
            market="BTC/USD:BTC", side="buy", order_type="limit",
            size=0.01, price=100.0,
        )
    assert client.create_order_calls == []

    # contractSize=1 venues (spot, linear USDT swaps): unchanged.
    linear_markets = {
        "SOL/USDT:USDT": {
            "swap": True, "contract": True, "contractSize": 1.0,
            "limits": {"cost": {"min": 10.0}},
        },
    }
    linear_conn = _connector(_FakeCcxtClient(linear_markets))
    linear_conn.place_order(
        market="SOL/USDT:USDT", side="buy", order_type="limit",
        size=0.2, price=100.0,  # 0.2 * 1 * 100 = 20 >= 10 → passes
    )
    assert len(linear_conn._client.create_order_calls) == 1


def test_amount_rounding_uses_amount_to_precision() -> None:
    client = _FakeCcxtClient(_SPOT_MARKETS)

    def truncate_to_lot(symbol, amount):
        # Venue step 0.5: 0.7 must round DOWN to 0.5.
        return str(int(float(amount) * 2) / 2)

    client.amount_to_precision = truncate_to_lot
    conn = _connector(client)
    conn.place_order(
        market="BTC/USDT", side="buy", order_type="market", size=0.7,
    )
    _, _, _, amount, _price, _params = client.create_order_calls[-1]
    assert amount == 0.5


# ---------------------------------------------------------------------------
# F7 — binanceusdm / binancecoinm resolve to the perpetual specs


def test_binance_perp_aliases_do_not_resolve_to_spot() -> None:
    from nerya.connectors.provider_spec import get_registry

    reg = get_registry()
    usdm = reg.find("binanceusdm")
    assert usdm is not None and usdm.id == "binance_perpetual"
    coinm = reg.find("binancecoinm")
    assert coinm is not None and coinm.id == "binance_coinm_perpetual"
    # Spot names still hit the spot spec.
    assert reg.find("binance").id == "binance"
    assert reg.find("binance_spot").id == "binance"

    # Building the perp venue yields a perp-prefering ccxt adapter.
    conn = usdm.factory({"venue": "binanceusdm"})
    assert conn.exchange_id == "binanceusdm"
    assert conn.prefer_perpetual is True


# ---------------------------------------------------------------------------
# F9 — polymarket extras + spec honesty


def test_polymarket_reads_extras_signed_order() -> None:
    from nerya.connectors.polymarket import PolymarketConnector

    class _T:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def request(self, method, url, *, headers=None, params=None,
                    body=None, timeout=15.0):
            self.calls.append((method, url))
            if method == "GET" and url.endswith("/markets"):
                return 200, [{"clobTokenIds": '["12345678901234567890"]'}]
            return 200, {"orderID": "pm-1", "status": "live"}

    transport = _T()
    conn = PolymarketConnector(
        credentials=CEXCredentials(
            api_key="0xaddr",
            extras={"signed_order": {"sig": "0xabc"}},
        ),
        live=True, transport=transport,
    )
    ack = conn.place_order(
        market="POLYMARKET:some-slug", side="buy", order_type="limit",
        size=5.0, price=0.5,
    )
    assert ack.order_id == "pm-1"
    assert any(m == "POST" for m, _ in transport.calls)

    # A signed order in the misspelled ``extra`` field must not work —
    # the spec no longer advertises writes either (F9).
    from nerya.connectors.provider_spec import get_registry

    spec = get_registry().find("polymarket")
    assert spec is not None
    assert spec.supports.get("place_order") is False


# ---------------------------------------------------------------------------
# F11 — exact base-unit conversion


def test_decimal_base_unit_conversion_is_exact() -> None:
    from nerya.connectors.bsc_native import _to_base_units

    assert _to_base_units(0.1, 18) == 100_000_000_000_000_000
    # float math gives 8099999999999999488 here; Decimal is exact.
    assert _to_base_units(8.1, 18) == 8_100_000_000_000_000_000
    # int inputs keep their behaviour.
    assert _to_base_units(5, 18) == 5_000_000_000_000_000_000
    assert _to_base_units("1.5", 6) == 1_500_000


def test_solana_quote_scales_amount_in_exactly() -> None:
    """6-decimal token, amount 8.1 → raw 8100000 (float gives 8099999)."""
    from nerya.wallet.providers.self_custody import SelfCustodyWallet

    captured: dict[str, Any] = {}

    class _T:
        def request(self, method, url, *, headers=None, params=None,
                    body=None, timeout=15.0):
            if isinstance(body, dict) and body.get("jsonrpc"):
                assert body["method"] == "getTokenSupply"
                return 200, {"jsonrpc": "2.0", "id": 1,
                             "result": {"value": {"decimals": 6}}}
            captured.update(params or {})
            return 200, {"outAmount": "1000000", "inAmount": "8100000"}

    sc = SelfCustodyWallet(
        rpc_urls={"solana": "http://sol-fake"}, transport=_T(),
    )
    quote = sc.quote(
        chain="solana", token_in="0x" + "a" * 20, token_out="0x" + "b" * 20,
        amount_in=8.1, slippage_bps=50,
    )
    assert captured["amount"] == "8100000"
    assert quote.extra["real_quote"] is True


# ---------------------------------------------------------------------------
# F12 — Solana v0 signer verification


def _v0_tx(signer_pubkey: bytes) -> str:
    message = (
        bytes([1, 0, 1])           # header: 1 required signer
        + b"\x02"                  # 2 account keys
        + signer_pubkey            # signer 0 == fee payer
        + bytes(range(32))         # readonly account
        + bytes(32)                # recent blockhash
        + b"\x00"                  # 0 address-table lookups
        + b"\x00"                  # 0 instructions
    )
    return base64.b64encode(b"\x01" + bytes(64) + message).decode()


def test_solana_signer_refuses_foreign_first_signer() -> None:
    from nacl.signing import SigningKey

    from nerya.connectors.solana_native import _sign_solana_v0_tx

    ours = SigningKey.generate()
    foreign = SigningKey.generate()
    tx = _v0_tx(bytes(foreign.verify_key))
    with pytest.raises(TradingError, match="first required signer"):
        _sign_solana_v0_tx(tx, bytes(ours).hex())


def test_solana_signer_signs_own_tx() -> None:
    from nacl.signing import SigningKey

    from nerya.connectors.solana_native import _sign_solana_v0_tx

    ours = SigningKey.generate()
    signed = _sign_solana_v0_tx(_v0_tx(bytes(ours.verify_key)), bytes(ours).hex())
    raw = base64.b64decode(signed)
    assert raw[1:65] != bytes(64), "signature slot 0 must be filled"


def test_solana_swap_refuses_receiver_different_from_wallet() -> None:
    from nacl.signing import SigningKey

    from nerya.wallet.errors import WalletPolicyDenied
    from nerya.wallet.providers.self_custody import SelfCustodyWallet

    class _T:
        def request(self, method, url, *, headers=None, params=None,
                    body=None, timeout=15.0):
            if isinstance(body, dict) and body.get("jsonrpc"):
                raise AssertionError("no rpc expected for SOL→SOL")
            return 200, {"outAmount": "1000000", "inAmount": "1000000000"}

    seed_hex = bytes(SigningKey.generate()).hex()
    sc = SelfCustodyWallet(
        signer_ref="vault://k",
        rpc_urls={"solana": "http://sol-fake"},
        transport=_T(),
        config={"signer_ref": "vault://k"},
    )
    with pytest.raises(WalletPolicyDenied, match="not supported"):
        sc._solana_swap(
            key=seed_hex, token_in="SOL", token_out="SOL",
            amount_in=1.0, slippage_bps=50,
            receiver="ReceiverAddressIsNotTheWallet1111111111111",
            min_out=None,
        )


# ---------------------------------------------------------------------------
# R2C3 — contract-suffixed markets need a derivatives-capable venue


def _risk_gate_for(tmp_root, *, venue: str, market: str):
    """Build a minimal paper workspace + gate and evaluate one intent."""
    from copy import deepcopy

    from nerya.core import yaml_io
    from nerya.core.config import Config, DEFAULT_CONFIG
    from nerya.core.paths import WorkspacePaths
    from nerya.trading.intents import TradeIntent
    from nerya.trading.risk import RiskGate

    cfg = Config(paths=WorkspacePaths(root=tmp_root), data=deepcopy(DEFAULT_CONFIG))
    paths = cfg.paths
    paths.db.parent.mkdir(parents=True, exist_ok=True)
    yaml_io.dump(
        paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "acct_1",
                    "exchange": venue, "venue": venue, "mode": "paper",
                    "status": "active", "initial_balance_usd": 50_000,
                    "permissions": {
                        "read_balances": True, "place_order": True,
                        "cancel_order": True,
                    },
                },
            ],
        },
    )
    yaml_io.dump(
        paths.strategy("alpha") / "strategy.yml",
        {
            "id": "alpha", "status": "paper", "account_id": "acct_1",
            "markets": [market],
            "paper_trading_enabled": True, "live_trading_enabled": False,
        },
    )
    yaml_io.dump(
        paths.strategy("alpha") / "limits.yml",
        {
            "allowed_markets": [market],
            "min_confidence": 0, "max_stale_seconds": 60,
            "approval_threshold_usd": 0,
            "max_single_order_usd": 100_000,
        },
    )
    intent = TradeIntent.new(
        strategy_id="alpha", account_id="acct_1",
        market=market, side="buy",
        size=1_000, size_unit="usd", order_type="market",
        confidence=1.0,
        # strategy automation source — "agent" always stops at the
        # Approval Gate regardless of account mode.
        source="strategy_runtime",
    )
    return RiskGate(cfg).evaluate(
        intent,
        market_snapshot={"price": 100.0, "age_s": 1, "source": "test"},
    )


def test_risk_gate_contract_suffix_market_venue_capability(tmp_path) -> None:
    """R2C3: a bare explicit contract symbol carries no venue prefix, so
    the venue-mismatch gate can't catch it — an account on a spot-only
    venue must be rejected up front while derivatives venues pass."""
    # Spot-only venue: rejected with the dedicated reason.
    decision = _risk_gate_for(
        tmp_path / "spot", venue="binance", market="SOL/USDT:USDT",
    )
    assert decision.decision == "reject"
    assert any(
        r.startswith("market_instrument_unsupported:SOL/USDT:USDT")
        for r in decision.reasons
    ), decision.reasons

    # Derivatives venue: the suffix market passes the gate cleanly.
    decision = _risk_gate_for(
        tmp_path / "perp", venue="bybit_perpetual", market="SOL/USDT:USDT",
    )
    assert decision.decision == "allow", decision.reasons

    # Mock stays exempt from the instrument check (paper rehearsal).
    decision = _risk_gate_for(
        tmp_path / "mock", venue="mock", market="SOL/USDT:USDT",
    )
    assert decision.decision == "allow", decision.reasons
