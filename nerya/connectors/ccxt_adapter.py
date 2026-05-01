"""ccxt-backed CEX connector.

Adapts the large ``ccxt`` unified API surface to Nerya's small
``Connector`` ABC so Nerya doesn't have to hand-roll venue-specific
REST wrappers. Supported venues = every CEX / DEX aggregator ccxt
supports (binance, okx, bybit, kraken, huobi, gate, mexc, bitget,
kucoin, coinbase, hyperliquid, …).

Usage flow:

    conn = CcxtConnector(
        exchange_id="binance",
        credentials=CEXCredentials(api_key=..., api_secret=...),
        live=False,
    )
    conn.get_ticker("BTC/USDT")      # public read, no key needed
    conn.get_klines("BTC/USDT")      # public read
    conn.get_balances()              # requires live=True + creds
    conn.place_order(...)            # requires live=True + creds

``live=False`` mirrors the native connectors: public market data still
works, but every private call raises :class:`TradingError` so the
execution engine's gate is preserved even when creds happen to exist.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import TradingError
from .base import Balance, CEXConnectorBase, OrderAck, Ticker
from .cex_base import CEXCredentials


def _lazy_ccxt():
    """Import ccxt lazily so the rest of Nerya still works without it."""
    try:
        import ccxt  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise TradingError(
            "ccxt is not installed — run `pip install ccxt` to use the "
            "unified CEX connector"
        ) from exc
    return ccxt


def supported_exchanges() -> list[str]:
    """Return every exchange id ccxt knows about.

    Returns ``[]`` when ccxt is not importable so callers can still boot.
    """
    try:
        import ccxt  # type: ignore
    except Exception:
        return []
    return sorted(getattr(ccxt, "exchanges", []))


@dataclass
class CcxtConnector(CEXConnectorBase):
    """ccxt-backed CEX connector wrapping the unified public + private API.

    Market symbols are passed through untouched — ccxt expects the
    ``BASE/QUOTE`` format (e.g. ``BTC/USDT``). A handful of convenience
    normalisations cover the ``BINANCE:BTCUSDT`` / ``btcusdt`` shapes
    the rest of Nerya happens to use.
    """

    exchange_id: str = "binance"
    venue: str = ""
    credentials: CEXCredentials = field(default_factory=CEXCredentials)
    live: bool = False
    options: dict[str, Any] = field(default_factory=dict)
    _client: Any = None

    def __post_init__(self) -> None:
        if not self.venue:
            self.venue = self.exchange_id.upper()

    # --------------------------------------------------------- client
    def _build_client(self) -> Any:
        ccxt = _lazy_ccxt()
        klass = getattr(ccxt, self.exchange_id, None)
        if klass is None:
            raise TradingError(
                f"ccxt has no exchange id {self.exchange_id!r}; known: "
                f"{', '.join(getattr(ccxt, 'exchanges', [])[:8])}..."
            )
        params: dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": 15_000,
        }
        if self.credentials.api_key:
            params["apiKey"] = self.credentials.api_key
        if self.credentials.api_secret:
            params["secret"] = self.credentials.api_secret
        if self.credentials.api_passphrase:
            params["password"] = self.credentials.api_passphrase
        if self.options:
            params["options"] = dict(self.options)
        return klass(params)

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    # --------------------------------------------------------- helpers
    def _normalise_symbol(self, market: str) -> str:
        tail = market.split(":", 1)[-1]
        if "/" in tail:
            return tail.upper()
        # crude USDT/USD/BUSD split
        s = tail.upper().replace("-", "")
        for q in ("USDT", "USDC", "BUSD", "USD", "USDS"):
            if s.endswith(q) and len(s) > len(q):
                return f"{s[:-len(q)]}/{q}"
        return tail.upper()

    def _check_live_and_keys(self) -> None:
        if not self.live:
            raise TradingError(
                f"{self.venue}: live trading disabled for this connector "
                f"(set accounts.live=true + runtime.live_trading_enabled=true)"
            )
        if not self.credentials.api_key or not self.credentials.api_secret:
            raise TradingError(
                f"{self.venue}: api credentials not resolved from vault"
            )

    # --------------------------------------------------------- public
    def get_ticker(self, market: str) -> Ticker:
        sym = self._normalise_symbol(market)
        try:
            raw = self.client.fetch_ticker(sym)
        except Exception as exc:
            raise TradingError(f"{self.venue} fetch_ticker {sym} failed: {exc}") from exc
        bid = float(raw.get("bid") or 0.0)
        ask = float(raw.get("ask") or 0.0)
        last = float(raw.get("last") or raw.get("close") or 0.0)
        mid = (bid + ask) / 2 if (bid and ask) else (bid or ask or last)
        spread_bps = ((ask - bid) / mid) * 10_000 if mid else 0.0
        ts_ms = int(raw.get("timestamp") or (time.time() * 1000))
        return Ticker(
            market=market, bid=bid, ask=ask, mid=mid, last=last,
            spread_bps=spread_bps, ts_ms=ts_ms, venue=self.venue,
        )

    def get_order_book(self, market: str) -> dict[str, Any]:
        sym = self._normalise_symbol(market)
        try:
            raw = self.client.fetch_order_book(sym)
        except Exception as exc:
            raise TradingError(f"{self.venue} fetch_order_book {sym} failed: {exc}") from exc
        bids = raw.get("bids") or []
        asks = raw.get("asks") or []
        return {
            "market": market,
            "bid": float(bids[0][0]) if bids else 0.0,
            "ask": float(asks[0][0]) if asks else 0.0,
            "bids": [[float(p), float(s)] for p, s in bids[:20]],
            "asks": [[float(p), float(s)] for p, s in asks[:20]],
            "venue": self.venue,
            "ts_ms": int(raw.get("timestamp") or (time.time() * 1000)),
        }

    def get_klines(
        self, market: str, *, interval: str = "1m", limit: int = 100,
    ) -> list[list[Any]]:
        sym = self._normalise_symbol(market)
        try:
            ohlcv = self.client.fetch_ohlcv(sym, timeframe=interval, limit=limit)
        except Exception as exc:
            raise TradingError(f"{self.venue} fetch_ohlcv {sym} failed: {exc}") from exc
        return [list(row) for row in (ohlcv or [])]

    # --------------------------------------------------------- private
    def get_balances(self) -> list[Balance]:
        self._check_live_and_keys()
        try:
            raw = self.client.fetch_balance()
        except Exception as exc:
            raise TradingError(f"{self.venue} fetch_balance failed: {exc}") from exc
        out: list[Balance] = []
        totals = raw.get("total") or {}
        frees = raw.get("free") or {}
        useds = raw.get("used") or {}
        for asset, total in totals.items():
            try:
                t = float(total or 0)
            except Exception:
                continue
            if t == 0 and not float(frees.get(asset) or 0) and not float(useds.get(asset) or 0):
                continue
            out.append(Balance(
                asset=asset,
                free=float(frees.get(asset) or 0.0),
                locked=float(useds.get(asset) or 0.0),
                total=t,
            ))
        return out

    def place_order(self, *, market: str, side: str, order_type: str,
                    size: float, price: float | None = None,
                    client_order_id: str | None = None,
                    time_in_force: str = "GTC") -> OrderAck:
        self._check_live_and_keys()
        sym = self._normalise_symbol(market)
        params: dict[str, Any] = {}
        if client_order_id:
            params["clientOrderId"] = client_order_id
        if time_in_force and order_type.lower() == "limit":
            params["timeInForce"] = time_in_force
        try:
            raw = self.client.create_order(
                sym, order_type.lower(), side.lower(), float(size),
                None if price is None else float(price), params,
            )
        except Exception as exc:
            raise TradingError(f"{self.venue} place_order failed: {exc}") from exc
        return OrderAck(
            order_id=str(raw.get("id") or ""),
            client_order_id=str(raw.get("clientOrderId") or client_order_id or ""),
            status=_map_order_status(raw.get("status")),
            market=market, side=side.lower(),
            price=float(raw.get("price") or price or 0) or None,
            size=float(raw.get("amount") or size or 0) or None,
            filled=float(raw.get("filled") or 0) or None,
            avg_price=float(raw.get("average") or 0) or None,
            raw=dict(raw),
        )

    def cancel_order(self, *, market: str, order_id: str) -> OrderAck:
        self._check_live_and_keys()
        sym = self._normalise_symbol(market)
        try:
            raw = self.client.cancel_order(order_id, sym)
        except Exception as exc:
            raise TradingError(f"{self.venue} cancel_order failed: {exc}") from exc
        return OrderAck(
            order_id=str(raw.get("id") or order_id),
            client_order_id=str(raw.get("clientOrderId") or ""),
            status=_map_order_status(raw.get("status") or "canceled"),
            market=market, side=str(raw.get("side") or ""),
            raw=dict(raw),
        )

    def get_order(self, *, market: str, order_id: str) -> OrderAck:
        self._check_live_and_keys()
        sym = self._normalise_symbol(market)
        try:
            raw = self.client.fetch_order(order_id, sym)
        except Exception as exc:
            raise TradingError(f"{self.venue} fetch_order failed: {exc}") from exc
        return OrderAck(
            order_id=str(raw.get("id") or order_id),
            client_order_id=str(raw.get("clientOrderId") or ""),
            status=_map_order_status(raw.get("status")),
            market=market, side=str(raw.get("side") or ""),
            price=float(raw.get("price") or 0) or None,
            size=float(raw.get("amount") or 0) or None,
            filled=float(raw.get("filled") or 0) or None,
            avg_price=float(raw.get("average") or 0) or None,
            raw=dict(raw),
        )


def _map_order_status(raw: str | None) -> str:
    s = str(raw or "").lower()
    if s in ("open", "new"):
        return "new"
    if s in ("closed", "filled"):
        return "filled"
    if s in ("partial", "partially_filled", "partiallyfilled"):
        return "partial"
    if s in ("canceled", "cancelled"):
        return "canceled"
    if s == "rejected":
        return "rejected"
    return s or "new"


__all__ = ["CcxtConnector", "supported_exchanges"]
