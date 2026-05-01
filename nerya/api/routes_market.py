"""Market data HTTP endpoints.

Exposes a thin candles / ticker / venue-discovery surface so the dashboard
can render real K-line charts and the user can pick which venue to pull
from at runtime.
"""

from __future__ import annotations

from typing import Any

from ..connectors.provider_spec import get_registry
from ..connectors.registry import build_connector
from ..connectors.mock_exchange import MockExchange
from ..core.market_defaults import resolve_market_defaults
from ..core.truth import (
    degraded_envelope,
    live_envelope,
    mock_envelope,
    resolve_allow_mock,
)
from ..data.candles import fetch_candles


# Mock/paper venues exposed only when runtime authorises mock mode.
_PAPER_VENUES = [
    {"name": "mock", "label": "Mock (deterministic)", "public": True, "mode": "mock"},
    {"name": "paper", "label": "Paper preset", "public": True, "mode": "paper"},
]

# The registry's mock/paper-only specs — never surfaced as live operator
# venues because they don't fetch real candles.
_MOCK_REGISTRY_IDS = {"mock", "mock_chain", "paper", "paper_chain"}


def _public_venues(config_like=None) -> list[dict]:
    """Derive the public venue catalog from the connector registry.

    We surface every registered spec whose factory can be built with
    public-only config (i.e. without vault creds). That means ccxt-based
    CEX providers, Polymarket, and any user-authored provider the
    operator hot-loaded, without having to edit this list.
    """
    reg = get_registry()
    # Ensure workspace-authored providers are reflected even if the
    # registry hasn't been touched yet in this process.
    workspace = None
    if config_like is not None:
        paths = getattr(config_like, "paths", None)
        if paths is not None:
            workspace = getattr(paths, "root", None)
    if workspace is not None:
        try:
            reg.reload_workspace(workspace)
        except Exception:  # pragma: no cover — never block the HTTP surface
            pass
    out: list[dict] = []
    seen: set[str] = set()
    for spec in reg.list_specs():
        if spec.id in _MOCK_REGISTRY_IDS:
            continue
        # Skip the catch-all ``ccxt`` alias bundle — its individual venues
        # show up via their own specs when registered.
        if spec.id == "ccxt":
            continue
        # Primary name: user:<slug> collapses to <slug> for UX.
        primary = spec.id.split(":", 1)[-1]
        if primary in seen:
            continue
        seen.add(primary)
        info = spec.to_info()
        out.append({
            "name": primary,
            "label": info.get("label") or primary.title(),
            "public": True,
            "mode": "live",
            "kind": info.get("kind"),
            "runtime": info.get("runtime"),
            "instrument_types": info.get("instrument_types") or [],
            "aliases": info.get("aliases") or [],
        })
    out.sort(key=lambda v: v["name"])
    if resolve_allow_mock(None, config_like):
        out.extend(_PAPER_VENUES)
    return out


def _normalize_market(venue: str, market: str) -> str:
    """Return a ``VENUE:SYMBOL`` market id that fetch_candles understands."""
    if ":" in market:
        return market
    return f"{venue.upper()}:{market.upper()}"


def _public_connector(venue: str, *, config_like=None):
    """Resolve a public connector for ``venue``.

    Mock/paper venues require explicit mock authorisation. Unknown venues
    return ``None`` so the caller can raise a degraded response instead of
    silently falling back to fake candles.
    """
    venue_l = (venue or "").lower()
    allow_mock = resolve_allow_mock(None, config_like)
    if venue_l in ("mock", "paper", ""):
        if allow_mock:
            return MockExchange()
        return None
    reg = get_registry()
    spec = reg.find(venue_l)
    if spec is None or spec.id in _MOCK_REGISTRY_IDS:
        return None
    try:
        return build_connector({"venue": venue_l, "live": False})
    except Exception:
        return MockExchange() if allow_mock else None


def routes():
    def venues(_client, _payload):
        cfg = getattr(_client, "config", None) if _client is not None else None
        return {"venues": _public_venues(cfg)}

    def candles(_client, payload):
        cfg = getattr(_client, "config", None) if _client is not None else None
        d = resolve_market_defaults(cfg)
        venue = str(payload.get("venue") or payload.get("source") or d["venue"])
        market = str(payload.get("market") or payload.get("symbol") or d["symbol"])
        count = int(payload.get("count") or payload.get("limit") or 96)
        interval = str(payload.get("interval") or "1m")

        market_id = _normalize_market(venue, market)
        connector = _public_connector(venue, config_like=cfg)
        if connector is None:
            env = degraded_envelope("candles",
                                    error="venue_unavailable",
                                    venue=venue.lower()).as_dict()
            return {
                "venue": venue, "market": market_id, "interval": interval,
                "count": 0, "candles": [],
                "_envelope": env,
            }
        try:
            rows = fetch_candles(
                market_id, count=count, interval=interval,
                connector=connector, config_like=cfg,
            )
        except Exception as exc:  # pragma: no cover
            return {
                "venue": venue, "market": market_id, "interval": interval,
                "candles": [], "error": f"{type(exc).__name__}: {exc}",
                "_envelope": degraded_envelope(
                    "candles", error=f"{type(exc).__name__}",
                    venue=venue.lower(),
                ).as_dict(),
            }

        # Surface the envelope of the underlying rows at the top level too.
        if rows and isinstance(rows[0], dict) and rows[0].get("_envelope"):
            envelope = rows[0]["_envelope"]
        else:
            envelope = live_envelope(source=venue.lower(),
                                     venue=venue.lower()).as_dict()
        return {
            "venue": venue,
            "market": market_id,
            "interval": interval,
            "count": len(rows),
            "candles": rows,
            "_envelope": envelope,
        }

    def ticker(_client, payload):
        cfg = getattr(_client, "config", None) if _client is not None else None
        d = resolve_market_defaults(cfg)
        venue = str(payload.get("venue") or d["venue"])
        market = str(payload.get("market") or d["symbol"])
        market_id = _normalize_market(venue, market)
        connector = _public_connector(venue, config_like=cfg)
        if connector is None:
            return {
                "venue": venue, "market": market_id,
                "error": "venue_unavailable",
                "_envelope": degraded_envelope(
                    "ticker", error="venue_unavailable",
                    venue=venue.lower(),
                ).as_dict(),
            }
        try:
            t = connector.get_ticker(market_id)
            return {
                "venue": getattr(t, "venue", venue),
                "market": market_id,
                "bid": t.bid, "ask": t.ask, "mid": t.mid, "last": t.last,
                "ts_ms": getattr(t, "ts_ms", 0),
                "_envelope": live_envelope(source=venue.lower(),
                                           venue=venue.lower()).as_dict(),
            }
        except Exception as exc:  # pragma: no cover
            return {
                "venue": venue, "market": market_id,
                "error": f"{type(exc).__name__}: {exc}",
                "_envelope": degraded_envelope(
                    "ticker", error=f"{type(exc).__name__}",
                    venue=venue.lower(),
                ).as_dict(),
            }

    return [
        ("POST", "/market/candles", candles),
        ("POST", "/market/ticker", ticker),
        ("GET", "/market/venues", venues),
    ]
