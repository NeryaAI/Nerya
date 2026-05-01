"""DeFi protocol metrics — DefiLlama API (free, no auth).

Endpoints used:
    * ``GET https://api.llama.fi/tvl/<slug>`` → current TVL (float USD)
    * ``GET https://api.llama.fi/protocol/<slug>`` → full doc with history

On error we fall back to :func:`mock_tvl` for deterministic tests.
"""

from __future__ import annotations

import logging
from typing import Any

from ..connectors.http import HttpTransport, UrllibHttp
from ..core.truth import (
    degraded_envelope,
    live_envelope,
    mock_envelope,
    resolve_allow_mock,
)

log = logging.getLogger(__name__)

_BASE = "https://api.llama.fi"


def fetch_tvl(
    protocol: str,
    *,
    transport: HttpTransport | None = None,
    allow_mock: bool | None = None,
    config_like=None,
) -> dict:
    """Return ``{protocol, tvl_usd, tvl_change_24h_pct, source}``.

    ``protocol`` is a DefiLlama slug (e.g. ``aave``, ``lido``, ``uniswap``).
    Mock fallback requires explicit authorisation via ``allow_mock`` or the
    ``NERYA_ALLOW_MOCK_DATA`` environment variable.
    """
    http = transport or UrllibHttp(rate_limit_per_sec=4.0)
    slug = protocol.lower().strip()

    def _degraded(err: str) -> dict:
        if resolve_allow_mock(allow_mock, config_like):
            return mock_tvl(protocol)
        return {
            "protocol": slug,
            "tvl_usd": 0.0,
            "tvl_change_24h_pct": 0.0,
            "chains": [],
            "category": "",
            "source": "unavailable",
            "_envelope": degraded_envelope("defi", error=err).as_dict(),
        }

    try:
        status, body = http.request("GET", f"{_BASE}/protocol/{slug}", timeout=15.0)
    except Exception as exc:
        log.debug("defillama fetch failed %s: %s", slug, exc)
        return _degraded(f"{type(exc).__name__}")
    if status >= 400 or not isinstance(body, dict):
        return _degraded(f"http_{status}")

    cur = body.get("currentChainTvls") or {}
    tvl_usd = float(sum(float(v or 0.0) for v in cur.values()))
    history = body.get("tvl") or []
    change_pct = 0.0
    if isinstance(history, list) and len(history) >= 2:
        try:
            last = float(history[-1].get("totalLiquidityUSD") or 0.0)
            day_idx = max(0, len(history) - 2)
            prev = float(history[day_idx].get("totalLiquidityUSD") or 0.0)
            if prev > 0:
                change_pct = (last - prev) / prev
        except Exception:
            change_pct = 0.0
    return {
        "protocol": slug,
        "tvl_usd": tvl_usd,
        "tvl_change_24h_pct": change_pct,
        "chains": body.get("chains") or [],
        "category": body.get("category") or "",
        "source": "defillama",
        "_envelope": live_envelope(source="defillama").as_dict(),
    }


def mock_tvl(protocol: str) -> dict:
    return {
        "protocol": protocol,
        "tvl_usd": 1_000_000_000,
        "tvl_change_24h_pct": 0.02,
        "chains": ["Ethereum"],
        "category": "",
        "source": "mock",
        "_envelope": mock_envelope(source="mock").as_dict(),
    }


__all__ = ["fetch_tvl", "mock_tvl"]
