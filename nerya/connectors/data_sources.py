"""Read-only data-source connectors.

These wrap public APIs (Tushare, AkShare, Polygon.io, CoinGecko,
CoinMarketCap, Glassnode, Dune, Tencent quote, MOEX ISS, Messari)
behind the standard :class:`Connector` interface. ``place_order`` is
intentionally inherited from the base — every call raises so paper
strategies can use the data without ever touching a trading endpoint.

Each connector lazy-imports its upstream SDK so the rest of Nerya
keeps working when the optional dependency isn't installed yet — the
intake / install endpoints surface the install command instead.

Market id conventions
---------------------

* Tushare / AkShare / Tencent — ``<VENUE>:<symbol>`` where ``<symbol>``
  follows the upstream code (e.g. ``600519.SH`` / ``00700.HK`` /
  ``sh600519`` / ``hk00700``). The connector strips the ``<VENUE>:``
  prefix verbatim.
* Polygon.io / CoinGecko / Messari / CoinMarketCap — ``<VENUE>:<id>``.
* Glassnode — ``GLASSNODE:<asset>:<metric>``; the metric portion maps
  to a Glassnode endpoint (``market/price_usd_close`` by default).
* Dune — ``DUNE:<query_id>`` (klines = the rows of a saved query).
* MOEX — ``MOEX:<ticker>`` (e.g. ``MOEX:SBER``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .base import CEXConnectorBase, Ticker


@dataclass
class DataSourceCredentials:
    api_key: str = ""
    api_secret: str = ""
    extras: dict[str, Any] | None = None


def _strip_prefix(market: str) -> str:
    return market.split(":", 1)[-1] if ":" in market else market


def _env_fallback_api_key(env_names: tuple[str, ...]) -> str:
    """Return the first non-empty env var from ``env_names``, else ``""``.

    Gives operators a one-line route to enable a data-source connector
    without having to register a full account row + vault entry.
    Used by Tushare / Polygon / etc.'s ``_client()`` when the credential
    bundle came in empty (e.g. agent's ``market_data`` tool builds with
    no operator-supplied credentials).

    Order matters: the canonical name is listed first, legacy synonyms
    come after.
    """
    import os as _os
    for name in env_names:
        val = (_os.environ.get(name) or "").strip()
        if val:
            return val
    return ""


def _resolve_extra_headers(creds: "DataSourceCredentials") -> dict[str, str]:
    """Pull operator-supplied auth headers off the credential bundle.

    ``provider_spec._data_source_creds`` packs the workspace + vault
    passphrase + raw header dict into ``extras``; this helper resolves
    any ``vault://...`` substitutions and returns a clean header map
    suitable for ``urllib.request.Request.add_header``. Returns ``{}``
    when no extras are present so callers can safely merge.
    """

    extras = (creds.extras or {}) if creds else {}
    headers = extras.get("headers") if isinstance(extras, dict) else None
    if not headers:
        return {}
    from .http_auth import resolve_headers
    return resolve_headers(
        headers,
        workspace=extras.get("workspace") if isinstance(extras, dict) else None,
        vault_passphrase=extras.get("vault_passphrase")
            if isinstance(extras, dict) else None,
    )


class _ReadOnlyMixin:
    """Marks a connector as read-only and supplies a shared ``get_klines``
    fallback that derives recent ohlc from the latest ticker — handy when
    the upstream API doesn't expose intraday candles."""

    def get_klines(self, market: str, *, interval: str = "1m",
                    limit: int = 100) -> list[list[Any]]:
        return []


# ---------------------------------------------------------------------------
# Tushare (China A-shares + futures)
# ---------------------------------------------------------------------------


class TushareConnector(_ReadOnlyMixin, CEXConnectorBase):
    venue = "TUSHARE"
    kind = "data_source"

    def __init__(self, credentials: DataSourceCredentials | None = None, **_):
        self.credentials = credentials or DataSourceCredentials()
        self._pro = None

    def _client(self):
        if self._pro is not None:
            return self._pro
        try:
            import tushare as ts  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "Tushare connector needs `pip install tushare`."
            ) from exc
        token = self.credentials.api_key or _env_fallback_api_key(
            ("TUSHARE_TOKEN", "TUSHARE_API_KEY", "NERYA_TUSHARE_TOKEN"),
        )
        if not token:
            raise RuntimeError(
                "Tushare requires an API token. Either register an account "
                "row in accounts.yml with a vault credential ref, or set the "
                "TUSHARE_TOKEN env var. Free tokens at "
                "https://tushare.pro/user/token."
            )
        ts.set_token(token)
        self._pro = ts.pro_api()
        return self._pro

    def get_ticker(self, market: str) -> Ticker:
        pro = self._client()
        symbol = _strip_prefix(market).upper()
        df = pro.daily(ts_code=symbol, limit=1)
        if df is None or len(df) == 0:
            raise RuntimeError(f"Tushare returned no daily row for {symbol!r}")
        row = df.iloc[0]
        last = float(row["close"])
        return Ticker(
            market=market, bid=last, ask=last, mid=last, last=last,
            spread_bps=0.0, ts_ms=int(time.time() * 1000), venue=self.venue,
        )

    def get_klines(self, market: str, *, interval: str = "1m",
                    limit: int = 100) -> list[list[Any]]:
        pro = self._client()
        symbol = _strip_prefix(market).upper()
        df = pro.daily(ts_code=symbol, limit=int(limit))
        if df is None:
            return []
        out: list[list[Any]] = []
        for _, row in df.iloc[::-1].iterrows():
            ts_ms = 0
            try:
                from datetime import datetime
                ts_ms = int(datetime.strptime(str(row["trade_date"]),
                                                  "%Y%m%d").timestamp() * 1000)
            except Exception:
                ts_ms = 0
            out.append([
                ts_ms, float(row["open"]), float(row["high"]),
                float(row["low"]), float(row["close"]),
                float(row.get("vol") or 0.0),
            ])
        return out


# ---------------------------------------------------------------------------
# AkShare (multi-market open data, no token)
# ---------------------------------------------------------------------------


class AkShareConnector(_ReadOnlyMixin, CEXConnectorBase):
    venue = "AKSHARE"
    kind = "data_source"

    def __init__(self, credentials: DataSourceCredentials | None = None, **_):
        self.credentials = credentials or DataSourceCredentials()
        self._ak = None

    def _client(self):
        if self._ak is None:
            try:
                import akshare as ak  # type: ignore
            except Exception as exc:
                raise RuntimeError(
                    "AkShare connector needs `pip install akshare`."
                ) from exc
            self._ak = ak
        return self._ak

    def get_ticker(self, market: str) -> Ticker:
        ak = self._client()
        symbol = _strip_prefix(market)
        # Try real-time quote API (CN A-share); fallback to last close
        try:
            df = ak.stock_zh_a_spot_em()
            row = df[df["代码"] == symbol].iloc[0]
            last = float(row["最新价"])
        except Exception:
            try:
                df = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                          adjust="", end_date="")
                last = float(df.iloc[-1]["收盘"])
            except Exception as exc:
                raise RuntimeError(
                    f"AkShare cannot fetch ticker for {symbol!r}: {exc}"
                )
        return Ticker(
            market=market, bid=last, ask=last, mid=last, last=last,
            spread_bps=0.0, ts_ms=int(time.time() * 1000), venue=self.venue,
        )


# ---------------------------------------------------------------------------
# Polygon.io (US equities + crypto)
# ---------------------------------------------------------------------------


class PolygonConnector(_ReadOnlyMixin, CEXConnectorBase):
    venue = "POLYGON"
    kind = "data_source"

    def __init__(self, credentials: DataSourceCredentials | None = None, **_):
        self.credentials = credentials or DataSourceCredentials()
        self._client_ = None

    def _client(self):
        if self._client_ is not None:
            return self._client_
        try:
            from polygon import RESTClient  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "Polygon connector needs `pip install polygon-api-client`."
            ) from exc
        api_key = self.credentials.api_key or _env_fallback_api_key(
            ("POLYGON_API_KEY", "POLYGONIO_API_KEY", "NERYA_POLYGON_API_KEY"),
        )
        if not api_key:
            raise RuntimeError(
                "Polygon.io requires an API key. Either register an account "
                "row in accounts.yml with a vault credential ref, or set the "
                "POLYGON_API_KEY env var. Free keys at "
                "https://polygon.io/dashboard/api-keys."
            )
        self._client_ = RESTClient(api_key=api_key)
        return self._client_

    def get_ticker(self, market: str) -> Ticker:
        client = self._client()
        symbol = _strip_prefix(market).upper()
        snapshot = client.get_snapshot_ticker(market_type="stocks", ticker=symbol)
        last = float(getattr(snapshot.last_trade, "p", 0.0) or 0.0)
        bid = float(getattr(snapshot, "min", None).c if hasattr(snapshot, "min") and snapshot.min else last)
        return Ticker(
            market=market, bid=last, ask=last, mid=last, last=last,
            spread_bps=0.0, ts_ms=int(time.time() * 1000), venue=self.venue,
        )

    def get_klines(self, market: str, *, interval: str = "1m",
                    limit: int = 100) -> list[list[Any]]:
        client = self._client()
        symbol = _strip_prefix(market).upper()
        from datetime import datetime, timedelta, timezone

        unit_map = {
            "1m": ("minute", 1), "5m": ("minute", 5), "15m": ("minute", 15),
            "30m": ("minute", 30), "1h": ("hour", 1), "4h": ("hour", 4),
            "1d": ("day", 1),
        }
        timespan, mult = unit_map.get(interval, ("minute", 1))
        end = datetime.now(timezone.utc)
        if timespan == "day":
            start = end - timedelta(days=int(limit) + 5)
        elif timespan == "hour":
            start = end - timedelta(hours=mult * (int(limit) + 5))
        else:
            start = end - timedelta(minutes=mult * (int(limit) + 5))
        out: list[list[Any]] = []
        for agg in client.get_aggs(
            ticker=symbol, multiplier=mult, timespan=timespan,
            from_=start.strftime("%Y-%m-%d"), to=end.strftime("%Y-%m-%d"),
            limit=int(limit),
        ):
            out.append([
                int(getattr(agg, "timestamp", 0)),
                float(agg.open), float(agg.high), float(agg.low),
                float(agg.close), float(getattr(agg, "volume", 0.0)),
            ])
        return out[-int(limit):]


# ---------------------------------------------------------------------------
# CoinGecko (free + pro)
# ---------------------------------------------------------------------------


class CoinGeckoConnector(_ReadOnlyMixin, CEXConnectorBase):
    venue = "COINGECKO"
    kind = "data_source"

    def __init__(self, credentials: DataSourceCredentials | None = None, **_):
        self.credentials = credentials or DataSourceCredentials()
        self._client_ = None

    def _client(self):
        if self._client_ is not None:
            return self._client_
        try:
            from pycoingecko import CoinGeckoAPI  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "CoinGecko connector needs `pip install pycoingecko`."
            ) from exc
        if self.credentials.api_key:
            self._client_ = CoinGeckoAPI(api_key=self.credentials.api_key)
        else:
            self._client_ = CoinGeckoAPI()
        return self._client_

    def get_ticker(self, market: str) -> Ticker:
        cg = self._client()
        coin = _strip_prefix(market).lower()
        prices = cg.get_price(ids=coin, vs_currencies="usd",
                              include_24hr_change=False)
        last = float(prices.get(coin, {}).get("usd", 0.0))
        return Ticker(
            market=market, bid=last, ask=last, mid=last, last=last,
            spread_bps=0.0, ts_ms=int(time.time() * 1000), venue=self.venue,
        )


# ---------------------------------------------------------------------------
# CoinMarketCap (REST, no SDK)
# ---------------------------------------------------------------------------


class CoinMarketCapConnector(_ReadOnlyMixin, CEXConnectorBase):
    venue = "CMC"
    kind = "data_source"

    BASE_URL = "https://pro-api.coinmarketcap.com/v1"

    def __init__(self, credentials: DataSourceCredentials | None = None, **_):
        self.credentials = credentials or DataSourceCredentials()

    def _headers(self) -> dict[str, str]:
        if not self.credentials.api_key:
            raise RuntimeError("CoinMarketCap requires an API key.")
        out = {"X-CMC_PRO_API_KEY": self.credentials.api_key,
               "Accept": "application/json"}
        out.update(_resolve_extra_headers(self.credentials))
        return out

    def get_ticker(self, market: str) -> Ticker:
        import urllib.parse
        import urllib.request
        import json as _json

        symbol = _strip_prefix(market).upper()
        url = (f"{self.BASE_URL}/cryptocurrency/quotes/latest"
               f"?symbol={urllib.parse.quote(symbol)}&convert=USD")
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = _json.loads(resp.read())
        data = (payload.get("data") or {}).get(symbol) or {}
        quote = (data.get("quote") or {}).get("USD") or {}
        last = float(quote.get("price", 0.0))
        return Ticker(
            market=market, bid=last, ask=last, mid=last, last=last,
            spread_bps=0.0, ts_ms=int(time.time() * 1000), venue=self.venue,
        )


# ---------------------------------------------------------------------------
# Glassnode (on-chain metrics)
# ---------------------------------------------------------------------------


class GlassnodeConnector(_ReadOnlyMixin, CEXConnectorBase):
    venue = "GLASSNODE"
    kind = "data_source"

    BASE_URL = "https://api.glassnode.com/v1/metrics"

    def __init__(self, credentials: DataSourceCredentials | None = None, **_):
        self.credentials = credentials or DataSourceCredentials()

    @staticmethod
    def _parse(market: str) -> tuple[str, str]:
        body = _strip_prefix(market)
        if ":" in body:
            asset, metric = body.split(":", 1)
        else:
            asset, metric = body, "market/price_usd_close"
        return asset.upper(), metric.strip("/")

    def get_ticker(self, market: str) -> Ticker:
        import urllib.parse
        import urllib.request
        import json as _json

        if not self.credentials.api_key:
            raise RuntimeError("Glassnode requires an API key.")
        asset, metric = self._parse(market)
        params = urllib.parse.urlencode({
            "a": asset, "i": "24h", "api_key": self.credentials.api_key,
        })
        url = f"{self.BASE_URL}/{metric}?{params}"
        req = urllib.request.Request(url)
        for k, v in _resolve_extra_headers(self.credentials).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = _json.loads(resp.read())
        if not rows:
            raise RuntimeError(f"Glassnode returned no rows for {asset}/{metric}")
        last = float(rows[-1].get("v", 0.0) or 0.0)
        return Ticker(
            market=market, bid=last, ask=last, mid=last, last=last,
            spread_bps=0.0, ts_ms=int(time.time() * 1000), venue=self.venue,
        )


# ---------------------------------------------------------------------------
# Dune Analytics (saved queries)
# ---------------------------------------------------------------------------


class DuneConnector(_ReadOnlyMixin, CEXConnectorBase):
    venue = "DUNE"
    kind = "data_source"

    def __init__(self, credentials: DataSourceCredentials | None = None, **_):
        self.credentials = credentials or DataSourceCredentials()
        self._client_ = None

    def _client(self):
        if self._client_ is not None:
            return self._client_
        try:
            from dune_client.client import DuneClient  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "Dune connector needs `pip install dune-client`."
            ) from exc
        if not self.credentials.api_key:
            raise RuntimeError("Dune requires an API key.")
        self._client_ = DuneClient(self.credentials.api_key)
        return self._client_

    def get_ticker(self, market: str) -> Ticker:
        client = self._client()
        try:
            query_id = int(_strip_prefix(market))
        except ValueError as exc:
            raise RuntimeError(
                f"Dune market must be DUNE:<query_id>, got {market!r}"
            ) from exc
        result = client.get_latest_result(query_id)
        rows = result.get_rows() if hasattr(result, "get_rows") else []
        last = float(rows[0].get("value", 0.0)) if rows else 0.0
        return Ticker(
            market=market, bid=last, ask=last, mid=last, last=last,
            spread_bps=0.0, ts_ms=int(time.time() * 1000), venue=self.venue,
        )


# ---------------------------------------------------------------------------
# Tencent stock quote (free, no token)
# ---------------------------------------------------------------------------


class TencentConnector(_ReadOnlyMixin, CEXConnectorBase):
    venue = "TENCENT"
    kind = "data_source"

    BASE_URL = "https://qt.gtimg.cn/q={code}"

    def __init__(self, credentials: DataSourceCredentials | None = None, **_):
        self.credentials = credentials or DataSourceCredentials()

    def get_ticker(self, market: str) -> Ticker:
        import urllib.request

        code = _strip_prefix(market).lower()
        url = self.BASE_URL.format(code=code)
        with urllib.request.urlopen(url, timeout=10) as resp:
            text = resp.read().decode("gbk", errors="replace")
        try:
            payload = text.split('"', 2)[1]
            parts = payload.split("~")
            last = float(parts[3])
            bid = float(parts[9]) if len(parts) > 9 and parts[9] else last
            ask = float(parts[19]) if len(parts) > 19 and parts[19] else last
        except (IndexError, ValueError):
            last = bid = ask = 0.0
        mid = (bid + ask) / 2 if bid and ask else last
        spread_bps = ((ask - bid) / mid * 10_000.0) if mid else 0.0
        return Ticker(
            market=market, bid=bid, ask=ask, mid=mid, last=last,
            spread_bps=spread_bps,
            ts_ms=int(time.time() * 1000), venue=self.venue,
        )


# ---------------------------------------------------------------------------
# MOEX (Russian Stock Exchange ISS)
# ---------------------------------------------------------------------------


class MOEXConnector(_ReadOnlyMixin, CEXConnectorBase):
    venue = "MOEX"
    kind = "data_source"

    def __init__(self, credentials: DataSourceCredentials | None = None, **_):
        self.credentials = credentials or DataSourceCredentials()

    def get_ticker(self, market: str) -> Ticker:
        try:
            import apimoex  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "MOEX connector needs `pip install apimoex`."
            ) from exc
        import requests
        symbol = _strip_prefix(market).upper()
        with requests.Session() as session:
            data = apimoex.get_market_candles(
                session, security=symbol, interval=1,
            )
        if not data:
            raise RuntimeError(f"MOEX returned no candles for {symbol}")
        last = float(data[-1]["close"])
        return Ticker(
            market=market, bid=last, ask=last, mid=last, last=last,
            spread_bps=0.0, ts_ms=int(time.time() * 1000), venue=self.venue,
        )


# ---------------------------------------------------------------------------
# Messari (REST, free public endpoints)
# ---------------------------------------------------------------------------


class MessariConnector(_ReadOnlyMixin, CEXConnectorBase):
    venue = "MESSARI"
    kind = "data_source"

    BASE_URL = "https://data.messari.io/api/v1"

    def __init__(self, credentials: DataSourceCredentials | None = None, **_):
        self.credentials = credentials or DataSourceCredentials()

    def get_ticker(self, market: str) -> Ticker:
        import urllib.request
        import json as _json

        slug = _strip_prefix(market).lower()
        url = f"{self.BASE_URL}/assets/{slug}/metrics/market-data"
        req = urllib.request.Request(url)
        if self.credentials.api_key:
            req.add_header("x-messari-api-key", self.credentials.api_key)
        for k, v in _resolve_extra_headers(self.credentials).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = _json.loads(resp.read())
        data = ((payload.get("data") or {}).get("market_data") or {})
        last = float(data.get("price_usd") or 0.0)
        return Ticker(
            market=market, bid=last, ask=last, mid=last, last=last,
            spread_bps=0.0, ts_ms=int(time.time() * 1000), venue=self.venue,
        )


__all__ = [
    "DataSourceCredentials",
    "TushareConnector",
    "AkShareConnector",
    "PolygonConnector",
    "CoinGeckoConnector",
    "CoinMarketCapConnector",
    "GlassnodeConnector",
    "DuneConnector",
    "TencentConnector",
    "MOEXConnector",
    "MessariConnector",
]
