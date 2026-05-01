"""Polymarket v2 CLOB + Gamma + Data API connector.

Surfaces prediction-market orderbooks and candles as ordinary Nerya
:class:`Connector` reads so the rest of the stack (market_data skill,
agent loop) doesn't need to know Polymarket is special.

Endpoints used (public):

* ``https://clob.polymarket.com``          — CLOB v2 (orderbooks, orders)
* ``https://gamma-api.polymarket.com``     — Gamma (market metadata)
* ``https://data-api.polymarket.com``      — price series / candles

Writes (``place_order``) require EIP-712 signing via the API user's
Polygon wallet — we require the caller to pass a pre-built JSON order
payload + signature in ``credentials.extra["signed_order"]`` since
EIP-712 on-chain signing lives in the wallet layer, not here.

Markets can be referenced by *token id* (the long hex condition token
id) or by *slug* (Gamma's ``market.slug``). ``_resolve_token`` handles
both — slugs get one metadata lookup and are cached.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import TradingError
from .base import Balance, CEXConnectorBase, OrderAck, Ticker
from .cex_base import CEXCredentials
from .http import HttpTransport, UrllibHttp


CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"
DATA_URL = "https://data-api.polymarket.com"


@dataclass
class PolymarketConnector(CEXConnectorBase):
    """Polymarket CLOB v2 connector.

    ``live=True`` + ``credentials.api_key/secret/passphrase`` are still
    required before any private call runs — we mirror the CEX gate so
    the execution engine's safety assumptions hold.
    """

    venue: str = "POLYMARKET"
    credentials: CEXCredentials = field(default_factory=CEXCredentials)
    live: bool = False
    transport: HttpTransport = field(default_factory=UrllibHttp)
    clob_url: str = CLOB_URL
    gamma_url: str = GAMMA_URL
    data_url: str = DATA_URL
    _market_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    # --------------------------------------------------------- helpers
    def _get(
        self, base: str, path: str, *,
        params: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any] | list[Any]:
        url = base.rstrip("/") + "/" + path.lstrip("/")
        status, doc = self.transport.request(
            "GET", url, params=params or {}, timeout=timeout,
        )
        if status >= 400:
            raise TradingError(f"polymarket GET {path} {status}: {doc}")
        return doc

    def _resolve_token(self, market: str) -> tuple[str, dict[str, Any]]:
        """Return ``(token_id, market_meta)``.

        Accepts either a condition token id (``0x...`` 66-char hex) or
        a Gamma market slug. Slugs are cached.
        """
        m = market.split(":", 1)[-1].strip()
        if m.lower().startswith("0x") and len(m) >= 60:
            return m, self._market_cache.get(m, {"token_id": m})
        if m in self._market_cache:
            meta = self._market_cache[m]
            return meta.get("token_id") or m, meta
        meta = self._lookup_slug(m)
        self._market_cache[m] = meta
        return meta["token_id"], meta

    def _lookup_slug(self, slug: str) -> dict[str, Any]:
        doc = self._get(self.gamma_url, "markets", params={"slug": slug})
        row = doc[0] if isinstance(doc, list) and doc else (doc if isinstance(doc, dict) else None)
        if not row:
            raise TradingError(f"polymarket: no market for slug {slug!r}")
        # Gamma returns `clobTokenIds` as a JSON string or list of 2 outcomes.
        raw_ids = row.get("clobTokenIds") or row.get("tokenIds")
        if isinstance(raw_ids, str):
            import json as _json
            try:
                raw_ids = _json.loads(raw_ids)
            except Exception:
                raw_ids = [raw_ids]
        if not raw_ids:
            raise TradingError(f"polymarket: slug {slug!r} has no clob token ids")
        token_id = raw_ids[0]
        return {
            "token_id": token_id,
            "slug": slug,
            "question": row.get("question"),
            "end_date": row.get("endDate"),
            "outcomes": row.get("outcomes"),
            "clob_token_ids": raw_ids,
        }

    # --------------------------------------------------------- public reads
    def get_ticker(self, market: str) -> Ticker:
        token_id, meta = self._resolve_token(market)
        book_raw = self._get(self.clob_url, "book", params={"token_id": token_id})
        bids = (book_raw or {}).get("bids") or []  # type: ignore[union-attr]
        asks = (book_raw or {}).get("asks") or []  # type: ignore[union-attr]
        bid = float(bids[0]["price"]) if bids else 0.0
        ask = float(asks[0]["price"]) if asks else 0.0
        mid = (bid + ask) / 2 if (bid and ask) else (bid or ask)
        last = float((book_raw or {}).get("tick_size") or 0) or mid  # type: ignore[union-attr]
        spread_bps = ((ask - bid) / mid) * 10_000 if mid else 0.0
        return Ticker(
            market=market, bid=bid, ask=ask, mid=mid, last=last,
            spread_bps=spread_bps, ts_ms=int(time.time() * 1000),
            venue=self.venue,
        )

    def get_order_book(self, market: str) -> dict[str, Any]:
        token_id, _meta = self._resolve_token(market)
        book = self._get(self.clob_url, "book", params={"token_id": token_id})
        if not isinstance(book, dict):
            raise TradingError(f"polymarket book bad response for {market}")
        bids = [[float(r["price"]), float(r["size"])] for r in book.get("bids") or []]
        asks = [[float(r["price"]), float(r["size"])] for r in book.get("asks") or []]
        return {
            "market": market, "token_id": token_id,
            "bid": bids[0][0] if bids else 0.0,
            "ask": asks[0][0] if asks else 0.0,
            "bids": bids[:20], "asks": asks[:20],
            "venue": self.venue, "ts_ms": int(time.time() * 1000),
        }

    def get_klines(
        self, market: str, *, interval: str = "1h", limit: int = 100,
    ) -> list[list[Any]]:
        """Polymarket Data API price history → ccxt-style OHLCV rows.

        The Polymarket data API returns point-in-time prices rather than
        full OHLCV. We synthesize OHLC from consecutive prices so the
        rest of the stack (candles cache, backtests) keeps working.
        """
        token_id, _meta = self._resolve_token(market)
        span = _interval_to_span(interval)
        doc = self._get(self.data_url, "prices-history", params={
            "market": token_id, "interval": span, "fidelity": limit,
        })
        rows = doc.get("history") if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            return []
        candles: list[list[Any]] = []
        prev_price: float | None = None
        for row in rows[-limit:]:
            ts_ms = int(row.get("t") or row.get("timestamp") or 0)
            if ts_ms and ts_ms < 10**12:
                ts_ms *= 1000
            price = float(row.get("p") or row.get("price") or 0.0)
            o = prev_price if prev_price is not None else price
            candles.append([ts_ms, o, max(o, price), min(o, price), price, 0.0])
            prev_price = price
        return candles

    # --------------------------------------------------------- private
    def get_balances(self) -> list[Balance]:
        self._require_ready()
        raw = self._get(
            self.clob_url, "balance-allowance",
            params={"asset_type": "COLLATERAL"},
        )
        if not isinstance(raw, dict):
            return []
        return [Balance(
            asset="USDC",
            free=float(raw.get("balance") or 0) / 1_000_000,
            locked=0.0,
            total=float(raw.get("balance") or 0) / 1_000_000,
        )]

    def place_order(self, *, market: str, side: str, order_type: str,
                    size: float, price: float | None = None,
                    client_order_id: str | None = None,
                    time_in_force: str = "GTC") -> OrderAck:
        self._require_ready()
        token_id, _meta = self._resolve_token(market)
        signed = getattr(self.credentials, "extra", {}).get("signed_order") \
            if hasattr(self.credentials, "extra") else None
        if not signed:
            raise TradingError(
                "polymarket.place_order requires a pre-signed EIP-712 order "
                "payload in credentials.extra['signed_order'] — "
                "build one with py-clob-client or the wallet skill first"
            )
        if not isinstance(signed, dict):
            raise TradingError("signed_order must be a dict")
        body = {"order": signed, "owner": self.credentials.api_key,
                "orderType": order_type.upper()}
        url = self.clob_url.rstrip("/") + "/order"
        status, doc = self.transport.request(
            "POST", url, body=body, timeout=15.0,
            headers={"Content-Type": "application/json",
                     "POLY_ADDRESS": self.credentials.api_key},
        )
        if status >= 400 or not isinstance(doc, dict):
            raise TradingError(f"polymarket place_order {status}: {doc}")
        return OrderAck(
            order_id=str(doc.get("orderID") or doc.get("id") or ""),
            client_order_id=str(doc.get("clientOrderId") or client_order_id or ""),
            status=_map_pm_status(doc.get("status")),
            market=market, side=side.lower(),
            price=price, size=size, raw=dict(doc),
        )

    def cancel_order(self, *, market: str, order_id: str) -> OrderAck:
        self._require_ready()
        url = self.clob_url.rstrip("/") + "/order"
        status, doc = self.transport.request(
            "DELETE", url, params={"orderID": order_id}, timeout=15.0,
            headers={"POLY_ADDRESS": self.credentials.api_key},
        )
        if status >= 400:
            raise TradingError(f"polymarket cancel_order {status}: {doc}")
        return OrderAck(
            order_id=order_id, client_order_id="",
            status="canceled", market=market, side="",
            raw=dict(doc) if isinstance(doc, dict) else {"raw": doc},
        )

    # --------------------------------------------------------- guards
    def _require_ready(self) -> None:
        if not self.live:
            raise TradingError(
                "polymarket: live trading disabled (set accounts.live + "
                "runtime.live_trading_enabled)"
            )
        if not self.credentials.api_key:
            raise TradingError(
                "polymarket: credentials.api_key required (your Polygon address)"
            )


def _interval_to_span(interval: str) -> str:
    """Map a Nerya/ccxt interval to a Polymarket data API ``interval=`` param."""
    s = interval.lower()
    mapping = {
        "1m": "1h", "5m": "1h", "15m": "1d",
        "1h": "1d", "4h": "1w", "1d": "1m", "1w": "max",
    }
    return mapping.get(s, "1d")


def _map_pm_status(raw: Any) -> str:
    s = str(raw or "").lower()
    if s in ("live", "open", "resting"):
        return "new"
    if s in ("filled", "matched"):
        return "filled"
    if s in ("partial", "partiallyfilled", "partially_filled"):
        return "partial"
    if s in ("cancelled", "canceled"):
        return "canceled"
    if s == "rejected":
        return "rejected"
    return s or "new"


__all__ = ["PolymarketConnector"]
