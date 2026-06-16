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
    resolve_allow_mock,
)
from ..data.candles import (
    discover_market_data_sources,
    fetch_candles,
    fetch_public_ticker,
)


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
    for source in discover_market_data_sources(config_like):
        name = str(source.get("venue") or "").strip().lower()
        if not name or name in seen:
            continue
        if source.get("origin") != "built_in_onchain_geckoterminal" and not str(
            source.get("origin") or ""
        ).startswith("wallet.providers"):
            continue
        seen.add(name)
        out.append({
            "name": name,
            "label": source.get("label") or (
                "On-chain DEX" if name == "onchain" else name.replace("_", " ").title()
            ),
            "public": True,
            "mode": "live",
            "kind": "data_source",
            "runtime": "http",
            "instrument_types": [
                "onchain" if source.get("market_format") == "chain:token" or name == "onchain"
                else "spot"
            ],
            "aliases": [],
            "origin": source.get("origin"),
            "market_format": source.get("market_format"),
            "wallet_id": source.get("wallet_id"),
            "provider": source.get("provider"),
        })
    out.sort(key=lambda v: v["name"])
    if resolve_allow_mock(None, config_like):
        out.extend(_PAPER_VENUES)
    return out


def _normalize_market(venue: str, market: str) -> str:
    """Return a ``VENUE:SYMBOL`` market id that fetch_candles understands."""
    venue_l = (venue or "").strip().lower()
    wallet_prefixes = {
        "okx_onchain": "OKX_ONCHAIN",
        "okx_os": "OKX_ONCHAIN",
        "bitget_onchain": "BITGET_ONCHAIN",
        "bitget_wallet": "BITGET_ONCHAIN",
        "binance_alpha": "BINANCE_ALPHA",
        "binance_web3": "BINANCE_ALPHA",
        "coinbase_wallet": "COINBASE_WALLET",
        "coinbase_exchange_wallet": "COINBASE_WALLET",
        "byreal": "BYREAL_ONCHAIN",
        "byreal_onchain": "BYREAL_ONCHAIN",
        "byreal_cli": "BYREAL_ONCHAIN",
        "byreal_solana": "BYREAL_ONCHAIN",
        "onchain": "ONCHAIN",
    }
    if venue_l in wallet_prefixes:
        prefix = wallet_prefixes[venue_l]
        if market.upper().startswith(f"{prefix}:"):
            return market
        return f"{prefix}:{market}"
    if ":" in market:
        return market
    return f"{venue.upper()}:{market.upper()}"


def _resolve_venue_and_market(payload: dict[str, Any], defaults: dict[str, Any]) -> tuple[str, str]:
    market = str(payload.get("market") or payload.get("symbol") or defaults["symbol"])
    raw_venue = payload.get("venue") or payload.get("source")
    if raw_venue:
        venue = str(raw_venue)
    elif ":" in market:
        venue = market.split(":", 1)[0]
    else:
        venue = str(defaults["venue"])
    return venue, _normalize_market(venue, market)


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
        count = int(payload.get("count") or payload.get("limit") or 96)
        interval = str(payload.get("interval") or "1m")

        venue, market_id = _resolve_venue_and_market(payload, d)
        try:
            rows = fetch_candles(
                market_id,
                count=count,
                interval=interval,
                allow_mock=None,
                config_like=cfg,
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
        elif not rows:
            envelope = degraded_envelope(
                "candles",
                error="no_rows",
                venue=venue.lower(),
            ).as_dict()
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
        venue, market_id = _resolve_venue_and_market(payload, d)
        try:
            t = fetch_public_ticker(market_id, allow_mock=None, config_like=cfg)
            env = t.get("_envelope") or {}
            price = float(t.get("price") or 0.0)
            if not price or env.get("mode") != "live":
                return {
                    "venue": venue,
                    "market": market_id,
                    "error": (env.get("error") or "ticker_unavailable"),
                    "_envelope": env,
                }
            return {
                "venue": env.get("venue") or venue,
                "market": market_id,
                "bid": None,
                "ask": None,
                "mid": price,
                "last": price,
                "age_s": t.get("age_s", 0),
                "_envelope": env,
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
