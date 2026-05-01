"""MetaTrader 5 broker connector.

The MetaQuotes ``MetaTrader5`` Python package speaks to a running MT5
terminal over local IPC (Windows-only). This connector wraps the
package, automatically initialising the terminal and logging in.

Order routing model
-------------------

MT5 markets in Nerya use the ``MT5:<symbol>`` convention. Symbols are
broker-specific: ``MT5:EURUSD``, ``MT5:XAUUSD``, ``MT5:US500.cash``,
``MT5:NAS100``, etc. The connector forwards the symbol verbatim; the
caller is responsible for using the broker's exact spelling.

Read methods (ticker / klines / balances) are safe to call even when
``live=False``; ``place_order`` is gated through the standard
``Connector.place_order`` paper guard.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .base import Balance, CEXConnectorBase, OrderAck, Ticker


@dataclass
class MT5Credentials:
    server: str = ""
    login: int = 0
    password: str = ""
    path: str = ""


# Constants shadowed in case the SDK isn't installed; the actual
# integers come from the MetaTrader5 module at runtime.
_DEFAULT_TIMEFRAMES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}


class MT5Connector(CEXConnectorBase):
    venue = "MT5"
    kind = "broker"

    def __init__(
        self,
        credentials: MT5Credentials | None = None,
        *,
        live: bool = False,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.credentials = credentials or MT5Credentials()
        self.live = bool(live)
        self.config = dict(config or {})
        self._mt5 = None
        self._initialised = False

    # ------------------------------------------------------------ private
    def _ensure_initialised(self):
        if self._initialised and self._mt5 is not None:
            return self._mt5
        try:
            import MetaTrader5 as _mt5  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "MT5 connector needs the MetaTrader5 package "
                "(`pip install MetaTrader5` on Windows)."
            ) from exc
        kwargs: dict[str, Any] = {}
        if self.credentials.path:
            kwargs["path"] = self.credentials.path
        if self.credentials.login:
            kwargs["login"] = int(self.credentials.login)
        if self.credentials.password:
            kwargs["password"] = self.credentials.password
        if self.credentials.server:
            kwargs["server"] = self.credentials.server
        if not _mt5.initialize(**kwargs):
            err = _mt5.last_error()
            raise RuntimeError(f"MT5 initialize failed: {err!r}")
        self._mt5 = _mt5
        self._initialised = True
        return _mt5

    def shutdown(self) -> None:
        try:
            if self._mt5 is not None:
                self._mt5.shutdown()
        finally:
            self._mt5 = None
            self._initialised = False

    @staticmethod
    def _strip_prefix(market: str) -> str:
        if ":" in market:
            return market.split(":", 1)[-1]
        return market

    # ------------------------------------------------------------ public
    def get_ticker(self, market: str) -> Ticker:
        mt5 = self._ensure_initialised()
        symbol = self._strip_prefix(market)
        info = mt5.symbol_info_tick(symbol)
        if info is None:
            mt5.symbol_select(symbol, True)
            info = mt5.symbol_info_tick(symbol)
        if info is None:
            raise RuntimeError(f"MT5 has no tick data for {symbol!r}")
        bid = float(getattr(info, "bid", 0.0) or 0.0)
        ask = float(getattr(info, "ask", 0.0) or 0.0)
        last = float(getattr(info, "last", 0.0) or (bid + ask) / 2)
        mid = (bid + ask) / 2 if bid and ask else last
        spread_bps = ((ask - bid) / mid * 10_000.0) if mid else 0.0
        ts_ms = int(getattr(info, "time_msc", int(time.time() * 1000)) or 0)
        return Ticker(
            market=market, bid=bid, ask=ask, mid=mid, last=last,
            spread_bps=spread_bps, ts_ms=ts_ms, venue=self.venue,
        )

    def get_klines(self, market: str, *, interval: str = "1m",
                    limit: int = 100) -> list[list[Any]]:
        mt5 = self._ensure_initialised()
        symbol = self._strip_prefix(market)
        mt5.symbol_select(symbol, True)
        tf = interval.lower()
        # Resolve timeframe via the SDK's TIMEFRAME_<key> constants.
        tf_const = None
        for key, mins in _DEFAULT_TIMEFRAMES.items():
            if tf == key:
                tf_const = getattr(mt5, f"TIMEFRAME_{tf.upper()}",
                                    getattr(mt5, "TIMEFRAME_M1"))
                break
        if tf_const is None:
            tf_const = getattr(mt5, "TIMEFRAME_M1")
        rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, int(limit))
        if rates is None:
            return []
        out: list[list[Any]] = []
        for r in rates:
            ts_ms = int(int(r["time"]) * 1000)
            out.append([
                ts_ms, float(r["open"]), float(r["high"]),
                float(r["low"]), float(r["close"]),
                float(r["tick_volume"]),
            ])
        return out

    def get_balances(self) -> list[Balance]:
        mt5 = self._ensure_initialised()
        info = mt5.account_info()
        if info is None:
            return []
        currency = getattr(info, "currency", "USD")
        balance = float(getattr(info, "balance", 0.0))
        equity = float(getattr(info, "equity", 0.0))
        margin = float(getattr(info, "margin", 0.0))
        return [Balance(asset=currency, free=balance,
                         locked=margin, total=equity)]

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
        mt5 = self._ensure_initialised()
        symbol = self._strip_prefix(market)
        mt5.symbol_select(symbol, True)
        otype = order_type.lower()
        action = mt5.TRADE_ACTION_DEAL
        if otype == "limit":
            action = mt5.TRADE_ACTION_PENDING
            mt5_type = (mt5.ORDER_TYPE_BUY_LIMIT
                        if side.lower() == "buy"
                        else mt5.ORDER_TYPE_SELL_LIMIT)
        else:
            mt5_type = (mt5.ORDER_TYPE_BUY
                        if side.lower() == "buy"
                        else mt5.ORDER_TYPE_SELL)
        request = {
            "action": action,
            "symbol": symbol,
            "volume": float(size),
            "type": mt5_type,
            "deviation": int(self.config.get("deviation", 10)),
            "type_time": getattr(mt5, "ORDER_TIME_GTC"),
            "type_filling": getattr(mt5, "ORDER_FILLING_IOC"),
            "comment": str(client_order_id or "nerya"),
        }
        if price is not None:
            request["price"] = float(price)
        result = mt5.order_send(request)
        if result is None:
            raise RuntimeError(
                f"MT5 order_send returned None: {mt5.last_error()!r}"
            )
        return OrderAck(
            order_id=str(getattr(result, "order", "")),
            client_order_id=str(client_order_id or ""),
            status=("filled" if getattr(result, "retcode", 0)
                    in (getattr(mt5, "TRADE_RETCODE_DONE", 10009),)
                    else "rejected"),
            market=market, side=side,
            price=float(getattr(result, "price", 0.0)),
            size=float(size),
            filled=float(getattr(result, "volume", 0.0)),
            avg_price=float(getattr(result, "price", 0.0)),
            raw={"retcode": int(getattr(result, "retcode", 0))},
        )


__all__ = ["MT5Connector", "MT5Credentials"]
