"""Runtime-facing market defaults.

Before this module existed, several hot paths hardcoded ``binance`` as the
default venue and ``BTCUSDT`` as the default symbol. That forced the runtime
into a very specific worldview and made it hard to operate the system with
a different primary venue or quote currency.

We keep a single small helper here so that every hot path resolves the same
operator preference. Inputs are read from ``workspace_preferences.market_defaults``
with safe fallbacks so operators that do not configure anything still get
a sensible default. Nothing here makes any network call.

The shape:

``venue`` – preferred venue identifier (``binance``, ``bybit``, ``okx``,
    ``hyperliquid`` ...). Always lowercase.
``symbol`` – preferred default symbol. Venue-native casing (``BTCUSDT``).
``quote`` – preferred quote asset (``USDT``). Used for alias expansion
    when the user only mentions the base token.
``aliases`` – free-form lower-case token -> symbol. Extends the built-in
    common-token map.
``preferred_venues`` – ordered list of venues the operator trusts for
    public market data. Used by UI/discovery; never used as a silent
    fallback on missing arguments.
"""

from __future__ import annotations

from typing import Any


# A conservative common-token map. Operators can extend it via
# ``workspace_preferences.market_defaults.aliases``.
_BUILTIN_ALIASES: dict[str, str] = {
    "btc": "BTC", "bitcoin": "BTC", "xbt": "BTC",
    "eth": "ETH", "ethereum": "ETH", "ether": "ETH",
    "sol": "SOL", "solana": "SOL",
    "bnb": "BNB",
    "xrp": "XRP", "ripple": "XRP",
    "ada": "ADA", "cardano": "ADA",
    "doge": "DOGE", "dogecoin": "DOGE",
    "matic": "MATIC", "polygon": "MATIC",
    "avax": "AVAX", "avalanche": "AVAX",
    "link": "LINK", "chainlink": "LINK",
    "arb": "ARB", "op": "OP",
    "sui": "SUI", "ton": "TON",
}


def _cfg_get(config_like: Any, path: str, default: Any = None) -> Any:
    """Tolerant ``config.get`` that also accepts raw dicts."""
    if config_like is None:
        return default
    getter = getattr(config_like, "get", None)
    if callable(getter):
        try:
            return getter(path, default)
        except TypeError:
            pass
    if isinstance(config_like, dict):
        cur: Any = config_like
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur
    return default


def resolve_market_defaults(config_like: Any) -> dict[str, Any]:
    """Return a normalised ``market_defaults`` mapping.

    Falls back to ``binance / BTCUSDT`` only when the operator has not
    explicitly configured anything. When the operator has pointed the
    runtime at a different venue / quote, every hot path observes the
    same preference.
    """
    raw = _cfg_get(config_like, "workspace_preferences.market_defaults") or {}
    if not isinstance(raw, dict):
        raw = {}

    venue = str(raw.get("venue") or "binance").lower()
    quote = str(raw.get("quote") or "USDT").upper()

    symbol_in = raw.get("symbol")
    if isinstance(symbol_in, str) and symbol_in.strip():
        symbol = symbol_in.strip().upper()
    else:
        # Keep the historical BTC default when the operator does not
        # pin a preferred symbol. Tied to the configured quote so that
        # operators pointed at a ``USDC`` or ``USD`` venue still get a
        # consistent default.
        symbol = f"BTC{quote}" if not quote.startswith("BTC") else "BTCUSDT"

    aliases: dict[str, str] = {k: v for k, v in _BUILTIN_ALIASES.items()}
    extra = raw.get("aliases") or {}
    if isinstance(extra, dict):
        for token, target in extra.items():
            if not isinstance(token, str) or not isinstance(target, str):
                continue
            aliases[token.strip().lower()] = target.strip().upper()

    preferred = raw.get("preferred_venues")
    if isinstance(preferred, list):
        preferred_venues = [str(v).lower() for v in preferred if isinstance(v, str)]
    else:
        preferred_venues = [venue]

    return {
        "venue": venue,
        "symbol": symbol,
        "quote": quote,
        "aliases": aliases,
        "preferred_venues": preferred_venues or [venue],
    }


def default_market_id(config_like: Any) -> str:
    """Return the operator's preferred ``VENUE:SYMBOL`` id.

    This is the id intent resolution / request defaults should fall
    back to when the user did not pick a market and the context is
    ambiguous.
    """
    d = resolve_market_defaults(config_like)
    return f"{d['venue'].upper()}:{d['symbol'].upper()}"


def resolve_symbol_for_token(config_like: Any, token: str) -> str | None:
    """Map a casual token (``"btc"``) to the operator's preferred symbol.

    Returns ``None`` when the token is not recognised. The caller decides
    how to combine this with the preferred venue to produce a
    ``VENUE:SYMBOL`` id.
    """
    if not token or not isinstance(token, str):
        return None
    d = resolve_market_defaults(config_like)
    hit = d["aliases"].get(token.strip().lower())
    if not hit:
        return None
    # If the alias already carries a quote (e.g. ``BTCUSDT``) use it as-is.
    if hit.endswith(d["quote"]) or len(hit) > 5:
        return hit
    return f"{hit}{d['quote']}"
