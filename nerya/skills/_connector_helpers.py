"""Shared helpers used by builtin skill action modules.

Before this module existed, every skill that needed a connector
(``market_data``, ``exchange``, ``onchain``, ``trading``, …) repeated
the same boilerplate:

    def _venue(market): ...
    def _mock_exchange(ctx): ...
    def _connector_for_market(ctx, market): ...
    def _account_connector(ctx, account_id): ...

Pulling these out into one module removes ~40 duplicated lines across
skills and gives all action modules identical cache semantics and
fallback-to-mock behaviour. Everything keyed by ``ctx.extras`` so the
cache never leaks across agent runs.

Usage:

    from .._connector_helpers import venue_of, public_connector
    conn = public_connector(ctx, market)
"""

from __future__ import annotations

from typing import Any

from ..core.errors import SkillActionError
from ..core.truth import resolve_allow_mock


def venue_of(market: str) -> str:
    """Parse the venue prefix from ``BINANCE:BTCUSDT``.

    Returns the lower-cased prefix, or ``""`` when the market id does
    not carry one. We deliberately do **not** default to ``mock`` —
    silently treating an unprefixed market as mock is a truth-gate
    violation. Callers decide how to handle the empty case.
    """
    if not market:
        return ""
    if ":" in market:
        return market.split(":", 1)[0].lower()
    return ""


def _mock_mode_from_ctx(ctx) -> bool:
    cfg = getattr(ctx, "config", None)
    return resolve_allow_mock(None, cfg)


def mock_exchange(ctx) -> Any:
    """Return a per-context cached ``MockExchange``."""
    from ..connectors.mock_exchange import MockExchange
    cached = ctx.extras.get("_mock_exchange")
    if cached:
        return cached
    exch = MockExchange()
    ctx.extras["_mock_exchange"] = exch
    return exch


def mock_chain(ctx) -> Any:
    """Return a per-context cached ``MockChain``."""
    from ..connectors.mock_chain import MockChain
    cached = ctx.extras.get("_mock_chain")
    if cached:
        return cached
    c = MockChain()
    ctx.extras["_mock_chain"] = c
    return c


def public_connector(ctx, market: str, *, cache_key: str = "_md_connectors") -> Any:
    """Resolve a *public* (no-credential) connector for a market id.

    The ``mock``/``paper`` venues always resolve to the in-memory exchange.
    Other venues resolve to the real public connector. If resolution fails
    we fall back to the mock **only when mock mode is authorised** (env
    ``NERYA_ALLOW_MOCK_DATA`` or ``runtime.mock_mode``); otherwise we raise
    :class:`SkillActionError` so the caller sees an explicit unavailable.
    """
    from ..connectors.registry import build_connector
    venue = venue_of(market)
    cache = ctx.extras.setdefault(cache_key, {})
    if venue in cache:
        return cache[venue]
    if venue in ("mock", "paper", ""):
        if not _mock_mode_from_ctx(ctx):
            raise SkillActionError(
                f"public connector for market {market!r} unavailable: "
                f"venue={venue!r} requires explicit mock authorisation "
                f"(set NERYA_ALLOW_MOCK_DATA=1 or runtime.mock_mode)"
            )
        cache[venue] = mock_exchange(ctx)
        return cache[venue]
    try:
        conn = build_connector(
            {"venue": venue, "live": False},
            workspace=ctx.config.paths.root,
        )
    except Exception as exc:
        if _mock_mode_from_ctx(ctx):
            conn = mock_exchange(ctx)
        else:
            raise SkillActionError(
                f"public connector for venue {venue!r} unavailable: {exc}"
            ) from exc
    cache[venue] = conn
    return conn


def public_chain_connector(ctx, chain: str, *, cache_key: str = "_onchain_connectors") -> Any:
    """Resolve a *public* chain connector (EVM/Solana/BSC) by chain id.

    Mirrors :func:`public_connector` exactly on truth-gate semantics:

    * ``mock`` / ``paper`` / ``""`` resolve to :class:`MockChain` **only
      when mock mode is explicitly authorised** (``NERYA_ALLOW_MOCK_DATA``
      / ``NERYA_MOCK_MODE`` env var, or ``runtime.mock_mode=true`` /
      ``runtime.paper_trading_enabled=true`` combined with
      ``runtime.mock_when_paper=true``). Otherwise we raise
      :class:`SkillActionError` so the caller cannot silently consume
      simulated chain data as if it were live evidence.
    * Real chain ids resolve via ``build_connector``. Resolution errors
      become explicit :class:`SkillActionError` unless mock mode is
      authorised, in which case we fall back to :class:`MockChain`.
    """
    from ..connectors.registry import build_connector
    chain_l = (chain or "").lower()
    cache = ctx.extras.setdefault(cache_key, {})
    if chain_l in cache:
        return cache[chain_l]
    if chain_l in ("mock", "paper", ""):
        if not _mock_mode_from_ctx(ctx):
            raise SkillActionError(
                f"public chain connector for {chain!r} unavailable: "
                f"chain={chain_l!r} requires explicit mock authorisation "
                f"(set NERYA_ALLOW_MOCK_DATA=1 or runtime.mock_mode / "
                f"runtime.mock_when_paper)"
            )
        cache[chain_l] = mock_chain(ctx)
        return cache[chain_l]
    try:
        conn = build_connector(
            {"venue": chain_l, "live": False},
            workspace=ctx.config.paths.root,
        )
    except Exception as exc:
        if _mock_mode_from_ctx(ctx):
            conn = mock_chain(ctx)
        else:
            raise SkillActionError(
                f"public chain connector for {chain_l!r} unavailable: {exc}"
            ) from exc
    cache[chain_l] = conn
    return conn


def account_connector(ctx, account_id: str, *, cache_key: str = "_exchange_account") -> Any:
    """Resolve a *credentialed* connector for a configured account id.

    Raises :class:`SkillActionError` if ``account_id`` isn't in the
    workspace account registry. Credentials come from
    ``ctx.config.vault_passphrase`` via the normal
    :func:`nerya.connectors.registry.build_connector` flow.
    """
    from ..connectors.registry import build_connector
    from ..trading.accounts import load_accounts

    cache = ctx.extras.setdefault(cache_key, {})
    if account_id in cache:
        return cache[account_id]
    accts = load_accounts(ctx.config.paths)
    if account_id not in accts:
        raise SkillActionError(f"unknown account_id: {account_id!r}")
    acc = accts[account_id]
    cfg = acc.connector_cfg()
    vault_passphrase = getattr(ctx.config, "vault_passphrase", None)
    conn = build_connector(
        cfg, workspace=ctx.config.paths.root,
        vault_passphrase=vault_passphrase,
    )
    cache[account_id] = conn
    return conn


__all__ = [
    "venue_of",
    "mock_exchange",
    "mock_chain",
    "public_connector",
    "public_chain_connector",
    "account_connector",
]
