"""Alpaca Markets connector.

Wraps ``alpaca-py`` for both market data and trading. The same SDK
serves equities, ETFs, options, and crypto; we expose a single
connector that reads ``data_kind`` from the account config (defaults
to ``crypto`` if the market id starts with ``ALPACA:CRYPTO_``).

Markets follow ``ALPACA:<symbol>`` (equities, e.g. ``ALPACA:AAPL``)
or ``ALPACA:CRYPTO_<symbol>`` (crypto, e.g. ``ALPACA:CRYPTO_BTC/USD``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .base import Balance, CEXConnectorBase, OrderAck, Ticker


@dataclass
class AlpacaCredentials:
    api_key: str = ""
    api_secret: str = ""
    paper: bool = True
    base_url: str = ""


class AlpacaConnector(CEXConnectorBase):
    venue = "ALPACA"
    kind = "broker"

    def __init__(
        self,
        credentials: AlpacaCredentials | None = None,
        *,
        live: bool = False,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.credentials = credentials or AlpacaCredentials()
        self.live = bool(live)
        self.config = dict(config or {})
        self._trading_client = None
        self._stock_data = None
        self._crypto_data = None

    # ------------------------------------------------------------ private
    def _ensure_clients(self) -> None:
        if self._trading_client is not None:
            return
        try:
            from alpaca.trading.client import TradingClient  # type: ignore
            from alpaca.data.historical import (  # type: ignore
                StockHistoricalDataClient, CryptoHistoricalDataClient,
            )
        except Exception as exc:
            raise RuntimeError(
                "Alpaca connector needs `alpaca-py` "
                "(`pip install alpaca-py`)."
            ) from exc
        paper_mode = self.credentials.paper or not self.live
        self._trading_client = TradingClient(
            api_key=self.credentials.api_key,
            secret_key=self.credentials.api_secret,
            paper=paper_mode,
        )
        self._stock_data = StockHistoricalDataClient(
            api_key=self.credentials.api_key,
            secret_key=self.credentials.api_secret,
        )
        self._crypto_data = CryptoHistoricalDataClient(
            api_key=self.credentials.api_key,
            secret_key=self.credentials.api_secret,
        )

    @staticmethod
    def _is_crypto_market(market: str) -> bool:
        body = market.split(":", 1)[-1]
        return body.upper().startswith("CRYPTO_") or "/" in body

    @staticmethod
    def _market_to_symbol(market: str) -> str:
        body = market.split(":", 1)[-1]
        if body.upper().startswith("CRYPTO_"):
            return body.split("_", 1)[-1]
        return body

    # ------------------------------------------------------------ public
    def get_ticker(self, market: str) -> Ticker:
        self._ensure_clients()
        symbol = self._market_to_symbol(market)
        if self._is_crypto_market(market):
            from alpaca.data.requests import CryptoLatestQuoteRequest  # type: ignore
            req = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
            quotes = self._crypto_data.get_crypto_latest_quote(req)
        else:
            from alpaca.data.requests import StockLatestQuoteRequest  # type: ignore
            req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quotes = self._stock_data.get_stock_latest_quote(req)
        q = quotes.get(symbol) if isinstance(quotes, dict) else quotes
        bid = float(getattr(q, "bid_price", 0.0) or 0.0)
        ask = float(getattr(q, "ask_price", 0.0) or 0.0)
        mid = (bid + ask) / 2 if bid and ask else max(bid, ask)
        spread_bps = ((ask - bid) / mid * 10_000.0) if mid else 0.0
        return Ticker(
            market=market, bid=bid, ask=ask, mid=mid, last=mid,
            spread_bps=spread_bps,
            ts_ms=int(time.time() * 1000), venue=self.venue,
        )

    def get_klines(self, market: str, *, interval: str = "1m",
                    limit: int = 100) -> list[list[Any]]:
        self._ensure_clients()
        from datetime import datetime, timedelta, timezone

        symbol = self._market_to_symbol(market)
        is_crypto = self._is_crypto_market(market)

        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit  # type: ignore
        unit_map = {
            "1m": (1, TimeFrameUnit.Minute), "5m": (5, TimeFrameUnit.Minute),
            "15m": (15, TimeFrameUnit.Minute), "30m": (30, TimeFrameUnit.Minute),
            "1h": (1, TimeFrameUnit.Hour), "4h": (4, TimeFrameUnit.Hour),
            "1d": (1, TimeFrameUnit.Day),
        }
        amt, unit = unit_map.get(interval, (1, TimeFrameUnit.Minute))
        tf = TimeFrame(amt, unit)

        end = datetime.now(tz=timezone.utc)
        # heuristic lookback: limit * bar size + buffer
        if unit == TimeFrameUnit.Day:
            start = end - timedelta(days=int(limit) + 1)
        elif unit == TimeFrameUnit.Hour:
            start = end - timedelta(hours=amt * (int(limit) + 1))
        else:
            start = end - timedelta(minutes=amt * (int(limit) + 1))

        if is_crypto:
            from alpaca.data.requests import CryptoBarsRequest  # type: ignore
            req = CryptoBarsRequest(
                symbol_or_symbols=symbol, timeframe=tf,
                start=start, end=end, limit=int(limit),
            )
            resp = self._crypto_data.get_crypto_bars(req)
        else:
            from alpaca.data.requests import StockBarsRequest  # type: ignore
            req = StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=tf,
                start=start, end=end, limit=int(limit),
            )
            resp = self._stock_data.get_stock_bars(req)

        bars = resp.data.get(symbol, []) if hasattr(resp, "data") else []
        out: list[list[Any]] = []
        for b in bars[-int(limit):]:
            ts = getattr(b, "timestamp", None)
            ts_ms = int(ts.timestamp() * 1000) if ts is not None else 0
            out.append([
                ts_ms, float(b.open), float(b.high), float(b.low),
                float(b.close), float(getattr(b, "volume", 0.0)),
            ])
        return out

    def get_balances(self) -> list[Balance]:
        self._ensure_clients()
        account = self._trading_client.get_account()
        cash = float(getattr(account, "cash", 0.0))
        equity = float(getattr(account, "portfolio_value", cash))
        currency = getattr(account, "currency", "USD")
        return [Balance(asset=currency, free=cash, locked=0.0, total=equity)]

    def place_order(self, *, market: str, side: str, order_type: str,
                     size: float, price: float | None = None,
                     client_order_id: str | None = None,
                     time_in_force: str = "GTC") -> OrderAck:
        if not self.live:
            return super().place_order(
                market=market, side=side, order_type=order_type, size=size,
                price=price, client_order_id=client_order_id,
                time_in_force=time_in_force,
            )
        self._ensure_clients()
        from alpaca.trading.requests import (  # type: ignore
            MarketOrderRequest, LimitOrderRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce  # type: ignore

        symbol = self._market_to_symbol(market)
        side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        tif = getattr(TimeInForce, (time_in_force or "GTC").upper(),
                      TimeInForce.GTC)
        otype = order_type.lower()
        if otype == "limit":
            req = LimitOrderRequest(
                symbol=symbol, qty=float(size), side=side_enum,
                limit_price=float(price or 0.0), time_in_force=tif,
                client_order_id=client_order_id,
            )
        else:
            req = MarketOrderRequest(
                symbol=symbol, qty=float(size), side=side_enum,
                time_in_force=tif, client_order_id=client_order_id,
            )
        result = self._trading_client.submit_order(req)
        return OrderAck(
            order_id=str(getattr(result, "id", "")),
            client_order_id=str(client_order_id or getattr(result, "client_order_id", "") or ""),
            status=str(getattr(result, "status", "submitted")).lower(),
            market=market, side=side,
            price=float(price) if price is not None else None,
            size=float(size),
            filled=float(getattr(result, "filled_qty", 0.0) or 0.0),
            avg_price=float(getattr(result, "filled_avg_price", 0.0) or 0.0),
            raw={"alpaca_id": str(getattr(result, "id", ""))},
        )


__all__ = ["AlpacaConnector", "AlpacaCredentials"]
