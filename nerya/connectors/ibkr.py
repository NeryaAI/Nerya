"""Interactive Brokers (TWS / IB Gateway) connector.

Wraps either ``ib_async`` (preferred, modern asyncio-based fork) or
``ib_insync`` (legacy maintained mirror). When neither package is
installed, the connector still constructs successfully but every
non-paper method raises a clear "install ib_async" error so the
account-intake / dashboard surface can keep working in dry-run mode.

Connection model
----------------

IBKR doesn't expose REST/key auth; instead the user runs a TWS or IB
Gateway desktop process locally and the connector opens a TCP socket
to it. Credentials therefore look very different from a CEX:

* ``host`` — usually ``127.0.0.1``.
* ``port`` — ``7497`` (TWS paper), ``7496`` (TWS live), ``4002`` (IB
  Gateway paper), ``4001`` (IB Gateway live).
* ``client_id`` — integer id distinguishing simultaneous connections.
* ``account_id`` — optional ``DU…`` (paper) / ``U…`` (live) identifier
  used when the login owns multiple accounts.

Order routing model
-------------------

IBKR markets in Nerya use the ``IBKR:<symbol>`` convention. The symbol
is parsed loosely — single underscores split exchange/symbol/currency
when present (e.g. ``IBKR:NASDAQ_TSLA_USD`` — exchange ``NASDAQ``,
ticker ``TSLA``, currency ``USD``). Plain ``IBKR:TSLA`` is treated as
SMART-routed equity in USD.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .base import Balance, CEXConnectorBase, OrderAck, Ticker


@dataclass
class IBKRCredentials:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 1
    account_id: str = ""


class IBKRConnector(CEXConnectorBase):
    """Connector for Interactive Brokers TWS / IB Gateway."""

    venue = "IBKR"
    kind = "broker"

    def __init__(
        self,
        credentials: IBKRCredentials | None = None,
        *,
        live: bool = False,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.credentials = credentials or IBKRCredentials()
        self.live = bool(live)
        self.config = dict(config or {})
        self._ib = None
        self._lib_kind = ""

    # ------------------------------------------------------------ private
    def _ensure_connected(self):
        """Lazy-import ``ib_async``/``ib_insync`` and open the socket.

        Re-raises with a clear install hint so the upstream
        ``WalletDependencyError``-equivalent message lands in the agent
        log (and in the gateway reply when this is invoked from a chat).
        """

        if self._ib is not None:
            return self._ib
        IB = None
        try:
            from ib_async import IB as _IB  # type: ignore
            IB = _IB
            self._lib_kind = "ib_async"
        except Exception:
            try:
                from ib_insync import IB as _IB  # type: ignore
                IB = _IB
                self._lib_kind = "ib_insync"
            except Exception as exc:
                raise RuntimeError(
                    "IBKR connector needs ib_async (preferred) or ib_insync; "
                    "install with `pip install ib_async`."
                ) from exc
        ib = IB()
        ib.connect(
            self.credentials.host,
            int(self.credentials.port),
            clientId=int(self.credentials.client_id),
            timeout=int(self.config.get("connect_timeout", 8)),
        )
        self._ib = ib
        return ib

    def disconnect(self) -> None:
        try:
            if self._ib is not None:
                self._ib.disconnect()
        finally:
            self._ib = None

    @staticmethod
    def _parse_market(market: str) -> tuple[str, str, str]:
        """Return ``(exchange, symbol, currency)`` from an IBKR market id.

        Accepts ``IBKR:TSLA``, ``IBKR:NASDAQ_TSLA``,
        ``IBKR:NASDAQ_TSLA_USD``. Also tolerates a plain symbol.
        """

        body = market.split(":", 1)[-1]
        parts = body.split("_")
        if len(parts) == 1:
            return "SMART", parts[0].upper(), "USD"
        if len(parts) == 2:
            return parts[0].upper(), parts[1].upper(), "USD"
        return parts[0].upper(), parts[1].upper(), parts[2].upper()

    def _build_contract(self, market: str):
        from importlib import import_module
        mod_name = self._lib_kind or "ib_async"
        try:
            mod = import_module(mod_name)
        except ImportError:
            mod = import_module("ib_insync")
        Stock = getattr(mod, "Stock")
        exchange, symbol, currency = self._parse_market(market)
        return Stock(symbol, exchange, currency)

    # ------------------------------------------------------------ public
    def get_ticker(self, market: str) -> Ticker:
        ib = self._ensure_connected()
        contract = self._build_contract(market)
        ib.qualifyContracts(contract)
        snap = ib.reqMktData(contract, snapshot=True, regulatorySnapshot=False)
        # Wait briefly for the snapshot to populate.
        for _ in range(10):
            ib.sleep(0.2)
            if snap.bid and snap.ask:
                break
        bid = float(snap.bid or 0.0)
        ask = float(snap.ask or 0.0)
        last = float(snap.last or snap.close or (bid + ask) / 2 or 0.0)
        mid = (bid + ask) / 2 if bid and ask else last
        spread_bps = ((ask - bid) / mid * 10_000.0) if mid else 0.0
        return Ticker(
            market=market, bid=bid, ask=ask, mid=mid, last=last,
            spread_bps=spread_bps, ts_ms=int(time.time() * 1000),
            venue=self.venue,
        )

    def get_klines(self, market: str, *, interval: str = "1m",
                    limit: int = 100) -> list[list[Any]]:
        ib = self._ensure_connected()
        contract = self._build_contract(market)
        ib.qualifyContracts(contract)
        # IBKR uses bar size strings: '1 min', '5 mins', '1 hour', '1 day'.
        bar_map = {
            "1m": "1 min", "5m": "5 mins", "15m": "15 mins", "30m": "30 mins",
            "1h": "1 hour", "4h": "4 hours", "1d": "1 day",
        }
        bar_size = bar_map.get(interval, "1 min")
        # Approximate duration string from limit + bar size.
        if "min" in bar_size:
            duration = f"{max(1, int(limit) * int(bar_size.split()[0]) // 60)} D"
        elif "hour" in bar_size:
            duration = f"{max(1, int(limit) // 6 + 1)} D"
        else:
            duration = f"{max(1, int(limit))} D"
        bars = ib.reqHistoricalData(
            contract, endDateTime="", durationStr=duration,
            barSizeSetting=bar_size, whatToShow="TRADES", useRTH=False,
            formatDate=2, keepUpToDate=False,
        )
        out: list[list[Any]] = []
        for b in bars[-int(limit):]:
            ts = int(getattr(b, "date").timestamp() * 1000) if hasattr(b.date, "timestamp") else 0
            out.append([
                ts, float(b.open), float(b.high), float(b.low),
                float(b.close), float(b.volume),
            ])
        return out

    def get_balances(self) -> list[Balance]:
        ib = self._ensure_connected()
        rows: list[Balance] = []
        target = self.credentials.account_id or ""
        for v in ib.accountValues():
            if target and v.account != target:
                continue
            if v.tag != "CashBalance" or v.currency in (None, "BASE"):
                continue
            try:
                free = float(v.value)
            except (TypeError, ValueError):
                continue
            rows.append(Balance(asset=v.currency, free=free, locked=0.0,
                                 total=free))
        return rows

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
        ib = self._ensure_connected()
        from importlib import import_module
        mod = import_module(self._lib_kind or "ib_async")
        Order = getattr(mod, "Order")
        contract = self._build_contract(market)
        ib.qualifyContracts(contract)
        action = "BUY" if side.lower() == "buy" else "SELL"
        order = Order()
        order.action = action
        order.totalQuantity = float(size)
        otype = order_type.lower()
        if otype == "market":
            order.orderType = "MKT"
        elif otype == "limit":
            order.orderType = "LMT"
            order.lmtPrice = float(price or 0.0)
        else:
            order.orderType = otype.upper()
        order.tif = (time_in_force or "GTC").upper()
        if client_order_id:
            order.orderRef = str(client_order_id)
        if self.credentials.account_id:
            order.account = self.credentials.account_id
        trade = ib.placeOrder(contract, order)
        ib.sleep(0.5)
        order_status = getattr(trade.orderStatus, "status", "PendingSubmit")
        return OrderAck(
            order_id=str(getattr(trade.order, "orderId", "")),
            client_order_id=str(client_order_id or ""),
            status=str(order_status).lower(), market=market, side=side,
            price=float(price) if price is not None else None,
            size=float(size),
            filled=float(getattr(trade.orderStatus, "filled", 0.0) or 0.0),
            avg_price=float(getattr(trade.orderStatus, "avgFillPrice", 0.0) or 0.0),
            raw={"library": self._lib_kind},
        )


__all__ = ["IBKRConnector", "IBKRCredentials"]
