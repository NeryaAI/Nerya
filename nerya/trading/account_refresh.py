"""Periodic account mark/NAV refresh for paper and live accounts."""

from __future__ import annotations

import logging
import time
from typing import Any

from ..core import jsonl
from ..core.config import Config
from ..core.time import now_iso
from .account_snapshots import (
    AccountSnapshot,
    capture_snapshot,
    latest_snapshot,
)
from .accounts import AccountProfile, load_account_profiles
from .executors.orchestrator import ExecutorOrchestrator
from .position_book import PositionBook
from .virtual_ledger import open_ledger

log = logging.getLogger(__name__)

# Paper accounts project from the local virtual ledger, so the cadence
# is essentially CPU-bound — staying at 5 minutes keeps the dashboard
# responsive without thrashing SQLite. Real accounts (live / canary /
# shadow) talk to a venue or RPC, so we want a tighter loop that
# still respects rate limits. Operators can tune both via
# ``trading.account_refresh.*`` in ``nerya.yml``.
DEFAULT_ACCOUNT_REFRESH_INTERVAL_S = 300.0
DEFAULT_LIVE_REFRESH_INTERVAL_S = 60.0


def _coerce_seconds(raw: Any, fallback: float, *, minimum: float = 5.0) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(fallback)
    return max(minimum, value)


def account_refresh_interval_seconds(config: Config) -> float:
    """Configured paper-account refresh cadence (default 5 minutes).

    Kept named the same so old callers (CLI, smoke scripts) keep
    working. Real-money accounts route through
    :func:`account_refresh_interval_for_profile` instead.
    """

    raw = config.get(
        "trading.account_refresh_interval_seconds",
        DEFAULT_ACCOUNT_REFRESH_INTERVAL_S,
    )
    return _coerce_seconds(raw, DEFAULT_ACCOUNT_REFRESH_INTERVAL_S)


def live_refresh_interval_seconds(config: Config) -> float:
    """Configured live/canary/shadow refresh cadence (default 60s)."""

    raw = config.get(
        "trading.account_refresh.live_interval_seconds",
        DEFAULT_LIVE_REFRESH_INTERVAL_S,
    )
    return _coerce_seconds(raw, DEFAULT_LIVE_REFRESH_INTERVAL_S)


def account_refresh_interval_for_profile(
    config: Config, profile: AccountProfile
) -> float:
    """Cadence applied to a specific account.

    - ``paper``: 5 min default (CPU-cheap, mostly idle).
    - ``live`` / ``canary``: 60 s default — operators rely on the
      snapshot for live PnL and risk decisions.
    - ``shadow``: matches live cadence; we still hit the venue to
      compare against the agent's projected state.
    """

    if profile.mode in ("live", "canary", "shadow"):
        return live_refresh_interval_seconds(config)
    return account_refresh_interval_seconds(config)


def next_refresh_ts_for_profile(
    config: Config,
    profile: AccountProfile,
    *,
    snapshot: AccountSnapshot | None = None,
) -> float:
    """Best-effort ETA for the next snapshot tick.

    Reads the latest snapshot ``ts`` if not supplied, adds the
    per-profile interval, and clamps to "now" so a stale loop doesn't
    show a negative countdown in the UI.
    """

    snap = snapshot
    if snap is None:
        try:
            snap = latest_snapshot(config.paths, profile.id)
        except Exception:  # pragma: no cover - defensive
            snap = None
    interval = account_refresh_interval_for_profile(config, profile)
    base = float(snap.ts) if snap is not None else time.time()
    return max(base + interval, time.time())


def refresh_account_marks(
    config: Config,
    *,
    account_id: str | None = None,
    persist_snapshot: bool = True,
    run_executors: bool = True,
    only_due: bool = False,
) -> dict[str, Any]:
    """Refresh open-position marks, drive active executors, and capture NAV.

    This is intentionally idempotent and safe for the local server's
    background loop. Public market-data failures are reported in the
    returned payload and journal but do not abort other accounts.

    ``only_due=True`` skips profiles whose latest snapshot is younger
    than their configured cadence — useful for the background loop so
    paper accounts don't get hit every 60s alongside live ones.
    """

    profiles = load_account_profiles(config.paths)
    selected: list[AccountProfile] = []
    now = time.time()
    for aid, profile in profiles.items():
        if account_id is not None and aid != account_id:
            continue
        if only_due:
            interval = account_refresh_interval_for_profile(config, profile)
            try:
                snap = latest_snapshot(config.paths, aid)
            except Exception:  # pragma: no cover - defensive
                snap = None
            if snap is not None and (now - float(snap.ts)) < interval:
                continue
        selected.append(profile)
    book = PositionBook(config.paths)
    account_rows: list[dict[str, Any]] = []
    marks_by_account: dict[str, dict[str, float]] = {}

    for profile in selected:
        marks: dict[str, float] = {}
        sources: dict[str, str] = {}
        errors: list[dict[str, str]] = []
        positions = book.open_positions(account_id=profile.id)
        markets = {p.market for p in positions if p.market}
        markets.update(_paper_ledger_markets(config, profile))

        for market in sorted(markets):
            price, source, error = _fetch_mark(config, market)
            if price is None:
                if error:
                    errors.append({"market": market, "error": error})
                continue
            marks[market] = price
            sources[market] = source or "market_data"
            for pos in positions:
                if pos.market == market:
                    book.update_mark(pos.position_id, price)

        marks_by_account[profile.id] = marks
        account_rows.append({
            "account_id": profile.id,
            "mode": profile.mode,
            "markets_checked": sorted(markets),
            "marks": marks,
            "mark_sources": sources,
            "errors": errors,
        })

    executors_touched = 0
    if run_executors:
        try:
            executors_touched = ExecutorOrchestrator(config).run_once()
        except Exception as exc:  # pragma: no cover - background loop guard
            log.exception("account refresh executor tick failed")
            account_rows.append({
                "account_id": account_id or "*",
                "errors": [{"market": "*", "error": f"executor_tick_failed: {exc}"}],
            })

    snapshots: dict[str, dict[str, Any]] = {}
    for profile in selected:
        try:
            snap = capture_snapshot(
                config,
                profile.id,
                profile=profile,
                persist=persist_snapshot,
                marks=marks_by_account.get(profile.id),
            )
            snapshots[profile.id] = snap.asdict()
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("account refresh snapshot failed for %s", profile.id)
            snapshots[profile.id] = {"error": str(exc)}

    result = {
        "ok": True,
        "ts": now_iso(),
        "accounts": account_rows,
        "snapshots": snapshots,
        "executors_touched": executors_touched,
    }
    try:
        jsonl.append(config.paths.journal("trading"), {
            "kind": "account_marks.refreshed",
            "ts": result["ts"],
            "account_id": account_id,
            "accounts": account_rows,
            "executors_touched": executors_touched,
        })
    except Exception:  # pragma: no cover
        log.debug("account refresh journal append failed", exc_info=True)
    return result


def _paper_ledger_markets(config: Config, profile: AccountProfile) -> set[str]:
    if profile.mode != "paper":
        return set()
    try:
        ledger = open_ledger(config.paths, profile.id, profile.initial_balance_usd)
        snap = ledger.snapshot()
        positions = snap.get("positions") or {}
        return {
            str(market)
            for market, pos in positions.items()
            if float((pos or {}).get("size") or 0.0)
        }
    except Exception:  # pragma: no cover
        log.debug("paper ledger market lookup failed for %s", profile.id, exc_info=True)
        return set()


def _fetch_mark(config: Config, market: str) -> tuple[float | None, str, str]:
    try:
        from ..data.candles import fetch_public_ticker

        snap = fetch_public_ticker(market, allow_mock=False, config_like=config)
        price = float(snap.get("price") or 0.0)
        if price <= 0:
            env = snap.get("_envelope") or {}
            return None, "", str(env.get("error") or "no_mark_price")
        return price, str(snap.get("source") or ""), ""
    except Exception as exc:
        return None, "", f"{type(exc).__name__}: {exc}"


__all__ = [
    "DEFAULT_ACCOUNT_REFRESH_INTERVAL_S",
    "DEFAULT_LIVE_REFRESH_INTERVAL_S",
    "account_refresh_interval_seconds",
    "live_refresh_interval_seconds",
    "account_refresh_interval_for_profile",
    "next_refresh_ts_for_profile",
    "refresh_account_marks",
]
