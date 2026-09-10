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
from .base import Balance, CEXConnectorBase, ContractPosition, OrderAck, Ticker
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


def _timeframe_ms(interval: str) -> int:
    raw = str(interval or "1m").strip()
    if not raw:
        return 60_000
    unit = raw[-1].lower()
    try:
        qty = int(raw[:-1] or 1)
    except ValueError:
        return 60_000
    if unit == "m":
        return qty * 60_000
    if unit == "h":
        return qty * 3_600_000
    if unit == "d":
        return qty * 86_400_000
    if unit == "w":
        return qty * 7 * 86_400_000
    return 60_000


def _dedupe_ohlcv(rows: list[list[Any]], *, limit: int) -> list[list[Any]]:
    by_ts: dict[int, list[Any]] = {}
    for row in rows:
        if not row:
            continue
        try:
            ts = int(row[0])
        except Exception:
            continue
        by_ts[ts] = list(row)
    out = [by_ts[ts] for ts in sorted(by_ts)]
    if limit > 0:
        out = out[-limit:]
    return out


def _filter_ohlcv_window(
    rows: list[list[Any]],
    *,
    since: int | None,
    end: int | None,
) -> list[list[Any]]:
    out: list[list[Any]] = []
    for row in rows:
        if not row:
            continue
        try:
            ts = int(row[0])
        except Exception:
            continue
        if since is not None and ts < int(since):
            continue
        if end is not None and ts > int(end):
            continue
        out.append(list(row))
    return out


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
    timeout_ms: int = 15_000
    # E3/C3: set by the provider factory for perp/linear venue specs
    # (bybit_perpetual, binance_perpetual, …). Bare BASE/QUOTE symbols
    # then resolve to the linear swap contract (BASE/QUOTE:QUOTE) so
    # perp accounts price and place on derivatives instead of silently
    # trading spot (which would skip leverage/reduceOnly/brackets).
    prefer_perpetual: bool = False
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
            "timeout": max(250, int(self.timeout_ms or 15_000)),
        }
        if self.credentials.api_key:
            params["apiKey"] = self.credentials.api_key
        if self.credentials.api_secret:
            params["secret"] = self.credentials.api_secret
        if self.credentials.api_passphrase:
            params["password"] = self.credentials.api_passphrase
        for key, value in (self.credentials.extras or {}).items():
            if value:
                params[str(key)] = value
        if self.options:
            params["options"] = dict(self.options)
        return klass(params)

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    @property
    def markets(self) -> dict[str, Any]:
        """Lazily-loaded ccxt ``markets`` map (per-symbol precision, contract
        size, lot limits). Cached for the connector's lifetime so every
        ``place_order`` / ``fetch_*`` call can precision-round without an
        extra round-trip.

        A failed load is NEVER cached (F2): caching ``{}`` made every
        symbol look spot-like for the rest of the process, silently
        dropping reduceOnly / bracket params — a protection flatten
        would have opened an opposite position instead of closing.
        """
        cache = getattr(self, "_markets", None)
        if cache is None:
            try:
                cache = self.client.load_markets()
            except Exception as exc:
                raise TradingError(
                    f"{self.venue} markets_unavailable — load_markets "
                    f"failed: {exc}"
                ) from exc
            if not isinstance(cache, dict):
                cache = dict(cache or {})
            self._markets = cache
        return cache

    def _is_derivatives(self, sym: str) -> bool:
        mkt = self.markets.get(sym) or {}
        contract = mkt.get("contract") or mkt.get("swap") or mkt.get("future")
        return bool(contract)

    def _contract_size(self, sym: str) -> float:
        mkt = self.markets.get(sym) or {}
        try:
            return float(mkt.get("contractSize") or 1.0)
        except Exception:
            return 1.0

    def _round_amount(self, sym: str, size: float, price: float | None = None) -> float:
        """Round an order amount down without exceeding the approved size,
        then enforce the venue's minimum order value (limits.cost.min)."""
        amount = float(size)
        if amount <= 0:
            raise TradingError(f"{self.venue} order amount must be positive")
        # Prefer ccxt's amount_to_precision: it covers precision.amount /
        # decimalPlaces / lot step in one shot for every venue — including
        # ones like Bybit that leave limits.amount.step unset (F6). Fall
        # back to the raw step logic when the venue adapter can't round.
        try:
            amount = float(self.client.amount_to_precision(sym, amount))
        except Exception:
            mkt = self.markets.get(sym) or {}
            lim = mkt.get("limits", {}).get("amount", {}) if isinstance(mkt, dict) else {}
            step = lim.get("step") if isinstance(lim, dict) else None
            try:
                if step:
                    amount = (amount // float(step)) * float(step)
            except Exception:
                pass
        mkt = self.markets.get(sym) or {}
        limits = mkt.get("limits", {}) if isinstance(mkt, dict) else {}
        amt_lim = limits.get("amount", {}) if isinstance(limits, dict) else {}
        try:
            min_amt = float(amt_lim.get("min") or 0.0) if isinstance(amt_lim, dict) else 0.0
        except Exception:
            min_amt = 0.0
        if min_amt > 0 and amount < min_amt:
            raise TradingError(
                f"{self.venue} order amount {amount:g} is below the "
                f"exchange minimum {min_amt:g} for {sym}"
            )
        # Minimum notional (F6): venues reject sub-min order value with a
        # cryptic upstream error — refuse here where the message is clear.
        # R2C1: ``amount`` arrives in ccxt *contracts* (place_order already
        # divided the base size by contractSize), so the order value is
        # amount * contractSize * price — for contractSize=1 venues (spot,
        # linear USDT swaps) that is identical to the old amount * price.
        try:
            cost_lim = limits.get("cost", {}) if isinstance(limits, dict) else {}
            min_cost = float(cost_lim.get("min") or 0.0) if isinstance(cost_lim, dict) else 0.0
        except Exception:
            min_cost = 0.0
        try:
            contract_size = float(mkt.get("contractSize") or 1.0)
        except Exception:
            contract_size = 1.0
        if contract_size <= 0:
            contract_size = 1.0
        if (
            min_cost > 0
            and price is not None
            and amount * contract_size * float(price) < min_cost
        ):
            raise TradingError(
                f"min_notional: {self.venue} order {amount:g} {sym} "
                f"(x{contract_size:g} contract) @ {float(price):g} is "
                f"below the exchange minimum notional {min_cost:g}"
            )
        if amount <= 0:
            raise TradingError(
                f"{self.venue} order amount rounds to zero for {sym}"
            )
        return amount

    def _round_price(self, sym: str, price: float | None) -> float | None:
        if price is None:
            return None
        try:
            return float(self.client.price_to_precision(sym, float(price)))
        except Exception:
            return float(price)

    def _price_param(self, sym: str, price: float) -> str:
        """Return an exchange-precision string for nested order params."""

        try:
            return str(self.client.price_to_precision(sym, float(price)))
        except Exception:
            return str(float(price))

    @staticmethod
    def _is_no_change(exc: Exception) -> bool:
        """True when ccxt reports 'value already set' (F3).

        Bybit answers 110043 'leverage not modified' / 110026 'margin
        mode already set' when the requested value is already active —
        ccxt 4.5.x surfaces those as ``NoChange`` (a BadRequest
        subclass). That is success, not a failure.
        """
        try:
            from ccxt.base.errors import NoChange  # type: ignore
        except Exception:
            return False
        return isinstance(exc, NoChange)

    @staticmethod
    def _is_ambiguous_exc(exc: Exception) -> bool:
        """True when a create_order failure leaves the venue outcome
        unknown (F4): timeout / network drop after a request that may
        have been accepted. Raw socket timeouts and resets surface as
        OSError subclasses.
        """
        try:
            from ccxt.base.errors import (  # type: ignore
                ExchangeNotAvailable, NetworkError, RequestTimeout,
            )
        except Exception:
            pass
        else:
            if isinstance(exc, (RequestTimeout, NetworkError,
                                ExchangeNotAvailable)):
                return True
        return isinstance(exc, (OSError, TimeoutError))

    @staticmethod
    def _is_not_found_exc(exc: Exception) -> bool:
        """True when ccxt classified the failure as 'no such order' (R3T3).

        ccxt maps venue-specific codes (e.g. Bybit retCode 10001 "order
        not exists or too late...") onto ``OrderNotFound``. Wrapping the
        exception into ``TradingError`` used to erase that class, so the
        poller's 4-strike ``lost`` machinery never fired on the target
        venue — the flag carries the classification through instead of
        relying on message substrings.
        """
        try:
            from ccxt.base.errors import OrderNotFound  # type: ignore
        except Exception:
            return False
        return isinstance(exc, OrderNotFound)

    def _ensure_leverage_and_margin(
        self,
        sym: str,
        *,
        leverage: float | None,
        margin_mode: str | None,
    ) -> None:
        """Set requested derivative controls, failing closed on any error."""
        if not self._is_derivatives(sym):
            return
        if leverage and float(leverage) > 0:
            try:
                self.client.set_leverage(int(float(leverage)), sym)
            except Exception as exc:
                if self._is_no_change(exc):
                    pass  # requested leverage already active
                else:
                    raise TradingError(
                        f"{self.venue} failed to set leverage for {sym}: {exc}"
                    ) from exc
        if margin_mode and str(margin_mode).lower() in ("isolated", "cross"):
            try:
                self.client.set_margin_mode(str(margin_mode).lower(), sym)
            except Exception as exc:
                if self._is_no_change(exc):
                    pass  # requested margin mode already active
                else:
                    raise TradingError(
                        f"{self.venue} failed to set margin mode for {sym}: {exc}"
                    ) from exc

    # --------------------------------------------------------- helpers
    def _normalise_symbol(self, market: str) -> str:
        """Normalise a market id to ccxt's unified symbol.

        Handled shapes:

        * ``BTCUSDT`` / ``btcusdt`` / ``BYBIT:SOLUSDT`` → ``BTC/USDT``
        * ``SOL/USDT:USDT`` / ``BYBIT:SOL/USDT:USDT`` → kept verbatim —
          an explicit contract suffix means linear swap on any adapter
        * ``BYBIT_PERPETUAL:SOLUSDT`` (the format Nerya tooling emits)
          → ``SOL/USDT:USDT`` — the derivatives prefix forces the
          linear reading even on a spot-capable connector

        On a perp-preferred connector (:attr:`prefer_perpetual`), bare
        ``BASE/QUOTE`` resolves to the swap contract so derivatives
        params keep applying instead of silently trading spot (E3/C3).
        The settlement currency follows the quote (R2C2): USDT/USDC
        pairs are linear (``ETHUSDT`` → ``ETH/USDT:USDT``) while
        USD-quoted pairs are the inverse / coin-margined family with
        the base as settle (``BTCUSD`` → ``BTC/USD:BTC``).
        """
        raw = str(market or "").strip()
        if not raw:
            return raw
        # Split an optional VENUE: prefix, but never inside a slash
        # symbol: "SOL/USDT:USDT" is a unified contract symbol, not
        # venue "SOL/USDT" + tail "USDT".
        head = ""
        body = raw
        if ":" in raw:
            cand_head, cand_body = raw.split(":", 1)
            if "/" not in cand_head:
                head, body = cand_head, cand_body
        force_perp = False
        if head:
            h = head.upper()
            force_perp = "PERP" in h or "SWAP" in h or "FUTURE" in h
        if "/" in body and ":" in body:
            # Explicit unified contract symbol — already unambiguous;
            # keep verbatim so linear stays linear.
            return body.upper()
        if "/" in body:
            sym = body.upper()
        else:
            # crude USDT/USD/BUSD split
            s = body.upper().replace("-", "")
            sym = body.upper()
            for q in ("USDT", "USDC", "BUSD", "USD", "USDS"):
                if s.endswith(q) and len(s) > len(q):
                    sym = f"{s[:-len(q)]}/{q}"
                    break
        if (force_perp or self.prefer_perpetual) and "/" in sym and ":" not in sym:
            base, quote = sym.split("/", 1)
            # R2C2: pick the settlement currency from the quote. USD-quoted
            # pairs are the coin-margined / inverse family — bybit's
            # inverse unified symbol is BTC/USD:BTC (settle = base), and
            # the old BTC/USD:USD mapping hit BadSymbol at the venue.
            # USDT/USDC-quoted pairs stay linear (settle = quote).
            q = quote.upper()
            settle = base.upper() if q == "USD" else q
            sym = f"{base}/{quote}:{settle}"
        return sym

    def _check_live_and_keys(self) -> None:
        if not self.live:
            raise TradingError(
                f"{self.venue}: live trading disabled for this connector "
                f"(set accounts.live=true + runtime.live_trading_enabled=true)"
            )
        if not self.credentials.api_key or not self.credentials.api_secret:
            if self.exchange_id == "hyperliquid" and (
                self.credentials.extras.get("walletAddress")
                and self.credentials.extras.get("privateKey")
            ):
                return
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
        self,
        market: str,
        *,
        interval: str = "1m",
        limit: int = 100,
        since: int | None = None,
        end: int | None = None,
    ) -> list[list[Any]]:
        sym = self._normalise_symbol(market)
        target = max(1, int(limit or 100))
        tf_ms = _timeframe_ms(interval)
        page_limit = min(target, int(self.options.get("ohlcv_page_limit") or 1000))
        end_ms = int(end) if end is not None else None
        now_ms = int(time.time() * 1000)
        cursor = int(since) if since is not None else (end_ms or now_ms) - (target + 5) * tf_ms
        max_pages = max(1, min(100, (target + page_limit - 1) // page_limit + 5))
        rows: list[list[Any]] = []
        try:
            rows = self._fetch_ohlcv_auto_pages(
                sym,
                interval=interval,
                target=target,
                page_limit=page_limit,
                max_pages=max_pages,
                since=since,
                end=end_ms,
            )
            if rows:
                return _dedupe_ohlcv(
                    _filter_ohlcv_window(rows, since=since, end=end_ms),
                    limit=target,
                )

            for _ in range(max_pages):
                ohlcv = self._fetch_ohlcv_page(sym, interval=interval, since=cursor, limit=page_limit)
                batch = [list(row) for row in (ohlcv or [])]
                batch = _filter_ohlcv_window(batch, since=since, end=end_ms)
                if not batch:
                    break
                rows.extend(batch)
                newest = max(int(row[0]) for row in batch if row)
                next_cursor = newest + tf_ms
                if next_cursor <= cursor:
                    break
                cursor = next_cursor
                rows = _dedupe_ohlcv(rows, limit=target)
                if len(rows) >= target:
                    break
                if end_ms is not None and cursor > end_ms:
                    break
            if not rows:
                rows = self._fetch_ohlcv_latest_backward(
                    sym,
                    interval=interval,
                    target=target,
                    page_limit=page_limit,
                    max_pages=max_pages,
                    tf_ms=tf_ms,
                    since=since,
                    end=end_ms,
                )
        except Exception as exc:
            raise TradingError(f"{self.venue} fetch_ohlcv {sym} failed: {exc}") from exc
        return _dedupe_ohlcv(
            _filter_ohlcv_window(rows, since=since, end=end_ms),
            limit=target,
        )

    def _fetch_ohlcv_page(
        self,
        sym: str,
        *,
        interval: str,
        since: int | None,
        limit: int,
        params: dict[str, Any] | None = None,
    ) -> list[Any]:
        if params:
            return self.client.fetch_ohlcv(
                sym,
                timeframe=interval,
                since=since,
                limit=limit,
                params=params,
            )
        return self.client.fetch_ohlcv(
            sym,
            timeframe=interval,
            since=since,
            limit=limit,
        )

    def _fetch_ohlcv_auto_pages(
        self,
        sym: str,
        *,
        interval: str,
        target: int,
        page_limit: int,
        max_pages: int,
        since: int | None,
        end: int | None,
    ) -> list[list[Any]]:
        """Use CCXT's built-in paginator when the installed version supports it.

        CCXT marks automatic pagination as experimental, so callers fall back to
        the deterministic manual loop below whenever a venue/version rejects
        the params or returns no rows.
        """

        enabled = bool(self.options.get("ohlcv_auto_paginate", True))
        if not enabled:
            return []
        params = dict(self.options.get("ohlcv_params") or {})
        params.setdefault("paginate", True)
        params.setdefault(
            "paginationCalls",
            max(1, min(100, int(self.options.get("ohlcv_pagination_calls") or max_pages))),
        )
        params.setdefault("maxEntriesPerRequest", page_limit)
        try:
            rows = self._fetch_ohlcv_page(
                sym,
                interval=interval,
                since=int(since) if since is not None else None,
                limit=target,
                params=params,
            )
        except Exception:
            return []
        batch = [list(row) for row in (rows or [])]
        return _filter_ohlcv_window(batch, since=since, end=end)

    def _fetch_ohlcv_latest_backward(
        self,
        sym: str,
        *,
        interval: str,
        target: int,
        page_limit: int,
        max_pages: int,
        tf_ms: int,
        since: int | None,
        end: int | None,
    ) -> list[list[Any]]:
        """Best-effort fallback for venues that ignore very old ``since``.

        Some ccxt venues return an empty page when ``since`` predates listing.
        Starting from the latest page still gives the operator a real short
        window, then we walk backward by asking for the page just before the
        oldest candle we have. This is deliberately conservative: it stops as
        soon as the venue repeats a page or cannot provide older rows.
        """

        rows: list[list[Any]] = []
        latest = self._fetch_ohlcv_page(
            sym,
            interval=interval,
            since=None,
            limit=page_limit,
        )
        batch = _filter_ohlcv_window([list(row) for row in (latest or [])], since=since, end=end)
        if not batch:
            return []
        rows.extend(batch)
        for _ in range(max(0, max_pages - 1)):
            rows = _dedupe_ohlcv(rows, limit=target)
            if len(rows) >= target:
                break
            oldest = min(int(row[0]) for row in rows if row)
            cursor = max(0, oldest - (page_limit + 5) * tf_ms)
            if since is not None:
                cursor = max(cursor, int(since))
            if cursor >= oldest:
                break
            older = self._fetch_ohlcv_page(
                sym,
                interval=interval,
                since=cursor,
                limit=page_limit,
            )
            older_batch = [
                row
                for row in _filter_ohlcv_window(
                    [list(item) for item in (older or [])],
                    since=since,
                    end=end,
                )
                if int(row[0]) < oldest
            ]
            if not older_batch:
                break
            rows.extend(older_batch)
        return _dedupe_ohlcv(rows, limit=target)

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

    def place_order(
        self,
        *,
        market: str,
        side: str,
        order_type: str,
        size: float,
        price: float | None = None,
        client_order_id: str | None = None,
        time_in_force: str = "GTC",
        reduce_only: bool = False,
        leverage: float | None = None,
        margin_mode: str | None = None,
        position_side: str | None = None,
        position_idx: int | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        trigger_price: float | None = None,
        extra_params: dict[str, Any] | None = None,
        reference_price: float | None = None,
    ) -> OrderAck:
        self._check_live_and_keys()
        sym = self._normalise_symbol(market)
        # F2: fail closed when instrument metadata can't be loaded —
        # an empty markets map would silently drop reduceOnly /
        # positionSide / bracket params and contract-size conversion.
        markets = self.markets
        if not markets:
            raise TradingError(
                f"{self.venue} markets_unavailable — refusing to place "
                f"{market}: load_markets returned no instruments, so "
                f"derivatives detection and precision rounding would "
                f"silently degrade"
            )
        is_deriv = self._is_derivatives(sym)

        # Pre-trade derivative setup (leverage / margin mode). Idempotent.
        if is_deriv:
            self._ensure_leverage_and_margin(
                sym, leverage=leverage, margin_mode=margin_mode,
            )

        # Precision rounding.
        contract_size = self._contract_size(sym) if is_deriv else 1.0
        amount = float(size)
        if is_deriv and contract_size and contract_size != 1.0:
            # ccxt amounts for swaps are in *contracts*; convert from base.
            amount = amount / float(contract_size or 1.0)
        px = self._round_price(sym, price)
        # R3T6: market orders have no order price, which used to skip the
        # min-notional guard entirely and let sub-min orders die at the
        # venue with a cryptic upstream error. Fall back to the caller's
        # reference mark (candidate meta) for the *cost check only* — it
        # is never sent to the venue.
        cost_ref = px
        if cost_ref is None and reference_price is not None:
            try:
                ref = float(reference_price)
            except Exception:
                ref = 0.0
            if ref > 0:
                cost_ref = ref
        amount = self._round_amount(sym, amount, price=cost_ref)

        # Build the ccxt params dict.
        params: dict[str, Any] = {}
        if client_order_id:
            params["clientOrderId"] = client_order_id
        if time_in_force and order_type.lower() == "limit":
            # E8: ccxt expects uppercase TIF codes; post_only is a
            # separate flag, not a timeInForce value.
            tif = str(time_in_force).strip()
            tif_l = tif.lower()
            if tif_l == "post_only":
                params["postOnly"] = True
            elif tif_l in ("gtc", "ioc", "fok"):
                params["timeInForce"] = tif_l.upper()
            else:
                # Unknown values pass through unchanged — the venue may
                # understand exchange-specific codes (e.g. "GTX", "DAY").
                params["timeInForce"] = tif
        if is_deriv:
            if reduce_only:
                params["reduceOnly"] = True
            if position_side:
                params["positionSide"] = str(position_side)
            if position_idx is not None:
                params["positionIdx"] = int(position_idx)
            # Native bracket — Bybit V5 uses stopLossPrice / takeProfitPrice;
            # ccxt normalises these for other venues too.
            if stop_loss is not None:
                params["stopLossPrice"] = self._price_param(sym, float(stop_loss))
            if take_profit is not None:
                params["takeProfitPrice"] = self._price_param(sym, float(take_profit))
            if trigger_price is not None:
                params["triggerPrice"] = self._price_param(sym, float(trigger_price))
        if extra_params:
            for k, v in extra_params.items():
                if v is not None and k not in params:
                    params[k] = v

        try:
            raw = self.client.create_order(
                sym, order_type.lower(), side.lower(), float(amount),
                None if px is None else float(px), params,
            )
        except Exception as exc:
            # F4: transport-level failures leave the venue outcome
            # unknown — flag them so the executor keeps the order
            # tracked + pollable instead of marking it rejected (which
            # would invite a duplicate re-place). Definitive rejects
            # (BadRequest / InsufficientFunds / PermissionDenied) stay
            # unambiguous.
            raise TradingError(
                f"{self.venue} place_order failed: {exc}",
                ambiguous=self._is_ambiguous_exc(exc),
            ) from exc

        fee_usd, fee_breakdown = self._extract_fee_usd(
            raw, market=market, avg_price=float(raw.get("average") or 0) or None,
        )
        bracket = _extract_bracket_order_ids(raw)
        return OrderAck(
            order_id=str(raw.get("id") or ""),
            client_order_id=str(raw.get("clientOrderId") or client_order_id or ""),
            status=_map_order_status(raw.get("status")),
            market=market, side=side.lower(),
            price=float(raw.get("price") or px or 0) or None,
            size=float(raw.get("amount") or amount or 0) or None,
            filled=float(raw.get("filled") or 0) or None,
            avg_price=float(raw.get("average") or 0) or None,
            fee_usd=fee_usd,
            fee_breakdown=fee_breakdown,
            attached_bracket_order_ids=bracket,
            raw=dict(raw),
        )

    def cancel_order(self, *, market: str, order_id: str) -> OrderAck:
        self._check_live_and_keys()
        sym = self._normalise_symbol(market)
        try:
            raw = self.client.cancel_order(order_id, sym)
        except Exception as exc:
            # R3T3: preserve ccxt's definitive not-found classification so
            # callers can tell "order gone" from transport noise.
            raise TradingError(
                f"{self.venue} cancel_order failed: {exc}",
                not_found=self._is_not_found_exc(exc),
            ) from exc
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
            # R3T3: preserve ccxt's definitive not-found classification
            # (OrderNotFound) on the raised TradingError so the order
            # poller's 4-strike ``lost`` machinery works on ccxt venues
            # instead of dying on a flattened message string.
            raise TradingError(
                f"{self.venue} fetch_order failed: {exc}",
                not_found=self._is_not_found_exc(exc),
            ) from exc
        avg = float(raw.get("average") or 0) or None
        fee_usd, fee_breakdown = self._extract_fee_usd(
            raw, market=market, avg_price=avg,
        )
        return OrderAck(
            order_id=str(raw.get("id") or order_id),
            client_order_id=str(raw.get("clientOrderId") or ""),
            status=_map_order_status(raw.get("status")),
            market=market, side=str(raw.get("side") or ""),
            price=float(raw.get("price") or 0) or None,
            size=float(raw.get("amount") or 0) or None,
            filled=float(raw.get("filled") or 0) or None,
            avg_price=avg,
            fee_usd=fee_usd,
            fee_breakdown=fee_breakdown,
            raw=dict(raw),
        )

    # ------------------------------------------------------------------
    # Derivatives reads — positions / open orders / fills
    # ------------------------------------------------------------------
    def fetch_positions(self, *, symbols: list[str] | None = None) -> list[ContractPosition]:
        self._check_live_and_keys()
        try:
            raw_positions = self.client.fetch_positions(symbols)
        except Exception as exc:
            raise TradingError(f"{self.venue} fetch_positions failed: {exc}") from exc
        out: list[ContractPosition] = []
        for r in raw_positions or []:
            try:
                sym = str(r.get("symbol") or "")
                side_raw = str(r.get("side") or "").lower() or "none"
                contracts = float(r.get("contracts") or 0.0)
                contract_size = float(r.get("contractSize") or 1.0)
                entry = float(r.get("entryPrice") or r.get("entry_price") or 0.0)
                mark = float(r.get("markPrice") or r.get("mark_price") or 0.0)
                notional = float(r.get("notional") or 0.0) or (abs(contracts) * contract_size * mark)
                margin = float(r.get("initialMargin") or r.get("initial_margin") or 0.0)
                upnl = float(
                    r.get("unrealisedPnl")
                    or r.get("unrealizedPnl")
                    or r.get("unrealized_pnl")
                    or 0.0
                )
                lev = float(r.get("leverage") or 1.0) or 1.0
                liq = r.get("liquidationPrice") or r.get("liquidation_price")
                liq_f = float(liq) if liq else None
                out.append(ContractPosition(
                    market=f"{self.venue.lower()}:{sym}" if sym else "",
                    side=side_raw,
                    contracts=contracts,
                    contract_size=contract_size,
                    entry_price=entry,
                    mark_price=mark,
                    notional_usd=notional,
                    initial_margin_usd=margin,
                    unrealised_pnl_usd=upnl,
                    leverage=lev,
                    liquidation_price=liq_f,
                    raw=dict(r) if isinstance(r, dict) else {"raw": str(r)},
                ))
            except Exception:
                continue
        return out

    def fetch_open_orders(self, *, symbols: list[str] | None = None) -> list[OrderAck]:
        self._check_live_and_keys()
        try:
            if symbols:
                raw_orders = []
                for s in symbols:
                    sym = self._normalise_symbol(s) if ":" in str(s) or "/" not in str(s) else s
                    raw_orders.extend(self.client.fetch_open_orders(sym))
            else:
                raw_orders = self.client.fetch_open_orders()
        except Exception as exc:
            raise TradingError(f"{self.venue} fetch_open_orders failed: {exc}") from exc
        return [_raw_order_to_ack(self, r) for r in raw_orders or []]

    def fetch_closed_orders(
        self,
        *,
        symbols: list[str] | None = None,
        since_ms: int | None = None,
        limit: int = 100,
    ) -> list[OrderAck]:
        """Recently closed orders (R3T4).

        The ``place_unknown`` adoption path scans closed orders for
        instantly-filled market orders that never rested in open orders.
        No connector implemented this before, so that adoption source was
        dead on every venue. Zero-argument calls (how the adoption loop
        invokes it) fall back to the most recent ``limit`` closed orders
        across all symbols.
        """
        self._check_live_and_keys()
        since = int(since_ms) if since_ms is not None else None
        try:
            if symbols:
                raw_orders: list[dict[str, Any]] = []
                for s in symbols:
                    sym = self._normalise_symbol(s) if ":" in str(s) or "/" not in str(s) else s
                    raw_orders.extend(
                        self.client.fetch_closed_orders(sym, since=since, limit=int(limit)),
                    )
            else:
                raw_orders = self.client.fetch_closed_orders(
                    None, since=since, limit=int(limit),
                )
        except Exception as exc:
            raise TradingError(f"{self.venue} fetch_closed_orders failed: {exc}") from exc
        return [_raw_order_to_ack(self, r) for r in raw_orders or []]

    def fetch_my_trades(
        self,
        *,
        market: str | None = None,
        since_ms: int | None = None,
        limit: int = 100,
    ) -> list[OrderAck]:
        self._check_live_and_keys()
        sym = self._normalise_symbol(market) if market else None
        try:
            # F10: ``since`` is fetch_my_trades' second positional/keyword
            # arg — passing it inside ``params`` is ignored by ccxt, which
            # made fill backfill fetch the wrong window.
            raw_trades = self.client.fetch_my_trades(
                sym,
                since=int(since_ms) if since_ms is not None else None,
                limit=int(limit),
                params={},
            )
        except Exception as exc:
            raise TradingError(f"{self.venue} fetch_my_trades failed: {exc}") from exc
        return [_raw_order_to_ack(self, r) for r in raw_trades or []]

    # ------------------------------------------------------------------
    # Fee extraction
    # ------------------------------------------------------------------
    def _extract_fee_usd(
        self,
        raw: dict[str, Any],
        *,
        market: str,
        avg_price: float | None,
    ) -> tuple[float | None, dict[str, float]]:
        """Pull the fee from a ccxt order/fetch response into USD.

        ccxt normalises fees into two shapes:

        * ``raw["fee"]``  → single ``{"currency": "BNB", "cost": 0.001}``
        * ``raw["fees"]`` → list of those dicts (some exchanges return
          maker + taker + funding fees separately)

        We aggregate every entry, converting non-USD costs to USD via
        ``avg_price`` when the fee asset matches the quote currency, or
        a public ticker fallback otherwise. Returns ``(None, {})`` when
        the broker didn't report a fee at all so callers can tell
        "swallowed" from "0".
        """
        entries: list[dict[str, Any]] = []
        single = raw.get("fee")
        if isinstance(single, dict) and single:
            entries.append(single)
        plural = raw.get("fees")
        if isinstance(plural, list):
            for f in plural:
                if isinstance(f, dict) and f:
                    entries.append(f)
        if not entries:
            return None, {}

        # symbol = "BASE/QUOTE" — quote currency is what we'd call USD
        # for ".../USDT" pairs etc.
        base_quote = self._normalise_symbol(market).split("/", 1)
        quote_ccy = base_quote[1].upper() if len(base_quote) == 2 else ""
        usd_total = 0.0
        breakdown: dict[str, float] = {}
        for f in entries:
            cost = float(f.get("cost") or 0.0)
            ccy = str(f.get("currency") or "").upper()
            if cost == 0.0:
                continue
            breakdown[ccy] = breakdown.get(ccy, 0.0) + cost
            usd = self._fee_cost_to_usd(
                cost=cost, ccy=ccy, quote_ccy=quote_ccy,
                avg_price=avg_price, base_market=market,
            )
            if usd is None:
                # Couldn't resolve a conversion — return what we know
                # in the breakdown but leave fee_usd as None so the
                # caller knows it's incomplete rather than zero.
                return None, breakdown
            usd_total += usd
        return usd_total, breakdown

    _USD_STABLES = frozenset({"USDT", "USDC", "BUSD", "FDUSD", "USD", "TUSD", "DAI"})

    def _fee_cost_to_usd(
        self,
        *,
        cost: float,
        ccy: str,
        quote_ccy: str,
        avg_price: float | None,
        base_market: str,
    ) -> float | None:
        if ccy in self._USD_STABLES:
            return cost
        # Fee paid in the quote currency — same dollar.
        if quote_ccy and ccy == quote_ccy and quote_ccy in self._USD_STABLES:
            return cost
        # Fee paid in the base currency of the same market — use avg_price.
        base_quote = self._normalise_symbol(base_market).split("/", 1)
        base_ccy = base_quote[0].upper() if len(base_quote) == 2 else ""
        if ccy == base_ccy and avg_price and avg_price > 0:
            return cost * float(avg_price)
        # Last resort — try a public ticker for ``ccy/USDT``.
        for stable in ("USDT", "USDC", "USD"):
            try:
                price = float(
                    self.client.fetch_ticker(f"{ccy}/{stable}").get("last") or 0.0
                )
            except Exception:
                price = 0.0
            if price > 0:
                return cost * price
        return None


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


def _extract_bracket_order_ids(raw: dict[str, Any]) -> dict[str, str]:
    """Pull native SL/TP bracket order ids out of a create_order response.

    Bybit V5 returns attached stop orders under ``stopLossOrder*`` /
    ``takeProfitOrder*`` keys; other venues surface them under
    ``stopLossOrderId`` / ``takeProfitOrderId`` or in an ``attachOrder``
    list. We harvest every known shape so the executor can record them
    for protection accounting and reconciliation.
    """
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    # Bybit V5 specific: stopLoss / takeProfit can be dict or scalar.
    sl = raw.get("stopLoss")
    tp = raw.get("takeProfit")
    sl_id: str | None = None
    tp_id: str | None = None
    if isinstance(sl, dict):
        sl_id = sl.get("orderId") or sl.get("id")
    elif isinstance(sl, str):
        sl_id = sl or None
    sl_id = sl_id or raw.get("stopLossOrderId")
    if isinstance(tp, dict):
        tp_id = tp.get("orderId") or tp.get("id")
    elif isinstance(tp, str):
        tp_id = tp or None
    tp_id = tp_id or raw.get("takeProfitOrderId")
    if sl_id:
        out["stop_loss"] = str(sl_id)
    if tp_id:
        out["take_profit"] = str(tp_id)
    # Generic attachOrder list (some venues)
    attached = raw.get("attachOrders") or raw.get("attachedOrders")
    if isinstance(attached, list):
        for ao in attached:
            if not isinstance(ao, dict):
                continue
            ao_id = str(ao.get("id") or ao.get("orderId") or "")
            ao_type = str(ao.get("type") or ao.get("stopType") or "").lower()
            if not ao_id:
                continue
            if "loss" in ao_type or "stop" in ao_type:
                out.setdefault("stop_loss", ao_id)
            elif "profit" in ao_type or "take" in ao_type:
                out.setdefault("take_profit", ao_id)
    return out


def _raw_order_to_ack(conn: "CcxtConnector", r: dict[str, Any]) -> OrderAck:
    """Map a raw ccxt order/trade dict to :class:`OrderAck` for reconciliation."""
    avg = float(r.get("average") or 0.0) or None
    fee_usd, fee_breakdown = conn._extract_fee_usd(
        r, market=str(r.get("symbol") or ""), avg_price=avg,
    )
    return OrderAck(
        order_id=str(r.get("id") or ""),
        client_order_id=str(r.get("clientOrderId") or r.get("client_order_id") or ""),
        status=_map_order_status(r.get("status")),
        market=str(r.get("symbol") or ""),
        side=str(r.get("side") or ""),
        price=float(r.get("price") or 0.0) or None,
        size=float(r.get("amount") or 0.0) or None,
        filled=float(r.get("filled") or r.get("amount") or 0.0) or None,
        avg_price=avg,
        fee_usd=fee_usd,
        fee_breakdown=fee_breakdown,
        raw=dict(r),
    )


__all__ = ["CcxtConnector", "supported_exchanges"]
