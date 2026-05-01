"""Generic HTTP data-source connector.

Lets operators (or the agent) plug in *any* read-only REST API
without writing a new connector class. The configuration lives on the
account row's ``provider_config`` dict and supports:

* ``url`` — full URL to fetch. Supports ``{symbol}`` substitution from
  the market id (the part after the ``HTTP:`` prefix).
* ``method`` — ``GET`` (default) or ``POST``.
* ``headers`` — dict of HTTP headers. Values may include
  ``vault://<ref>`` substrings; they are resolved via
  :mod:`nerya.connectors.http_auth`.
* ``params`` — query-string dict (``GET``).
* ``body`` — JSON body dict (``POST``).
* ``json_paths`` — dict mapping ``last`` / ``bid`` / ``ask`` /
  ``ts_ms`` to a dotted path inside the JSON response (e.g.
  ``{"last": "data.price", "ts_ms": "data.timestamp"}``).
* ``timeout_s`` — optional integer override (default 10).

This connector is intentionally minimal — it never places orders
and never tries to interpret venue-specific data shapes. For deeper
integrations, write a dedicated factory.
"""

from __future__ import annotations

import json as _json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .base import CEXConnectorBase, Ticker
from .http_auth import resolve_headers


@dataclass
class HttpSourceConfig:
    url: str = ""
    method: str = "GET"
    headers: dict[str, str] | None = None
    params: dict[str, Any] | None = None
    body: dict[str, Any] | None = None
    json_paths: dict[str, str] | None = None
    timeout_s: int = 10


class HttpDataSourceConnector(CEXConnectorBase):
    venue = "HTTP"
    kind = "data_source"

    def __init__(
        self,
        cfg: HttpSourceConfig | None = None,
        *,
        workspace: Any = None,
        vault_passphrase: str | None = None,
    ) -> None:
        self.cfg = cfg or HttpSourceConfig()
        self._workspace = workspace
        self._vault_passphrase = vault_passphrase

    # ------------------------------------------------------------ private
    def _strip_prefix(self, market: str) -> str:
        return market.split(":", 1)[-1] if ":" in market else market

    def _build_url(self, market: str) -> str:
        symbol = self._strip_prefix(market)
        url = self.cfg.url
        if "{symbol}" in url:
            url = url.replace("{symbol}", urllib.parse.quote(symbol, safe=""))
        if self.cfg.params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(self.cfg.params)
        return url

    def _fetch(self, market: str) -> Any:
        url = self._build_url(market)
        headers = resolve_headers(
            self.cfg.headers,
            workspace=self._workspace,
            vault_passphrase=self._vault_passphrase,
        )
        method = (self.cfg.method or "GET").upper()
        if method == "POST":
            body_bytes = _json.dumps(self.cfg.body or {}).encode("utf-8")
            req = urllib.request.Request(url, data=body_bytes, method="POST")
            headers.setdefault("Content-Type", "application/json")
        else:
            req = urllib.request.Request(url, method="GET")
        for k, v in headers.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=int(self.cfg.timeout_s)) as resp:
            payload = resp.read()
        try:
            return _json.loads(payload)
        except Exception:
            return {"_raw": payload.decode("utf-8", errors="replace")}

    @staticmethod
    def _dig(obj: Any, path: str) -> Any:
        cur = obj
        for piece in path.split("."):
            if cur is None:
                return None
            if isinstance(cur, dict):
                cur = cur.get(piece)
            elif isinstance(cur, list):
                try:
                    cur = cur[int(piece)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return cur

    # ------------------------------------------------------------ public
    def get_ticker(self, market: str) -> Ticker:
        if not self.cfg.url:
            raise RuntimeError(
                "HTTP data source requires provider_config.url; configure it "
                "via /accounts/upsert or the dashboard."
            )
        data = self._fetch(market)
        paths = self.cfg.json_paths or {}
        last = float(self._dig(data, paths.get("last") or "price") or 0.0)
        bid_path = paths.get("bid")
        ask_path = paths.get("ask")
        bid = float(self._dig(data, bid_path) or 0.0) if bid_path else last
        ask = float(self._dig(data, ask_path) or 0.0) if ask_path else last
        ts_path = paths.get("ts_ms")
        ts_val = self._dig(data, ts_path) if ts_path else None
        try:
            ts_ms = int(float(ts_val)) if ts_val is not None else int(time.time() * 1000)
        except (TypeError, ValueError):
            ts_ms = int(time.time() * 1000)
        mid = (bid + ask) / 2 if bid and ask else last
        spread_bps = ((ask - bid) / mid * 10_000.0) if mid and bid and ask else 0.0
        return Ticker(
            market=market, bid=bid, ask=ask, mid=mid, last=last,
            spread_bps=spread_bps, ts_ms=ts_ms, venue=self.venue,
        )


__all__ = ["HttpDataSourceConnector", "HttpSourceConfig"]
