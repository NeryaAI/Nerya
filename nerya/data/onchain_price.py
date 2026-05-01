"""On-chain price oracle.

Resolves a USD spot price for an arbitrary on-chain asset across
EVM chains (ethereum/bsc/polygon/arbitrum/base/optimism) and Solana
using free public DEX indexers. Every return value is tagged with a
:class:`nerya.core.truth.RuntimeEnvelope` so callers can see exactly
whether the price is live, mocked, or degraded.

The default resolution chain:

1. If a custom HTTP transport is injected (tests), use it verbatim.
2. Otherwise, query DEX Screener's public ``/tokens/{chain}/{address}``
   endpoint over HTTPS. This covers every major EVM chain + solana
   without API keys.
3. On failure, we do NOT silently fall back to mock — unless
   ``NERYA_ALLOW_MOCK_DATA=1`` authorises it. A degraded envelope
   is returned instead.

Strategy scripts can call this via the ``onchain.get_onchain_price``
skill action.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..connectors.http import HttpTransport, UrllibHttp
from ..core.truth import (
    degraded_envelope,
    live_envelope,
    mock_envelope,
    resolve_allow_mock,
)

log = logging.getLogger(__name__)


# DEX Screener lets us query any token by address on any supported chain
# in a single endpoint. Pair discovery is handled server-side, which
# means we do NOT need a Uniswap-v3 / Jupiter client in-process.
_DS_BASE = "https://api.dexscreener.com/latest/dex/tokens/{address}"


class _HttpLike(Protocol):
    def get_json(self, url: str, *, timeout: float = ...) -> dict[str, Any]: ...


@dataclass
class OnchainPrice:
    chain: str
    address: str
    price_usd: float | None
    pair_address: str | None = None
    liquidity_usd: float | None = None
    venue: str = "dexscreener"
    envelope: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "address": self.address,
            "price_usd": self.price_usd,
            "pair_address": self.pair_address,
            "liquidity_usd": self.liquidity_usd,
            "venue": self.venue,
            "_envelope": self.envelope,
        }


def get_onchain_price(
    chain: str,
    address: str,
    *,
    http: _HttpLike | None = None,
    allow_mock_override: bool | None = None,
    config: Any | None = None,
) -> OnchainPrice:
    """Return the current USD spot price of ``address`` on ``chain``.

    :param chain: lowercase chain id (``ethereum``, ``bsc``, ``solana`` ...)
    :param address: the token address on that chain.
    :param http: optional HTTP client with a ``get_json(url, timeout=...)``
        method. Injected by tests; defaults to :class:`UrllibHttp`.
    :param allow_mock_override: force-enable the mock fallback even if
        ``NERYA_ALLOW_MOCK_DATA`` is unset. Used by the preflight suite.
    :param config: an optional :class:`nerya.core.config.Config` to
        resolve truth-gate rules from.
    :returns: an :class:`OnchainPrice` with a :class:`RuntimeEnvelope`
        attached explaining whether the price is live, mock, or degraded.
    """
    chain_l = (chain or "").strip().lower()
    if not chain_l:
        env = degraded_envelope(
            "onchain_price", error="chain_required", venue="onchain",
        ).as_dict()
        return OnchainPrice(chain=chain_l, address=address, price_usd=None,
                            envelope=env)

    if chain_l in ("mock", "paper"):
        return _mock_price(chain_l, address, allow_mock_override, config)

    client: _HttpLike = http or _DefaultHttp()
    try:
        body = client.get_json(_DS_BASE.format(address=address), timeout=6.0)
    except Exception as exc:
        log.debug("onchain price fetch failed for %s/%s: %s",
                  chain_l, address, exc)
        if _mock_ok(allow_mock_override, config):
            return _mock_price(chain_l, address, True, config)
        env = degraded_envelope(
            "onchain_price",
            error=f"transport_error:{type(exc).__name__}",
            venue="dexscreener",
        ).as_dict()
        return OnchainPrice(chain=chain_l, address=address,
                            price_usd=None, envelope=env)

    pair = _pick_best_pair(body, chain_l)
    if pair is None:
        env = degraded_envelope(
            "onchain_price", error="no_pair_found", venue="dexscreener",
        ).as_dict()
        return OnchainPrice(chain=chain_l, address=address,
                            price_usd=None, envelope=env)

    try:
        price = float(pair.get("priceUsd", 0.0))
    except (TypeError, ValueError):
        price = 0.0
    liq = _liquidity(pair)
    env = live_envelope(source="dexscreener", venue=chain_l).as_dict()
    return OnchainPrice(
        chain=chain_l, address=address, price_usd=price,
        pair_address=pair.get("pairAddress"),
        liquidity_usd=liq, envelope=env,
    )


# ---------------------------------------------------------------- helpers
def _mock_ok(override: bool | None, config: Any | None) -> bool:
    if override is True:
        return True
    if override is False:
        return False
    return resolve_allow_mock(None, config)


def _mock_price(chain: str, address: str, override: bool | None,
                config: Any | None) -> OnchainPrice:
    if not _mock_ok(override, config):
        env = degraded_envelope(
            "onchain_price", error="mock_not_authorised", venue=chain,
        ).as_dict()
        return OnchainPrice(chain=chain, address=address,
                            price_usd=None, envelope=env)
    # Deterministic mock price derived from the address hash so that
    # tests are reproducible without hitting the internet.
    h = sum(ord(c) for c in address) or 1
    price = 0.01 + (h % 1000) / 100.0
    env = mock_envelope(source="mock", venue=chain).as_dict()
    return OnchainPrice(chain=chain, address=address,
                        price_usd=round(price, 4),
                        liquidity_usd=1_000_000.0,
                        venue="mock", envelope=env)


def _pick_best_pair(body: dict[str, Any], chain: str) -> dict | None:
    pairs = body.get("pairs") if isinstance(body, dict) else None
    if not pairs:
        return None
    # Filter by chain id when possible. DEX Screener uses "ethereum",
    # "bsc", "polygon", "arbitrum", "base", "optimism", "solana".
    same_chain = [p for p in pairs
                  if str(p.get("chainId", "")).lower() == chain]
    candidates = same_chain or pairs
    # Pick highest liquidity pair.
    candidates.sort(key=_liquidity, reverse=True)
    return candidates[0]


def _liquidity(pair: dict[str, Any]) -> float:
    liq = pair.get("liquidity")
    if isinstance(liq, dict):
        return float(liq.get("usd") or 0.0)
    return 0.0


class _DefaultHttp:
    """Thin wrapper over :class:`UrllibHttp` that normalises the return
    to a parsed JSON dict."""

    def __init__(self) -> None:
        self._http: HttpTransport = UrllibHttp()

    def get_json(self, url: str, *, timeout: float = 6.0) -> dict[str, Any]:
        status, doc = self._http.request("GET", url, timeout=timeout)
        if not 200 <= int(status) < 300:
            raise ValueError(f"onchain price http status={status}")
        if isinstance(doc, dict):
            return doc
        if isinstance(doc, (bytes, bytearray)):
            return json.loads(doc.decode("utf-8", errors="ignore"))
        if isinstance(doc, str):
            return json.loads(doc)
        raise ValueError(f"unexpected onchain price response type: {type(doc)}")


__all__ = ["OnchainPrice", "get_onchain_price"]
