"""StrategyContext facade — the only object generated strategy code sees.

Why a facade
------------
Generated strategy ``main.py`` files are agent-authored. Even after
static analysis + contract tests + operator approval, they should
not have direct access to:

* raw :class:`~nerya.core.config.Config` (would expose vault paths,
  signer policy, LLM credentials);
* :class:`~nerya.skills.kernel.SkillKernel` (would let strategies
  call arbitrary skills, including disallowed ones);
* :class:`~nerya.connectors.base.Connector` (would let strategies
  bypass the trading kernel and place raw orders);
* :class:`~nerya.llm.gateway.LLMGateway` (would bypass tier policy
  and per-strategy budget caps).

Instead, the runner threads a single :class:`StrategyContext` into
``run(ctx)``. Every side effect — placing trades, reading market
data, calling LLMs, dispatching subagents, sending operator
messages, persisting state, journaling — goes through one of the
sub-facades attached to ``ctx``.

The facade also injects strategy attribution everywhere:

* ``strategy_id`` on every journal row, trade intent, message, and
  subagent invocation;
* ``source="strategy_runtime"`` on trade intents so risk/approval
  pipelines can distinguish strategy-driven orders from manual
  ones;
* a ``_caller="strategy:<id>"`` tag on every LLM call so per-strategy
  budgets can be observed in the telemetry journal.

Sub-facades:

* :class:`StrategyConfig`     — read-only view of manifest fields.
* :class:`StrategyPolicy`     — read-only typed policy + LLM policy.
* :class:`StrategyMarket`     — ticker / candles / orderbook / mark.
* :class:`StrategyNews`       — pluggable news fetchers (operator-
  configured) + dedupe helpers.
* :class:`StrategyDedupe`     — deterministic dedupe over a JSON-able
  ``id`` field, persisted in strategy state.
* :class:`StrategyLLMFacade`  — tier-aware ``classify`` /
  ``extract_json`` / ``analyze_signal`` / ``compress`` with strategy-
  policy enforcement (allowed tiers, per-run call cap).
* :class:`StrategySubAgents`  — ``run`` / ``run_many`` scoped to this
  strategy; rejects disallowed lanes; validates the optional schema.
* :class:`StrategyTrading`    — ``submit_intent`` (delegates to
  :func:`nerya.trading.submit.submit_trade_intent`).
* :class:`StrategyMessages`   — outbox/queue helper for operator-
  facing notifications.
* :class:`StrategyState`      — strategy-local key/value store with
  optimistic locking.
* :class:`StrategyClock`      — deterministic time source so tests /
  replay can pin ``now``.
* :class:`StrategyAudit`      — structured run-journal writer.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ..core import jsonl
from ..core.config import Config
from ..core.errors import NeryaError, TradingError
from ..core.paths import WorkspacePaths
from ..core.time import now_iso as _real_now_iso
from ..data.candles import fetch_candles, fetch_public_ticker, normalize_klines
from ..data.features import compute_features
from ..workspace.state_store import StateStore
from .package import StrategyManifest, StrategyPackage
from .backtest_bridge import backtest_replay as _strategy_backtest_replay
from .prompt_io import StrategyPromptIO
from .result import ResultBuilder, StrategyResult


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StrategyRuntimeError(NeryaError):
    """Raised by the facade when the strategy violates a runtime policy.

    Distinct from :class:`~nerya.core.errors.TradingError` (which the
    trading kernel raises on intent validation) and
    :class:`~nerya.core.errors.LLMError` (raised by the LLM gateway on
    tier/quota violations). The runner catches this and converts it
    into a :class:`StrategyResult` with ``status='error'``.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_NEWS_ID_SAFE = re.compile(r"[^A-Za-z0-9._\-]+")


def _safe_news_id(value: str) -> str:
    """Slug used as a dedupe key under strategy state."""

    s = _NEWS_ID_SAFE.sub("_", str(value or "")).strip("_")
    return s[:120] or uuid.uuid4().hex[:12]


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _market_key(value: str) -> str:
    tail = str(value or "").strip().lower().split(":", 1)[-1]
    return "".join(ch for ch in tail if ch.isalnum())


# ---------------------------------------------------------------------------
# Sub-facades — all read-only or scoped to one strategy
# ---------------------------------------------------------------------------


@dataclass
class StrategyConfig:
    """Read-only manifest projection exposed as ``ctx.config``.

    Generated code reads things like ``ctx.config.markets[0]`` or
    ``ctx.config.news_sources``. We deliberately surface the *typed*
    manifest — not the raw YAML — so strategy code never sees keys
    that aren't in the schema, even if a future operator hand-edits
    ``strategy.yml`` to add unrelated metadata.
    """

    strategy_id: str
    title: str
    mode: str  # paper | shadow | live
    markets: tuple[str, ...]
    accounts: tuple[str, ...]
    news_sources: tuple[str, ...]
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyPolicyView:
    """Read-only policy projection exposed as ``ctx.policy``.

    Mirrors :class:`~nerya.strategies.package.StrategyPolicy` plus
    the ``llm_policy`` block so generated code can branch on
    ``ctx.policy.min_confidence`` etc. without re-reading YAML.
    """

    max_single_order_usd: float
    max_daily_notional_usd: float
    max_open_positions: int
    min_confidence: float
    allow_direct_order: bool
    require_subagent_before_order: bool
    default_order_usd: float
    max_run_seconds: float

    # llm policy
    default_tier: str
    allowed_tiers: tuple[str, ...]
    max_calls_per_run: int

    # raw extras for forward compat
    raw_policy: dict[str, Any] = field(default_factory=dict)
    raw_llm_policy: dict[str, Any] = field(default_factory=dict)


# ---- market ----------------------------------------------------------------


@dataclass
class StrategyMarket:
    """Read-only market data facade.

    ``account`` selection: most strategies trade a single account
    (the first entry in ``manifest.accounts``). The facade caches
    one connector per ``(account_id, market)`` pair so the
    connector registry isn't re-built on every call.

    All methods raise :class:`StrategyRuntimeError` (not
    :class:`TradingError` or connector exceptions) so generated
    code only has to handle one error type.
    """

    paths: WorkspacePaths
    accounts: tuple[str, ...]
    _registry_factory: Callable[[], Any] = field(repr=False)
    _connector_cache: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def _connector_for(self, market: str, *, account: Optional[str] = None) -> Any:
        from ..trading.accounts import load_accounts as _load_accounts

        account_id = account or (self.accounts[0] if self.accounts else None)
        if not account_id:
            raise StrategyRuntimeError(
                "no account configured; set manifest.accounts before calling ctx.market"
            )
        cache_key = f"{account_id}::{market}"
        if cache_key in self._connector_cache:
            return self._connector_cache[cache_key]
        accounts = _load_accounts(self.paths)
        if account_id not in accounts:
            raise StrategyRuntimeError(f"unknown account_id: {account_id}")
        try:
            registry = self._registry_factory()
            conn = registry.get(account_id, accounts[account_id].connector_cfg())
        except Exception as exc:  # connector build / vault failure
            raise StrategyRuntimeError(
                f"connector unavailable for account={account_id} market={market}: {exc}"
            ) from exc
        self._connector_cache[cache_key] = conn
        return conn

    def ticker(self, market: str, *, account: Optional[str] = None) -> dict[str, Any]:
        """Latest ticker (bid/ask/mid) for ``market``."""

        venue = market.split(":", 1)[0].upper() if ":" in market else ""
        if venue and venue not in {"MOCK", "PAPER"}:
            try:
                snap = fetch_public_ticker(
                    market,
                    allow_mock=False,
                )
            except Exception as exc:
                raise StrategyRuntimeError(f"ticker failed: {exc}") from exc
            env = snap.get("_envelope") if isinstance(snap, dict) else None
            price = float(
                snap.get("last")
                or snap.get("mid")
                or snap.get("price")
                or 0.0
            )
            if not price or not isinstance(env, dict) or env.get("mode") != "live":
                err = str((env or {}).get("error") or "no_live_ticker")
                raise StrategyRuntimeError(f"ticker failed: {err}")
            bid = snap.get("bid")
            ask = snap.get("ask")
            mid = snap.get("mid") or price
            return {
                "market": market,
                "bid": float(bid) if bid is not None else None,
                "ask": float(ask) if ask is not None else None,
                "mid": float(mid),
                "last": price,
                "spread_bps": float(snap.get("spread_bps") or 0.0),
                "ts_ms": int(snap.get("ts_ms") or time.time() * 1000),
                "venue": env.get("venue") or venue.lower(),
                "source": snap.get("source") or env.get("source"),
                "_envelope": env,
            }

        conn = self._connector_for(market, account=account)
        try:
            t = conn.get_ticker(market)
        except Exception as exc:
            raise StrategyRuntimeError(f"ticker failed: {exc}") from exc
        return t.asdict() if hasattr(t, "asdict") else dict(t or {})

    def get_ticker(self, market: str, *, account: Optional[str] = None) -> dict[str, Any]:
        """Compatibility alias for generated strategies."""

        return self.ticker(market, account=account)

    def mark_price(self, market: str, *, account: Optional[str] = None) -> float:
        conn = self._connector_for(market, account=account)
        try:
            return float(conn.get_mark_price(market))
        except Exception as exc:
            raise StrategyRuntimeError(f"mark_price failed: {exc}") from exc

    def orderbook(self, market: str, *, depth: int = 20, account: Optional[str] = None) -> dict[str, Any]:
        """Top-of-book / aggregated book snapshot.

        ``depth`` is currently a hint — the base ``Connector.get_order_book``
        falls back to ticker-level top-of-book; native connectors override
        with full depth. We pass it through so future connectors can honour
        it without breaking strategy code.
        """

        conn = self._connector_for(market, account=account)
        try:
            book = conn.get_order_book(market)
        except Exception as exc:
            raise StrategyRuntimeError(f"orderbook failed: {exc}") from exc
        out = dict(book or {})
        out.setdefault("depth_hint", depth)
        return out

    def candles(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: Optional[str] = None,
        symbol: str | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        """OHLCV candles. Connectors that don't implement candles return ``[]``.

        We normalize the connector output (``[ts, open, high, low, close, volume]``
        rows) into dicts so strategy code can write
        ``c["close"]`` instead of indexing positionally.
        """

        market, timeframe, limit = self._normalise_candle_args(
            market,
            args,
            timeframe=interval or timeframe,
            limit=count or limit,
            symbol=symbol,
        )
        venue = market.split(":", 1)[0].upper() if ":" in market else ""
        if venue and venue not in {"MOCK", "PAPER"}:
            return fetch_candles(
                market,
                count=limit,
                interval=timeframe,
                allow_mock=False,
            )

        conn = self._connector_for(market, account=account)
        try:
            rows = conn.get_klines(market, interval=timeframe, limit=limit)
        except Exception as exc:
            raise StrategyRuntimeError(f"candles failed: {exc}") from exc
        venue_hint = getattr(conn, "venue", venue) or venue
        return normalize_klines(str(venue_hint), list(rows or ()))

    def _normalise_candle_args(
        self,
        market: str | None,
        args: tuple[Any, ...],
        *,
        timeframe: str,
        limit: int,
        symbol: str | None = None,
    ) -> tuple[str, str, int]:
        chosen_market_value = symbol or market
        if not chosen_market_value:
            raise StrategyRuntimeError(
                "candles requires an explicit market or symbol; use "
                "ctx.market.candles(ctx.config.markets[0], timeframe=..., limit=...)"
            )
        chosen_market = str(chosen_market_value)
        chosen_timeframe = str(timeframe or "1m")
        chosen_limit = int(limit or 100)
        if args:
            chosen_timeframe = str(args[0])
        if len(args) >= 2:
            try:
                chosen_limit = int(args[1])
            except Exception:
                chosen_limit = int(limit or 100)
        return chosen_market, chosen_timeframe, chosen_limit

    def get_candles(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: Optional[str] = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Compatibility alias matching common market-data wording.

        Accepts the same positional variants as :meth:`candles` (for example
        ``get_candles("BINANCE:BTCUSDT", "1h", 200)``) so generated strategy
        code behaves identically through either name.
        """

        return self.candles(
            market,
            *args,
            timeframe=interval or timeframe,
            limit=count or limit,
            account=account,
            symbol=symbol,
            **kwargs,
        )

    def ohlcv(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: Optional[str] = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Compatibility alias for generated strategies that ask for OHLCV."""

        return self.candles(
            market,
            *args,
            timeframe=interval or timeframe,
            limit=count or limit,
            account=account,
            symbol=symbol,
            **kwargs,
        )

    def get_ohlcv(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: Optional[str] = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Compatibility alias for SDKs/generated code that use get_ohlcv."""

        return self.ohlcv(
            market,
            *args,
            timeframe=interval or timeframe,
            limit=count or limit,
            account=account,
            symbol=symbol,
            **kwargs,
        )

    def klines(
        self,
        market: str,
        *,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        account: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Compatibility alias for generated strategies that ask for klines."""

        return self.candles(
            market,
            timeframe=interval or timeframe,
            limit=limit,
            account=account,
        )

    def features(
        self,
        market: str,
        *,
        timeframe: str = "1m",
        lookback: int = 100,
        account: Optional[str] = None,
    ) -> dict[str, Any]:
        """Convenience: latest OHLCV-derived feature + indicator payload."""

        rows = self.candles(market, timeframe=timeframe, limit=lookback, account=account)
        indicator_features = compute_features(rows)
        if not rows:
            return {
                "market": market,
                "timeframe": timeframe,
                "rows": 0,
                **indicator_features,
            }
        closes = [float(r.get("close") or 0.0) for r in rows]
        highs = [float(r.get("high") or 0.0) for r in rows]
        lows = [float(r.get("low") or 0.0) for r in rows]
        volumes = [float(r.get("volume") or 0.0) for r in rows]
        return {
            "market": market,
            "timeframe": timeframe,
            "rows": len(rows),
            "first": rows[0],
            "last": rows[-1],
            "close_min": min(closes) if closes else 0.0,
            "close_max": max(closes) if closes else 0.0,
            "high_max": max(highs) if highs else 0.0,
            "low_min": min(lows) if lows else 0.0,
            "volume_sum": sum(volumes),
            **indicator_features,
        }


# ---- news ------------------------------------------------------------------


NewsFetcher = Callable[..., list[dict[str, Any]]]
"""Operator-supplied callable that returns news rows.

Signature: ``fetcher(*, since: Optional[str], limit: int) -> list[dict]``.

Each row should expose ``id`` (stable), ``ts``, ``source``, ``title``,
``summary``. The runner pre-resolves these fetchers from
``manifest.news_sources`` and injects them into the facade.
"""


@dataclass
class StrategyNews:
    """Pluggable news / social feed facade.

    The runner pre-resolves a fetcher per ``manifest.news_sources``
    entry; strategy code calls ``ctx.news.fetch(...)`` to pull a
    deduped list across all sources. When the workspace hasn't
    configured a source for the strategy, ``fetch`` returns an
    empty list (not an error) so generated code can still run.
    """

    sources: tuple[str, ...]
    _fetchers: dict[str, NewsFetcher] = field(default_factory=dict, repr=False)

    def register(self, source_id: str, fetcher: NewsFetcher) -> None:
        """Register / override a fetcher (used by tests & runner)."""

        self._fetchers[str(source_id)] = fetcher

    def fetch(
        self,
        *,
        sources: Optional[Iterable[str]] = None,
        since: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Pull news rows from each configured fetcher.

        Returns an aggregated, source-tagged list. Downstream code is
        expected to dedupe via :class:`StrategyDedupe`.
        """

        out: list[dict[str, Any]] = []
        chosen = list(sources) if sources is not None else list(self.sources)
        for src in chosen:
            fetcher = self._fetchers.get(str(src))
            if fetcher is None:
                continue
            try:
                rows = fetcher(since=since, limit=limit) or []
            except Exception as exc:
                _LOG.warning("news fetcher %s failed: %s", src, exc)
                continue
            for r in rows:
                if not isinstance(r, dict):
                    continue
                row = dict(r)
                row.setdefault("source", src)
                out.append(row)
        return out


@dataclass
class StrategyDedupe:
    """Strategy-local dedupe scratchpad.

    Stores seen ``id`` values in ``state['__dedupe__'][bucket]`` so
    a news strategy can remember headlines across ticks without
    every author rolling their own hash set.
    """

    state: "StrategyState"

    def news(
        self,
        items: Iterable[dict[str, Any]],
        *,
        bucket: str = "news",
        max_keys: int = 5000,
    ) -> list[dict[str, Any]]:
        """Drop items whose ``id`` was already seen in ``bucket``."""

        seen: dict[str, str] = self.state.get("__dedupe__", {}).get(bucket, {}) or {}
        if not isinstance(seen, dict):
            seen = {}
        fresh: list[dict[str, Any]] = []
        added = 0
        for item in items or ():
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id") or item.get("guid") or item.get("link")
            key = _safe_news_id(str(raw_id or "")) if raw_id else uuid.uuid4().hex[:16]
            if key in seen:
                continue
            seen[key] = _real_now_iso()
            fresh.append(item)
            added += 1
        if added:
            # Cap the size so we don't grow the state file forever.
            if len(seen) > max_keys:
                # Drop oldest by insertion order (CPython dicts preserve it).
                excess = len(seen) - max_keys
                for k in list(seen.keys())[:excess]:
                    seen.pop(k, None)
            buckets = self.state.get("__dedupe__", {}) or {}
            if not isinstance(buckets, dict):
                buckets = {}
            buckets[bucket] = seen
            self.state.set("__dedupe__", buckets)
        return fresh


# ---- llm -------------------------------------------------------------------


@dataclass
class StrategyLLMFacade:
    """Tier-aware LLM facade.

    Enforces the strategy's ``llm_policy``:

    * ``allowed_tiers`` — calls requesting an out-of-policy tier are
      rejected with :class:`StrategyRuntimeError`.
    * ``max_calls_per_run`` — counted across all four methods; once
      exceeded the next call raises.
    * ``default_tier``    — applied when the caller didn't pass one.

    Each call is tagged ``_caller = "strategy:<id>"`` so the LLM
    journal attributes spend per strategy.
    """

    config: Config
    strategy_id: str
    default_tier: str
    allowed_tiers: tuple[str, ...]
    max_calls_per_run: int
    _calls_made: int = 0
    _gateway: Any = field(default=None, init=False, repr=False)

    def _gw(self) -> Any:
        if self._gateway is None:
            from ..llm.gateway import LLMGateway

            self._gateway = LLMGateway(self.config)
        return self._gateway

    def _resolve_tier(self, tier: Optional[str]) -> str:
        chosen = (tier or self.default_tier or "light").strip().lower()
        if self.allowed_tiers and chosen not in self.allowed_tiers:
            raise StrategyRuntimeError(
                f"strategy {self.strategy_id!r}: tier {chosen!r} not in "
                f"allowed_tiers={list(self.allowed_tiers)}"
            )
        return chosen

    def _budget_check(self) -> None:
        if self.max_calls_per_run > 0 and self._calls_made >= self.max_calls_per_run:
            raise StrategyRuntimeError(
                f"strategy {self.strategy_id!r}: LLM call budget exhausted "
                f"(max_calls_per_run={self.max_calls_per_run})"
            )

    def _caller_tag(self) -> str:
        return f"strategy:{self.strategy_id}"

    def classify(
        self,
        *,
        prompt: str,
        labels: list[str],
        tier: Optional[str] = None,
    ) -> dict[str, Any]:
        """Pick one label from ``labels``."""

        self._budget_check()
        chosen = self._resolve_tier(tier)
        self._calls_made += 1
        result = self._gw().call(
            task="classify",
            prompt=(
                f"Pick exactly one label from {labels!r} that best matches "
                f"the input. Reply with strict JSON: "
                f'{{"label": <one of {labels!r}>, "confidence": 0..1, '
                f'"reason": <short>}}\n\nInput:\n{prompt}'
            ),
            caller=self._caller_tag(),
            tier=chosen,
            schema={
                "type": "object",
                "properties": {
                    "label": {"type": "string", "enum": list(labels)},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["label"],
            },
        )
        parsed = result.parsed if isinstance(result.parsed, dict) else {}
        return {
            "label": parsed.get("label"),
            "confidence": float(parsed.get("confidence", 0.0) or 0.0),
            "reason": str(parsed.get("reason") or ""),
            "tokens": result.tokens,
            "usd": result.usd,
            "tier": result.tier,
        }

    def extract_json(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        tier: Optional[str] = None,
    ) -> dict[str, Any]:
        self._budget_check()
        chosen = self._resolve_tier(tier)
        self._calls_made += 1
        result = self._gw().call(
            task="extract_json",
            prompt=prompt,
            caller=self._caller_tag(),
            tier=chosen,
            schema=schema,
        )
        return {
            "data": result.parsed,
            "tokens": result.tokens,
            "usd": result.usd,
            "tier": result.tier,
        }

    def analyze_signal(
        self,
        *,
        context: str,
        schema: Optional[dict[str, Any]] = None,
        tier: Optional[str] = None,
    ) -> dict[str, Any]:
        self._budget_check()
        chosen = self._resolve_tier(tier)
        self._calls_made += 1
        result = self._gw().call(
            task="analyze_signal",
            prompt=context,
            caller=self._caller_tag(),
            tier=chosen,
            schema=schema,
        )
        return {
            "data": result.parsed,
            "raw": result.raw,
            "tokens": result.tokens,
            "usd": result.usd,
            "tier": result.tier,
        }

    def compress(
        self,
        *,
        text: str,
        max_tokens: int = 256,
        tier: Optional[str] = None,
    ) -> dict[str, Any]:
        self._budget_check()
        # compress always runs on the cheapest allowed tier unless the
        # caller insists otherwise; this matches the existing llm_skill
        # contract and keeps multi-source news pipelines affordable.
        chosen = self._resolve_tier(tier or "light")
        self._calls_made += 1
        result = self._gw().call(
            task="compress",
            prompt=(
                f"Compress the following text into <= {max_tokens} tokens. "
                f"Preserve names, numbers, dates. No editorial.\n\n{text}"
            ),
            caller=self._caller_tag(),
            tier=chosen,
        )
        return {
            "text": result.raw,
            "tokens": result.tokens,
            "usd": result.usd,
            "tier": result.tier,
        }

    @property
    def calls_made(self) -> int:
        return self._calls_made


# ---- subagents -------------------------------------------------------------


@dataclass
class StrategySubAgents:
    """Strategy-scoped subagent dispatcher.

    Forwards to :class:`~nerya.subagents.dispatcher.SubAgentDispatcher`
    but injects ``strategy_id`` and ``session_id`` so attribution
    journals correctly. Optional ``schema`` validation runs on the
    output dict so generated code can fail fast when a subagent
    drifts from its declared output shape.
    """

    config: Config
    skills: Any  # SkillKernel — type-hinted as Any to avoid import cycle
    strategy_id: str
    session_id: Optional[str] = None
    tool_registry: Any = None
    executor: Any = None
    runtime_mode: str = "auto"
    _dispatcher: Any = field(default=None, init=False, repr=False)

    def _disp(self) -> Any:
        if self._dispatcher is None:
            from ..subagents.dispatcher import SubAgentDispatcher

            kwargs: dict[str, Any] = {
                "config": self.config,
                "skills": self.skills,
            }
            if self.tool_registry is not None:
                kwargs["tool_registry"] = self.tool_registry
            if self.executor is not None:
                kwargs["executor"] = self.executor
            mode = self.runtime_mode
            if mode == "auto" and (
                self.tool_registry is None or self.executor is None
            ):
                # SDK/scheduler callers have no turn-owned policy chokepoint;
                # preserve their pre-native legacy behavior instead of
                # fabricating one or advertising an unusable native surface.
                mode = "legacy"
            if mode != "auto":
                kwargs["runtime_mode"] = mode
            self._dispatcher = SubAgentDispatcher(**kwargs)
        return self._dispatcher

    def run(
        self,
        name: str,
        *,
        payload: Optional[dict[str, Any]] = None,
        schema: Optional[dict[str, Any]] = None,
        trigger_event_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run one subagent and return its envelope.

        The envelope shape mirrors :class:`SubAgentResult` —
        ``ok / output / tier / tokens / usd / wall_ms / error / error_kind``.
        When ``schema`` is provided we run a *minimal* JSON schema
        check on ``output``; failure raises :class:`StrategyRuntimeError`
        so the caller decides whether to hold or retry.
        """

        target = name if name.startswith("subagent:") else f"subagent:{name}"
        envelope = self._disp().dispatch(
            target,
            payload=dict(payload or {}),
            trigger_event_id=trigger_event_id,
            strategy_id=self.strategy_id,
            session_id=self.session_id,
        )
        if schema is not None:
            output = envelope.get("output") if isinstance(envelope, dict) else None
            if isinstance(output, dict):
                _validate_minimal_schema(
                    output,
                    schema,
                    where=f"subagent:{name} output",
                )
            else:
                raise StrategyRuntimeError(
                    f"subagent {name!r} returned non-dict output; "
                    f"cannot validate against schema"
                )
        return dict(envelope or {})

    def run_many(
        self,
        names: Iterable[str],
        *,
        payload: Optional[dict[str, Any]] = None,
        schema: Optional[dict[str, Any]] = None,
        trigger_event_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Run several subagents sequentially. Failures don't abort siblings.

        We don't expose dispatcher concurrency here yet — the strategy
        runtime owns its own per-tick budget and parallelism is its
        choice; for now sequential is the safest default.
        """

        out: list[dict[str, Any]] = []
        for n in names or ():
            try:
                out.append(
                    self.run(
                        n,
                        payload=payload,
                        schema=schema,
                        trigger_event_id=trigger_event_id,
                    )
                )
            except StrategyRuntimeError as exc:
                out.append(
                    {
                        "ok": False,
                        "subagent": n,
                        "error": str(exc),
                        "error_kind": "schema",
                    }
                )
        return out


# ---- trading ---------------------------------------------------------------


@dataclass
class StrategyTrading:
    """Trade-intent submission scoped to one strategy.

    Always routes through :func:`nerya.trading.submit.submit_trade_intent`
    — the central pipeline that runs Risk Gate → Approval Gate →
    Execution Engine. The facade enforces *strategy-local* policy on
    top of that:

    * order size cap (``policy.max_single_order_usd``);
    * direct-order allowance (``policy.allow_direct_order``);
    * confidence floor (``policy.min_confidence``).

    Subagents are optional analysis helpers. A strategy can choose to
    call them before submitting an intent, but the runtime does not
    require subagent confirmation for any strategy class or mode.

    All intents inherit ``strategy_id`` and ``source="strategy_runtime"``
    automatically.
    """

    config: Config
    strategy_id: str
    policy: StrategyPolicyView
    accounts: tuple[str, ...]
    session_id: Optional[str] = None

    def _resolve_account(self, account: Optional[str]) -> str:
        if account:
            return str(account)
        if not self.accounts:
            raise StrategyRuntimeError(
                f"strategy {self.strategy_id!r}: no accounts configured; "
                f"set manifest.accounts before submitting intents"
            )
        return self.accounts[0]

    def submit_intent(
        self,
        *,
        market: str,
        side: str,
        size: float,
        size_unit: str = "usd",
        order_type: str = "market",
        limit_price: Optional[float] = None,
        confidence: Optional[float] = None,
        reasoning: Optional[str] = None,
        account: Optional[str] = None,
        market_snapshot: Optional[dict[str, Any]] = None,
        trigger_event_id: Optional[str] = None,
        plan_action: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Submit a trade intent and return the canonical envelope.

        Caller can pass ``market_snapshot`` (live ticker) so the risk
        engine sees the same prices the strategy reasoned over;
        omitting it falls back to the default resolver in
        :func:`submit_trade_intent`.

        ``plan_action`` (e.g. ``"close"``, ``"reduce_position"``,
        ``"open_long"``) is forwarded to the risk gate via
        :attr:`TradeIntent.meta`. The risk gate uses it to (a)
        relax snapshot-freshness gates for risk-reducing intents and
        (b) tell the reconciliation layer apart "operator opens fresh
        position" from "operator flattens an existing one".

        ``metadata`` and any other ``**extra`` kwargs are merged into
        ``meta`` as well so the strategy can stamp evidence trails
        (``signal_id``, ``feature_snapshot_ref``, …) that show up on
        the dashboard. Unknown kwargs accepted here are forwarded as
        meta entries — never silently dropped — so auto-generated
        templates that emit advisory keys keep working.
        """

        if not self.policy.allow_direct_order:
            raise StrategyRuntimeError(
                f"strategy {self.strategy_id!r}: allow_direct_order=False; "
                f"submit_intent calls are disabled by policy"
            )
        account_id = self._resolve_account(account)
        notional_usd = float(size) if size_unit == "usd" else 0.0
        if (
            self.policy.max_single_order_usd > 0
            and notional_usd > self.policy.max_single_order_usd
        ):
            raise StrategyRuntimeError(
                f"strategy {self.strategy_id!r}: order size "
                f"{notional_usd!r} exceeds max_single_order_usd="
                f"{self.policy.max_single_order_usd}"
            )
        if (
            confidence is not None
            and self.policy.min_confidence > 0
            and float(confidence) < self.policy.min_confidence
        ):
            raise StrategyRuntimeError(
                f"strategy {self.strategy_id!r}: confidence {confidence!r} "
                f"< policy.min_confidence={self.policy.min_confidence}"
            )

        # Merge plan_action + metadata + extra into a single ``meta``
        # blob that downstream consumers (risk gate, reconciliation,
        # dashboard) can read off the persisted intent row.
        meta_payload: dict[str, Any] = {}
        if isinstance(metadata, dict):
            meta_payload.update(metadata)
        for key, value in (extra or {}).items():
            # Skip private double-underscore kwargs the runner pre-pops.
            if key.startswith("_"):
                continue
            meta_payload[key] = value
        if plan_action is not None:
            meta_payload["plan_action"] = str(plan_action)

        spec: dict[str, Any] = {
            "account_id": account_id,
            "market": market,
            "side": side,
            "size": float(size),
            "size_unit": size_unit,
            "order_type": order_type,
            "strategy_id": self.strategy_id,
            "source": "strategy_runtime",
        }
        if limit_price is not None:
            spec["limit_price"] = float(limit_price)
        if confidence is not None:
            spec["confidence"] = float(confidence)
        if reasoning is not None:
            spec["reasoning"] = str(reasoning)
        if trigger_event_id is not None:
            spec["trigger_event_id"] = str(trigger_event_id)
        if meta_payload:
            spec["meta"] = meta_payload

        from ..trading.submit import submit_trade_intent as _submit

        try:
            envelope = _submit(
                self.config,
                spec=spec,
                market_snapshot=market_snapshot,
                default_strategy=self.strategy_id,
                default_source="strategy_runtime",
            )
        except TradingError as exc:
            raise StrategyRuntimeError(f"trading kernel rejected intent: {exc}") from exc
        # Stamp our session_id on the envelope when the kernel returned
        # something different (rare — usually it opens its own session).
        if self.session_id and not envelope.get("session_id"):
            envelope["session_id"] = self.session_id
        return envelope

    # ------------------------------------------------------------------
    # typed control-plane helpers. These mirror the
    # methods on :class:`nerya.sdk.trading_api.TradingAPI` and route
    # through the same :func:`submit_trade_plan` pipeline so strategy
    # code, agent code, and CLI code all share one risk gate path.
    # ------------------------------------------------------------------

    def open_position(
        self,
        *,
        market: str,
        side: str,
        sizing: Any,
        entry: Any = None,
        protection: Any = None,
        confidence: float = 0.0,
        reasoning_ref: str = "",
        trigger_event_id: Optional[str] = None,
        account: Optional[str] = None,
        market_snapshot: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.policy.allow_direct_order:
            raise StrategyRuntimeError(
                f"strategy {self.strategy_id!r}: allow_direct_order=False; "
                f"open_position calls are disabled by policy"
            )
        from ..sdk.trading_api import TradingAPI
        # Lazy SkillKernel import to avoid a circular import.
        from ..skills.kernel import SkillKernel

        api = TradingAPI(config=self.config, skills=SkillKernel.boot(self.config))
        return api.open_position(
            strategy_id=self.strategy_id,
            account_id=self._resolve_account(account),
            market=market,
            side=side,  # type: ignore[arg-type]
            sizing=sizing,
            entry=entry,
            protection=protection,
            confidence=confidence,
            reasoning_ref=reasoning_ref,
            trigger_event_id=trigger_event_id,
            source="strategy_runtime",
            market_snapshot=market_snapshot,
        )

    def close_position(
        self,
        *,
        market: str,
        side: str,
        entry: Any = None,
        confidence: float = 0.0,
        reasoning_ref: str = "",
        account: Optional[str] = None,
        market_snapshot: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.policy.allow_direct_order:
            raise StrategyRuntimeError(
                f"strategy {self.strategy_id!r}: allow_direct_order=False; "
                f"close_position calls are disabled by policy"
            )
        from ..sdk.trading_api import TradingAPI
        from ..skills.kernel import SkillKernel

        api = TradingAPI(config=self.config, skills=SkillKernel.boot(self.config))
        return api.close_position(
            strategy_id=self.strategy_id,
            account_id=self._resolve_account(account),
            market=market,
            side=side,  # type: ignore[arg-type]
            entry=entry,
            confidence=confidence,
            reasoning_ref=reasoning_ref,
            source="strategy_runtime",
            market_snapshot=market_snapshot,
        )

    def reduce_position(
        self,
        *,
        market: str,
        side: str,
        reduce_pct: Optional[float] = None,
        fixed_base: Optional[float] = None,
        entry: Any = None,
        confidence: float = 0.0,
        reasoning_ref: str = "",
        account: Optional[str] = None,
        market_snapshot: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.policy.allow_direct_order:
            raise StrategyRuntimeError(
                f"strategy {self.strategy_id!r}: allow_direct_order=False; "
                f"reduce_position calls are disabled by policy"
            )
        from ..sdk.trading_api import TradingAPI
        from ..skills.kernel import SkillKernel

        api = TradingAPI(config=self.config, skills=SkillKernel.boot(self.config))
        return api.reduce_position(
            strategy_id=self.strategy_id,
            account_id=self._resolve_account(account),
            market=market,
            side=side,  # type: ignore[arg-type]
            reduce_pct=reduce_pct,
            fixed_base=fixed_base,
            entry=entry,
            confidence=confidence,
            reasoning_ref=reasoning_ref,
            source="strategy_runtime",
            market_snapshot=market_snapshot,
        )

    def attach_protection(
        self,
        *,
        position_id: str,
        market: str,
        side: str,
        stop_loss: Any = None,
        take_profit: Any = None,
        trailing_stop: Any = None,
        partial_exits: Any = None,
        time_limit_sec: Optional[int] = None,
        mode: str = "soft",
        account: Optional[str] = None,
    ) -> dict[str, Any]:
        from ..sdk.trading_api import TradingAPI
        from ..skills.kernel import SkillKernel

        api = TradingAPI(config=self.config, skills=SkillKernel.boot(self.config))
        return api.attach_protection(
            strategy_id=self.strategy_id,
            account_id=self._resolve_account(account),
            position_id=position_id,
            market=market,
            side=side,  # type: ignore[arg-type]
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop=trailing_stop,
            partial_exits=partial_exits,
            time_limit_sec=time_limit_sec,
            mode=mode,  # type: ignore[arg-type]
        )

    def cancel_executor(self, *, executor_id: str) -> dict[str, Any]:
        from ..sdk.trading_api import TradingAPI
        from ..skills.kernel import SkillKernel

        api = TradingAPI(config=self.config, skills=SkillKernel.boot(self.config))
        return api.cancel_executor(executor_id=executor_id)

    def portfolio_snapshot(self, *, account: Optional[str] = None) -> dict[str, Any]:
        from ..sdk.trading_api import TradingAPI
        from ..skills.kernel import SkillKernel

        api = TradingAPI(config=self.config, skills=SkillKernel.boot(self.config))
        return api.portfolio_snapshot(account_id=account)

    def signal(
        self,
        *,
        market: str,
        signal_kind: str,
        confidence: float,
        reasoning_ref: str = "",
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        from ..sdk.trading_api import TradingAPI
        from ..skills.kernel import SkillKernel

        api = TradingAPI(config=self.config, skills=SkillKernel.boot(self.config))
        return api.signal(
            strategy_id=self.strategy_id,
            market=market,
            signal_kind=signal_kind,
            confidence=confidence,
            reasoning_ref=reasoning_ref,
            payload=payload,
        )

    def risk_preview(
        self,
        *,
        plan: Any = None,
        intent: Optional[dict[str, Any]] = None,
        market_snapshot: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        from ..sdk.trading_api import TradingAPI
        from ..skills.kernel import SkillKernel

        api = TradingAPI(config=self.config, skills=SkillKernel.boot(self.config))
        return api.risk_preview(plan=plan, intent=intent, market_snapshot=market_snapshot)


# ---- messages --------------------------------------------------------------


@dataclass
class StrategyMessages:
    """Operator notification helper.

    Routes through the same outbox the legacy notify skill writes to,
    so the dashboard's existing message panel renders strategy
    notifications alongside everything else.
    """

    paths: WorkspacePaths
    strategy_id: str
    session_id: Optional[str] = None

    def send(
        self,
        *,
        text: str,
        channel: str = "default",
        level: str = "info",
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        from ..skills.builtin.notify.scripts._outbox import queue_message

        return queue_message(
            channel=channel,
            text=str(text),
            severity=level,
            extra=dict(extra or {}),
            workspace=str(self.paths.root),
            strategy_id=self.strategy_id,
            session_id=self.session_id,
        )


# ---- state -----------------------------------------------------------------


@dataclass
class StrategyState:
    """Strategy-local key/value store with optimistic locking.

    Persisted at ``<strategy_root>/state/state.json``. Reads return a
    snapshot; writes serialise via the underlying :class:`StateStore`'s
    file lock. Strategy code should treat the store as eventually-
    consistent (a sibling tick can mutate values between calls) and
    use ``compare_and_set`` when ordering matters.
    """

    store: StateStore

    def get(self, key: str, default: Any = None) -> Any:
        return self.store.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.store.set(key, value)

    def update(self, **kwargs: Any) -> None:
        self.store.update(**kwargs)

    def compare_and_set(
        self,
        key: str,
        *,
        expect: Any,
        new_value: Any,
    ) -> bool:
        """Set ``key`` to ``new_value`` only if its current value equals ``expect``."""

        with self.store._lock:  # underlying RLock — module-internal access
            data = self.store._read()
            if data.get(key) != expect:
                return False
            data[key] = new_value
            self.store._write(data)
            return True

    def delete(self, key: str) -> None:
        with self.store._lock:
            data = self.store._read()
            if key in data:
                data.pop(key, None)
                self.store._write(data)


# ---- clock -----------------------------------------------------------------


@dataclass
class StrategyClock:
    """Deterministic time source.

    Defaults to wall-clock; tests + replay can inject a fixed
    ``now_iso`` provider to make time-dependent strategies
    reproducible.
    """

    _now_iso_fn: Callable[[], str] = field(default=_real_now_iso)
    _now_ms_fn: Callable[[], int] = field(default=lambda: int(time.time() * 1000))

    def now_iso(self) -> str:
        return self._now_iso_fn()

    def now_ms(self) -> int:
        return int(self._now_ms_fn())

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def freeze(self, *, iso: str, ms: Optional[int] = None) -> None:
        """Pin the clock for tests / replay sessions.

        Idempotent — call again to update. After a freeze,
        ``now_iso()`` always returns ``iso`` and ``now_ms()`` returns
        ``ms`` (or the parsed equivalent).
        """

        frozen_iso = iso
        if ms is None:
            try:
                ms = int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                ms = int(time.time() * 1000)
        frozen_ms = int(ms)
        self._now_iso_fn = lambda: frozen_iso
        self._now_ms_fn = lambda: frozen_ms


# ---- portfolio / pnl --------------------------------------------------------


@dataclass(frozen=True)
class StrategyPosition:
    """Attribute-friendly read model for generated strategy code."""

    account_id: str = ""
    market: str = ""
    size: float = 0.0
    avg_price: float = 0.0
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    market_value_usd: float = 0.0

    @property
    def quantity(self) -> float:
        return self.size

    @property
    def qty(self) -> float:
        return self.size

    @property
    def notional_usd(self) -> float:
        return self.market_value_usd

    @property
    def value_usd(self) -> float:
        return self.market_value_usd

    def asdict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "market": self.market,
            "size": self.size,
            "quantity": self.quantity,
            "qty": self.qty,
            "avg_price": self.avg_price,
            "realized_pnl_usd": self.realized_pnl_usd,
            "unrealized_pnl_usd": self.unrealized_pnl_usd,
            "market_value_usd": self.market_value_usd,
            "notional_usd": self.notional_usd,
            "value_usd": self.value_usd,
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self.asdict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.asdict()[key]


def _position_from_row(row: dict[str, Any]) -> StrategyPosition:
    size = _coerce_float(row.get("size") or row.get("quantity") or row.get("qty"))
    avg_price = _coerce_float(
        row.get("avg_price") or row.get("avg_entry_price") or row.get("entry_price")
    )
    market_value = _coerce_float(
        row.get("market_value_usd")
        or row.get("notional_usd")
        or row.get("value_usd"),
        default=abs(size) * avg_price,
    )
    return StrategyPosition(
        account_id=str(row.get("account_id") or ""),
        market=str(row.get("market") or ""),
        size=size,
        avg_price=avg_price,
        realized_pnl_usd=_coerce_float(row.get("realized_pnl_usd")),
        unrealized_pnl_usd=_coerce_float(row.get("unrealized_pnl_usd")),
        market_value_usd=market_value,
    )


@dataclass
class StrategyPortfolio:
    """Read-only portfolio facade backed by the workspace ledger.

    .. note::

       Post-v6 the ``positions``/``position`` accessors return the
       **strategy's own slice** of any merged ``(account, market)``
       position, not the broker-truth merged total. A strategy
       running alongside others on ``binance:BTCUSDT`` therefore sees
       only the BTC it itself opened — preserving the pre-v6 contract
       relied on by auto-generated scalping templates that compute
       ``qty = abs(position.size)`` before issuing a flatten.

       To inspect the merged broker-truth view of a market, callers
       should hit ``ctx.account.positions()`` or the
       ``/portfolio/summary`` HTTP endpoint instead.
    """

    paths: WorkspacePaths
    strategy_id: str = ""

    def summary(self) -> dict[str, Any]:
        from ..trading.portfolio import get_portfolio_summary

        return dict(get_portfolio_summary(self.paths) or {})

    @property
    def equity_usd(self) -> float:
        totals = (self.summary().get("totals") or {})
        return _coerce_float(totals.get("equity_usd"))

    @property
    def cash_usd(self) -> float:
        totals = (self.summary().get("totals") or {})
        return _coerce_float(totals.get("cash_usd"))

    def ledger(self, account_id: Optional[str] = None) -> dict[str, Any]:
        """Return a ``{equity, nav, cash}`` snapshot of the portfolio.

        Compatibility accessor for strategies that read NAV via a ledger
        handle. The backtest ``MockPortfolio`` exposes the same shape, so
        a strategy reads NAV identically in backtest and live.
        """
        totals = (self.summary().get("totals") or {})
        equity = _coerce_float(totals.get("equity_usd"))
        cash = _coerce_float(totals.get("cash_usd"))
        return {"equity": equity, "nav": equity, "cash": cash, "account_id": account_id or ""}

    def positions(self, market: Optional[str] = None) -> list[StrategyPosition]:
        """Return this strategy's per-share positions.

        Before v6 each strategy owned its own ``positions`` row, so
        the legacy fallback path (``get_positions(paths)``) was safe.
        After v6 those rows are merged with a ``__merged__`` sentinel
        strategy_id and the per-strategy sizing lives in
        ``position_shares``. Reading the merged row from a strategy's
        own ``ctx.portfolio.positions()`` would make every scalper
        believe it owned the sum of all sibling strategies' exposure
        — which caused runaway "close" loops in production.

        We now scan ``position_shares`` filtered by this strategy_id
        and return one ``StrategyPosition`` per open share. If the
        portfolio facade was constructed without a strategy_id (e.g.
        ad-hoc admin tooling) we fall back to the legacy merged-row
        view so existing callers keep working.
        """

        wanted = _market_key(market or "")
        if not self.strategy_id:
            # Admin / sdk callers without strategy_id keep the legacy
            # merged-row view. Strategies always have one set by
            # ``build_strategy_context`` below.
            from ..trading.portfolio import get_positions

            out: list[StrategyPosition] = []
            for row in get_positions(self.paths) or []:
                if not isinstance(row, dict):
                    continue
                pos = _position_from_row(row)
                if wanted and _market_key(pos.market) != wanted:
                    continue
                out.append(pos)
            return out

        try:
            from ..trading.position_book import PositionBook
        except Exception:
            return []

        try:
            book = PositionBook(self.paths)
            shares = book.list_shares_history(
                strategy_id=self.strategy_id,
                open_only=True,
                limit=10_000,
            )
        except Exception:
            return []

        out: list[StrategyPosition] = []
        for share in shares or []:
            if wanted and _market_key(share.market) != wanted:
                continue
            # Pull the merged mark so unrealized PnL on the strategy's
            # share is at least proxied off the broker-truth mark. We
            # only need this for ``mark_price``/``market_value_usd`` —
            # the absolute size is the share's own ``size_share_base``.
            merged = book.get_open_merged(
                account_id=share.account_id, market=share.market
            )
            mark = float((merged.mark_price if merged else 0.0) or share.avg_entry_share_price or 0.0)
            size = float(share.size_share_base or 0.0)
            avg = float(share.avg_entry_share_price or 0.0)
            side_factor = 1.0 if size >= 0 else -1.0
            unrealized = (
                (mark - avg) * abs(size) * side_factor if avg and mark else 0.0
            )
            market_value = abs(size) * mark
            out.append(
                StrategyPosition(
                    account_id=str(share.account_id or ""),
                    market=str(share.market or ""),
                    size=size,
                    avg_price=avg,
                    realized_pnl_usd=float(share.realized_pnl_share_usd or 0.0),
                    unrealized_pnl_usd=float(unrealized),
                    market_value_usd=market_value,
                )
            )
        return out

    def position(self, market: str) -> Optional[StrategyPosition]:
        rows = self.positions(market)
        return rows[0] if rows else None


@dataclass
class StrategyPnL:
    """Read-only PnL facade for old generated strategy templates."""

    paths: WorkspacePaths
    strategy_id: str

    def summary(self) -> dict[str, Any]:
        from ..trading.portfolio import get_pnl

        out = dict(get_pnl(self.paths) or {})
        equity = _coerce_float(out.get("equity_usd"))
        drawdown_usd = 0.0
        try:
            from .performance import _summarise_trades, read_strategy_ledger

            trade = _summarise_trades(
                read_strategy_ledger(self.paths, self.strategy_id, "intents"),
                read_strategy_ledger(self.paths, self.strategy_id, "orders"),
                read_strategy_ledger(self.paths, self.strategy_id, "fills"),
                read_strategy_ledger(self.paths, self.strategy_id, "pnl"),
            )
            drawdown_usd = _coerce_float(trade.get("max_drawdown_usd"))
            out.update(
                {
                    "pnl_total_usd": _coerce_float(trade.get("pnl_total_usd")),
                    "wins": int(trade.get("wins") or 0),
                    "losses": int(trade.get("losses") or 0),
                    "win_rate": _coerce_float(trade.get("win_rate")),
                }
            )
        except Exception:
            drawdown_usd = 0.0
        out["max_drawdown_usd"] = drawdown_usd
        out["drawdown_pct"] = (abs(drawdown_usd) / equity * 100.0) if equity > 0 else 0.0
        return out


# ---- audit -----------------------------------------------------------------


@dataclass
class StrategyAudit:
    """Structured run-journal writer.

    Emits JSONL rows under ``workspace/journals/strategy_runs.jsonl``
    with strategy_id, run_id, session_id, and a free-form ``payload``.
    The runner uses this for evidence trails ("strategy saw price=X,
    chose to hold because reason=Y"); operators replay the journal
    when reviewing a run that produced an unexpected order.
    """

    paths: WorkspacePaths
    strategy_id: str
    run_id: str
    session_id: Optional[str] = None
    _events: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)

    def log(
        self,
        kind: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        level: str = "info",
    ) -> None:
        record = {
            "kind": f"strategy.{kind}",
            "strategy_id": self.strategy_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "level": level,
            "ts": _real_now_iso(),
            "payload": dict(payload or {}),
        }
        try:
            jsonl.append(self.paths.journal("strategy_runs"), record)
        except Exception:
            _LOG.exception("strategy audit append failed")
        self._events.append(record)

    def events(self) -> list[dict[str, Any]]:
        return list(self._events)


@dataclass(frozen=True)
class StrategyTriggerContext:
    """Read-only trigger envelope visible to strategy scripts."""

    event_id: Optional[str] = None
    source: str = ""
    kind: str = ""
    target: str = ""
    strategy_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: str = ""
    idempotency_key: Optional[str] = None

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


# ---------------------------------------------------------------------------
# Lightweight schema validation (no jsonschema dep)
# ---------------------------------------------------------------------------


def _validate_minimal_schema(
    payload: dict[str, Any],
    schema: dict[str, Any],
    *,
    where: str,
) -> None:
    """Same shape as :func:`nerya.tools.executor._validate_against_schema`.

    Checks ``type`` / ``required`` / direct ``properties.<name>.type``
    and ``properties.<name>.enum``. Anything more elaborate is the
    validator's job at strategy-promotion time.
    """

    if not isinstance(schema, dict) or not schema:
        return
    expected_type = schema.get("type")
    if expected_type == "object" and not isinstance(payload, dict):
        raise StrategyRuntimeError(f"{where}: expected object, got {type(payload).__name__}")
    required = schema.get("required") or []
    if isinstance(required, list):
        for key in required:
            if key not in payload:
                raise StrategyRuntimeError(f"{where}: missing required field {key!r}")
    props = schema.get("properties") or {}
    if isinstance(props, dict):
        for name, sub in props.items():
            if name not in payload or not isinstance(sub, dict):
                continue
            sub_type = sub.get("type")
            value = payload[name]
            if sub_type == "string" and not isinstance(value, str):
                raise StrategyRuntimeError(
                    f"{where}: field {name!r} expected string, got {type(value).__name__}"
                )
            if sub_type == "number" and not isinstance(value, (int, float)):
                raise StrategyRuntimeError(
                    f"{where}: field {name!r} expected number, got {type(value).__name__}"
                )
            if sub_type == "integer" and not isinstance(value, int):
                raise StrategyRuntimeError(
                    f"{where}: field {name!r} expected integer, got {type(value).__name__}"
                )
            if sub_type == "boolean" and not isinstance(value, bool):
                raise StrategyRuntimeError(
                    f"{where}: field {name!r} expected boolean, got {type(value).__name__}"
                )
            if sub_type == "array" and not isinstance(value, list):
                raise StrategyRuntimeError(
                    f"{where}: field {name!r} expected array, got {type(value).__name__}"
                )
            enum = sub.get("enum")
            if isinstance(enum, list) and value not in enum:
                raise StrategyRuntimeError(
                    f"{where}: field {name!r} value {value!r} not in enum {enum!r}"
                )


# ---------------------------------------------------------------------------
# Top-level facade
# ---------------------------------------------------------------------------


@dataclass
class StrategyContext:
    """The single object passed into ``main.py::run(ctx)``.

    Holds nine sub-facades and a few read-only views. Constructed by
    the runner; strategy authors must not instantiate it directly.

    Attributes
    ----------
    config:
        Read-only :class:`StrategyConfig` projection (markets,
        accounts, news_sources, ...).
    policy:
        Read-only :class:`StrategyPolicyView` projection (size caps,
        confidence thresholds, llm allowed tiers, ...).
    market / news / dedupe / llm / subagents / trading / messages /
    state / clock / audit:
        Sub-facades described in the module docstring.
    result:
        :class:`~nerya.strategies.result.ResultBuilder` —
        ``return ctx.result.hold(reason="no setup")``.
    run_id / session_id / strategy_root:
        Identifiers + path the runner uses for journaling. Strategy
        code can read these; mutating them has no effect.
    """

    strategy_id: str
    run_id: str
    session_id: Optional[str]
    strategy_root: Path

    config: StrategyConfig
    policy: StrategyPolicyView
    trigger: StrategyTriggerContext
    prompt: StrategyPromptIO

    market: StrategyMarket
    news: StrategyNews
    dedupe: StrategyDedupe
    llm: StrategyLLMFacade
    subagents: StrategySubAgents
    trading: StrategyTrading
    portfolio: StrategyPortfolio
    pnl: StrategyPnL
    messages: StrategyMessages
    state: StrategyState
    clock: StrategyClock
    audit: StrategyAudit
    result: ResultBuilder = field(default_factory=ResultBuilder)
    backtest_replay: Callable[..., dict[str, Any]] | None = None

    @property
    def mode(self) -> str:
        return self.config.mode

    @property
    def runmode(self) -> str:
        return "live" if self.config.mode == "live" else self.config.mode

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(f"nerya.strategy.{self.strategy_id}")

    @property
    def log(self) -> logging.Logger:
        return self.logger

    @property
    def market_data(self) -> StrategyMarket:
        return self.market

    def ohlcv(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: Optional[str] = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Top-level OHLCV helper matching ``ctx.market.candles``."""

        return self.market.candles(
            market,
            *args,
            timeframe=interval or timeframe,
            limit=count or limit,
            account=account,
            symbol=symbol,
            **kwargs,
        )

    def get_ohlcv(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: Optional[str] = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Top-level compatibility alias matching ``ctx.ohlcv``."""

        return self.ohlcv(
            market,
            *args,
            timeframe=interval or timeframe,
            limit=count or limit,
            account=account,
            symbol=symbol,
            **kwargs,
        )

    def get_candles(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: Optional[str] = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Top-level compatibility alias for common generated-code wording."""

        return self.ohlcv(
            market,
            *args,
            timeframe=interval or timeframe,
            limit=count or limit,
            account=account,
            symbol=symbol,
            **kwargs,
        )

    def klines(
        self,
        market: str | None = None,
        *args: Any,
        timeframe: str = "1m",
        interval: str | None = None,
        limit: int = 100,
        count: int | None = None,
        account: Optional[str] = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Top-level compatibility alias matching ``ctx.market.klines``."""

        return self.ohlcv(
            market,
            *args,
            timeframe=interval or timeframe,
            limit=count or limit,
            account=account,
            symbol=symbol,
            **kwargs,
        )

    def history(
        self,
        market: str | None = None,
        timeframe: str = "1m",
        field: str = "close",
        *,
        length: int = 100,
        count: int | None = None,
        limit: int | None = None,
        symbol: str | None = None,
        **kwargs: Any,
    ) -> list[float]:
        """Return one numeric field from OHLCV rows for common generated code."""

        rows = self.ohlcv(
            market,
            timeframe=timeframe,
            limit=count or limit or length,
            symbol=symbol,
            **kwargs,
        )
        values: list[float] = []
        for row in rows:
            try:
                values.append(float(row.get(field, 0.0)))
            except Exception:
                values.append(0.0)
        return values

    def now(self) -> datetime:
        return self.clock.now()


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_strategy_context(
    *,
    config: Config,
    package: StrategyPackage,
    skills: Any = None,  # SkillKernel — kept Any to avoid import cycle
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    news_fetchers: Optional[dict[str, NewsFetcher]] = None,
    clock: Optional[StrategyClock] = None,
    connector_registry: Any = None,  # nerya.connectors.registry.ConnectorRegistry
    tool_registry: Any = None,
    executor: Any = None,
    trigger_event: Any = None,
    trigger_payload: Optional[dict[str, Any]] = None,
    trigger_event_id: Optional[str] = None,
) -> StrategyContext:
    """Construct a :class:`StrategyContext` for one strategy run.

    Parameters
    ----------
    config:
        Workspace ``Config``. Used by every facade that needs to
        reach the trading kernel / LLM gateway / paths.
    package:
        Loaded :class:`StrategyPackage` (manifest + root path).
    skills:
        :class:`~nerya.skills.kernel.SkillKernel` — required when the
        strategy declares subagents in its manifest. The runner
        injects the workspace kernel so subagent dispatch shares the
        same skill universe the main agent sees.
    run_id / session_id:
        Generated upstream by the runner. Falling back to fresh
        UUIDs makes the facade usable in unit tests that don't go
        through the runner.
    news_fetchers:
        Optional mapping ``source_id -> NewsFetcher`` registered onto
        :class:`StrategyNews`. Operators ship these via the workspace
        config; the runner will look them up by
        ``manifest.news_sources`` and call this helper.
    clock:
        Optional :class:`StrategyClock`. Defaults to wall-clock.
    connector_registry:
        Optional pre-built :class:`~nerya.connectors.registry.ConnectorRegistry`.
        When ``None`` we lazily construct one rooted at the workspace
        path; sharing one across strategies is preferable in long-
        running processes (the runner does so).
    """

    manifest: StrategyManifest = package.manifest
    paths = config.paths

    rid = run_id or uuid.uuid4().hex[:12]
    sid = session_id
    trigger_ctx = _build_trigger_context(
        trigger_event=trigger_event,
        trigger_payload=trigger_payload,
        trigger_event_id=trigger_event_id,
        strategy_id=manifest.strategy_id,
    )

    cfg_view = StrategyConfig(
        strategy_id=manifest.strategy_id,
        title=manifest.title,
        mode=manifest.mode,
        markets=manifest.markets,
        accounts=manifest.accounts,
        news_sources=manifest.news_sources,
        extras=dict(manifest.extras),
    )

    policy_view = StrategyPolicyView(
        max_single_order_usd=manifest.policy.max_single_order_usd,
        max_daily_notional_usd=manifest.policy.max_daily_notional_usd,
        max_open_positions=manifest.policy.max_open_positions,
        min_confidence=manifest.policy.min_confidence,
        allow_direct_order=manifest.policy.allow_direct_order,
        require_subagent_before_order=manifest.policy.require_subagent_before_order,
        default_order_usd=manifest.policy.default_order_usd,
        max_run_seconds=manifest.policy.max_run_seconds,
        default_tier=manifest.llm_policy.default_tier,
        allowed_tiers=manifest.llm_policy.allowed_tiers,
        max_calls_per_run=manifest.llm_policy.max_calls_per_run,
        raw_policy=manifest.policy.asdict(),
        raw_llm_policy=manifest.llm_policy.asdict(),
    )

    def _registry_factory() -> Any:
        if connector_registry is not None:
            return connector_registry
        from ..connectors.registry import ConnectorRegistry

        # Vault passphrase is intentionally None at the strategy
        # facade — strategies never resolve secrets directly. Live
        # accounts that need vaulted credentials must be invoked
        # through the trading kernel, which has its own resolver.
        return ConnectorRegistry(workspace=paths.root, vault_passphrase=None)

    market = StrategyMarket(
        paths=paths,
        accounts=manifest.accounts,
        _registry_factory=_registry_factory,
    )

    news = StrategyNews(sources=manifest.news_sources)
    # Operator-supplied fetchers override defaults (registered first so
    # the loop below can detect already-registered sources).
    if news_fetchers:
        for src, fetcher in news_fetchers.items():
            news.register(src, fetcher)
    # Auto-register defaults for built-in source names (``crypto`` /
    # ``equity``) the v6 generator emits, so every code path that builds
    # a context — script runner, agent-task executor, agent-team
    # fallback, ad-hoc tests — gets live news for free.
    try:
        from .news_fetchers import default_fetchers_for

        missing_sources = [
            s for s in manifest.news_sources
            if str(s).strip() and str(s).strip().lower() not in news._fetchers
        ]
        if missing_sources:
            for src, fetcher in default_fetchers_for(
                missing_sources, markets=manifest.markets
            ).items():
                news.register(src, fetcher)
    except Exception:  # pragma: no cover - never block context build
        pass

    state_path = package.state_dir / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = StrategyState(store=StateStore(state_path))

    dedupe = StrategyDedupe(state=state)

    llm = StrategyLLMFacade(
        config=config,
        strategy_id=manifest.strategy_id,
        default_tier=manifest.llm_policy.default_tier,
        allowed_tiers=manifest.llm_policy.allowed_tiers,
        max_calls_per_run=int(manifest.llm_policy.max_calls_per_run or 0),
    )

    subagents = StrategySubAgents(
        config=config,
        skills=skills,
        strategy_id=manifest.strategy_id,
        session_id=sid,
        tool_registry=tool_registry,
        executor=executor,
    )

    trading = StrategyTrading(
        config=config,
        strategy_id=manifest.strategy_id,
        policy=policy_view,
        accounts=manifest.accounts,
        session_id=sid,
    )
    portfolio = StrategyPortfolio(paths=paths, strategy_id=manifest.strategy_id)
    pnl = StrategyPnL(paths=paths, strategy_id=manifest.strategy_id)

    messages = StrategyMessages(
        paths=paths,
        strategy_id=manifest.strategy_id,
        session_id=sid,
    )

    audit = StrategyAudit(
        paths=paths,
        strategy_id=manifest.strategy_id,
        run_id=rid,
        session_id=sid,
    )

    prompt = StrategyPromptIO(strategy_root=package.root, run_id=rid)

    def _bound_backtest_replay(run_fn: Callable[[Any], Any], **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("markets", list(manifest.markets))
        return _strategy_backtest_replay(run_fn, **kwargs)

    return StrategyContext(
        strategy_id=manifest.strategy_id,
        run_id=rid,
        session_id=sid,
        strategy_root=package.root,
        config=cfg_view,
        policy=policy_view,
        trigger=trigger_ctx,
        prompt=prompt,
        market=market,
        news=news,
        dedupe=dedupe,
        llm=llm,
        subagents=subagents,
        trading=trading,
        portfolio=portfolio,
        pnl=pnl,
        messages=messages,
        state=state,
        clock=clock or StrategyClock(),
        audit=audit,
        backtest_replay=_bound_backtest_replay,
    )


def _build_trigger_context(
    *,
    trigger_event: Any = None,
    trigger_payload: Optional[dict[str, Any]] = None,
    trigger_event_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
) -> StrategyTriggerContext:
    payload = dict(trigger_payload or {})
    if trigger_event is not None:
        event_payload = getattr(trigger_event, "payload", None)
        if isinstance(event_payload, dict):
            payload = dict(event_payload)
        return StrategyTriggerContext(
            event_id=(
                getattr(trigger_event, "event_id", None)
                or getattr(trigger_event, "id", None)
                or trigger_event_id
            ),
            source=str(getattr(trigger_event, "source", "") or ""),
            kind=str(getattr(trigger_event, "kind", "") or ""),
            target=str(getattr(trigger_event, "target", "") or ""),
            strategy_id=getattr(trigger_event, "strategy_id", None) or strategy_id,
            payload=payload,
            idempotency_key=getattr(trigger_event, "idempotency_key", None),
        )
    return StrategyTriggerContext(
        event_id=trigger_event_id,
        strategy_id=strategy_id,
        payload=payload,
    )


__all__ = [
    "NewsFetcher",
    "ResultBuilder",
    "StrategyAudit",
    "StrategyClock",
    "StrategyConfig",
    "StrategyContext",
    "StrategyDedupe",
    "StrategyLLMFacade",
    "StrategyMarket",
    "StrategyMessages",
    "StrategyNews",
    "StrategyPnL",
    "StrategyPortfolio",
    "StrategyPosition",
    "StrategyPolicyView",
    "StrategyResult",
    "StrategyRuntimeError",
    "StrategyState",
    "StrategySubAgents",
    "StrategyTrading",
    "StrategyTriggerContext",
    "build_strategy_context",
]
