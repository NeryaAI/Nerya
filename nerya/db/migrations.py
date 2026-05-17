"""Versioned migrations with a ``schema_version`` ledger.

the runtime records the applied migration set in a
``schema_migrations`` table so non-idempotent statements (ALTER, RENAME,
DATA fix-ups) can land safely. Until now Nerya only ran a flat list of
``CREATE TABLE IF NOT EXISTS`` statements, which works for new tables
but silently breaks the moment we need to evolve a column.

This module keeps the existing flat list operating as ``v1`` and adds a
``schema_version(version INTEGER PRIMARY KEY, applied_at REAL)``
ledger plus an ordered ``MIGRATIONS`` registry. Each migration runs
exactly once per database; an integrity check verifies the registry has
no gaps and no duplicates so accidental re-numbering is caught early.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass(frozen=True)
class Migration:
    """A single migration step.

    ``up`` may be a SQL string or a callable receiving the live
    connection. Callables are useful for data fix-ups that can't be
    expressed as a single statement.
    """

    version: int
    name: str
    up: str | Callable[[sqlite3.Connection], None]


def _ensure_schema_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at REAL NOT NULL
        )
        """
    )


def _applied_versions(con: sqlite3.Connection) -> set[int]:
    rows = con.execute("SELECT version FROM schema_version").fetchall()
    return {int(r[0]) for r in rows}


def _validate_registry(migrations: Sequence[Migration]) -> None:
    """Cheap sanity check: registry must be sorted, dense from 1, no dups."""
    versions = [m.version for m in migrations]
    if not versions:
        return
    if versions != sorted(versions):
        raise RuntimeError(
            f"db migrations are out of order: {versions}"
        )
    if len(set(versions)) != len(versions):
        raise RuntimeError(
            f"db migrations have duplicate versions: {versions}"
        )
    if versions[0] != 1:
        raise RuntimeError(
            f"db migrations must start at version 1, got {versions[0]}"
        )
    for prev, cur in zip(versions, versions[1:]):
        if cur != prev + 1:
            raise RuntimeError(
                f"db migrations have a gap between v{prev} and v{cur}"
            )


_V1_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS dedupe (
        scope TEXT NOT NULL,
        key   TEXT NOT NULL,
        ts    REAL NOT NULL,
        PRIMARY KEY (scope, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cooldown (
        scope TEXT NOT NULL,
        key   TEXT NOT NULL,
        until REAL NOT NULL,
        PRIMARY KEY (scope, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proposals (
        id     TEXT PRIMARY KEY,
        kind   TEXT NOT NULL,
        state  TEXT NOT NULL,
        path   TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS approvals (
        id         TEXT PRIMARY KEY,
        kind       TEXT NOT NULL,
        state      TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        payload    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_usage (
        ts     REAL NOT NULL,
        tier   TEXT NOT NULL,
        task   TEXT NOT NULL,
        caller TEXT NOT NULL,
        tokens INTEGER NOT NULL,
        usd    REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dedupe_ts ON dedupe(ts)",
    "CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts)",
)


def _v1(con: sqlite3.Connection) -> None:
    for stmt in _V1_STATEMENTS:
        con.execute(stmt)


def _v2_idempotent_indexes(con: sqlite3.Connection) -> None:
    """Backfill indexes the runtime uses for hot-path lookups."""
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_cooldown_until ON cooldown(until)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_proposals_state ON proposals(state, created_at)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals(state, expires_at)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_usage_caller ON llm_usage(caller, ts)"
    )


_V3_TRADING_CONTROL_PLANE = (
    # Account snapshots (live + paper + shadow). One row per snapshot
    # we pull from the venue / virtual ledger. ``raw_ref`` is a redacted
    # artifact pointer so we never store raw API payloads on the hot
    # path.
    """
    CREATE TABLE IF NOT EXISTS account_snapshots (
        snapshot_id          TEXT PRIMARY KEY,
        account_id           TEXT NOT NULL,
        ts                   REAL NOT NULL,
        source               TEXT NOT NULL,
        nav_usd              REAL NOT NULL DEFAULT 0,
        cash_by_asset_json   TEXT NOT NULL DEFAULT '{}',
        free_by_asset_json   TEXT NOT NULL DEFAULT '{}',
        locked_by_asset_json TEXT NOT NULL DEFAULT '{}',
        margin_used_usd      REAL NOT NULL DEFAULT 0,
        unrealized_pnl_usd   REAL NOT NULL DEFAULT 0,
        open_order_notional_usd REAL NOT NULL DEFAULT 0,
        health               TEXT NOT NULL DEFAULT 'ok',
        latency_ms           INTEGER NOT NULL DEFAULT 0,
        raw_ref              TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_account_snapshots_acct_ts ON account_snapshots(account_id, ts DESC)",
    # Capital reservations. The reservation lifecycle is the single
    # source of truth for "money the trading kernel has earmarked
    # for an unfinished decision".
    """
    CREATE TABLE IF NOT EXISTS capital_reservations (
        reservation_id       TEXT PRIMARY KEY,
        account_id           TEXT NOT NULL,
        strategy_id          TEXT NOT NULL,
        intent_id            TEXT,
        plan_id              TEXT,
        executor_id          TEXT,
        market               TEXT NOT NULL,
        side                 TEXT NOT NULL,
        notional_usd         REAL NOT NULL,
        estimated_fee_usd    REAL NOT NULL DEFAULT 0,
        estimated_margin_usd REAL NOT NULL DEFAULT 0,
        state                TEXT NOT NULL,
        risk_evaluation_id   TEXT,
        created_at           REAL NOT NULL,
        updated_at           REAL NOT NULL,
        expires_at           REAL,
        meta_json            TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_capital_reservations_state ON capital_reservations(account_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_capital_reservations_intent ON capital_reservations(intent_id)",
    # Durable order ledger. connector-oriented active/cached/lost split
    # via ``state`` + ``terminal_at`` (terminal orders stay around for a
    # configurable retention window so out-of-order fills/updates can
    # still be matched).
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_id            TEXT PRIMARY KEY,
        client_order_id     TEXT NOT NULL,
        exchange_order_id   TEXT,
        account_id          TEXT NOT NULL,
        strategy_id         TEXT NOT NULL,
        executor_id         TEXT,
        market              TEXT NOT NULL,
        side                TEXT NOT NULL,
        order_type          TEXT NOT NULL,
        size_base           REAL,
        notional_usd        REAL,
        price               REAL,
        stop_price          REAL,
        leverage            REAL NOT NULL DEFAULT 1,
        reduce_only         INTEGER NOT NULL DEFAULT 0,
        time_in_force       TEXT NOT NULL DEFAULT 'gtc',
        state               TEXT NOT NULL,
        filled_size         REAL NOT NULL DEFAULT 0,
        avg_price           REAL,
        fee_usd             REAL NOT NULL DEFAULT 0,
        intent_id           TEXT,
        plan_id             TEXT,
        reservation_id      TEXT,
        created_at          REAL NOT NULL,
        submitted_at        REAL,
        updated_at          REAL NOT NULL,
        terminal_at         REAL,
        last_seen_at        REAL,
        not_found_streak    INTEGER NOT NULL DEFAULT 0,
        meta_json           TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client_order_id ON orders(client_order_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state, account_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_executor ON orders(executor_id)",
    # Order events log every state transition / fetch / fill update
    # / operator action so the lifecycle is fully replayable.
    """
    CREATE TABLE IF NOT EXISTS order_events (
        event_id   TEXT PRIMARY KEY,
        order_id   TEXT NOT NULL,
        kind       TEXT NOT NULL,
        ts         REAL NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_order_events_order_id ON order_events(order_id, ts)",
    # Fills are the unit of truth for PnL / position attribution.
    # ``source`` distinguishes paper vs live vs imported-from-exchange
    # so reconciliation can compare projections cheaply.
    """
    CREATE TABLE IF NOT EXISTS fills (
        fill_id      TEXT PRIMARY KEY,
        order_id     TEXT NOT NULL,
        client_order_id TEXT,
        account_id   TEXT NOT NULL,
        strategy_id  TEXT NOT NULL,
        executor_id  TEXT,
        market       TEXT NOT NULL,
        side         TEXT NOT NULL,
        price        REAL NOT NULL,
        size_base    REAL NOT NULL,
        notional_usd REAL NOT NULL,
        fee_usd      REAL NOT NULL DEFAULT 0,
        funding_usd  REAL NOT NULL DEFAULT 0,
        source       TEXT NOT NULL DEFAULT 'paper',
        ts           REAL NOT NULL,
        intent_id    TEXT,
        meta_json    TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_fills_account_market ON fills(account_id, market, ts)",
    # Executor runs are the persistent state-machine for orchestrators.
    # Survives crash/restart so we can resume long-lived executors
    # (limit chasers, TP/SL watchers, TWAPs) without re-issuing orders.
    """
    CREATE TABLE IF NOT EXISTS executor_runs (
        executor_id      TEXT PRIMARY KEY,
        kind             TEXT NOT NULL,
        account_id       TEXT NOT NULL,
        strategy_id      TEXT NOT NULL,
        market           TEXT NOT NULL,
        state            TEXT NOT NULL,
        close_type       TEXT,
        retries          INTEGER NOT NULL DEFAULT 0,
        last_heartbeat   REAL,
        plan_json        TEXT NOT NULL DEFAULT '{}',
        config_json      TEXT NOT NULL DEFAULT '{}',
        result_json      TEXT NOT NULL DEFAULT '{}',
        order_ids_json   TEXT NOT NULL DEFAULT '[]',
        reservation_ids_json TEXT NOT NULL DEFAULT '[]',
        position_id      TEXT,
        protection_id    TEXT,
        intent_id        TEXT,
        plan_id          TEXT,
        created_at       REAL NOT NULL,
        updated_at       REAL NOT NULL,
        terminal_at      REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_executor_runs_state ON executor_runs(state, account_id)",
    "CREATE INDEX IF NOT EXISTS idx_executor_runs_strategy ON executor_runs(strategy_id, state)",
    # Persistent position book. ``source`` mirrors how the row was
    # populated (paper ledger projection / exchange import / reconciled
    # diff) so the dashboard can show provenance.
    """
    CREATE TABLE IF NOT EXISTS positions (
        position_id          TEXT PRIMARY KEY,
        account_id           TEXT NOT NULL,
        strategy_id          TEXT NOT NULL,
        market               TEXT NOT NULL,
        venue                TEXT NOT NULL DEFAULT '',
        side                 TEXT NOT NULL,
        size_base            REAL NOT NULL,
        avg_entry_price      REAL NOT NULL,
        mark_price           REAL,
        liquidation_price    REAL,
        realized_pnl_usd     REAL NOT NULL DEFAULT 0,
        unrealized_pnl_usd   REAL NOT NULL DEFAULT 0,
        fees_usd             REAL NOT NULL DEFAULT 0,
        funding_usd          REAL NOT NULL DEFAULT 0,
        leverage             REAL NOT NULL DEFAULT 1,
        source               TEXT NOT NULL DEFAULT 'paper',
        executor_id          TEXT,
        protection_id        TEXT,
        opened_at            REAL NOT NULL,
        updated_at           REAL NOT NULL,
        closed_at            REAL,
        meta_json            TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(account_id, market) WHERE closed_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions(strategy_id, closed_at)",
    """
    CREATE TABLE IF NOT EXISTS position_events (
        event_id    TEXT PRIMARY KEY,
        position_id TEXT NOT NULL,
        kind        TEXT NOT NULL,
        ts          REAL NOT NULL,
        size_delta  REAL NOT NULL DEFAULT 0,
        price       REAL,
        pnl_delta   REAL NOT NULL DEFAULT 0,
        fee_delta   REAL NOT NULL DEFAULT 0,
        order_id    TEXT,
        fill_id     TEXT,
        payload_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_position_events_pos ON position_events(position_id, ts)",
    # Protection rules attached to open positions.
    """
    CREATE TABLE IF NOT EXISTS protection_rules (
        protection_id   TEXT PRIMARY KEY,
        position_id     TEXT NOT NULL,
        executor_id     TEXT,
        strategy_id     TEXT NOT NULL,
        account_id      TEXT NOT NULL,
        market          TEXT NOT NULL,
        side            TEXT NOT NULL,
        mode            TEXT NOT NULL,
        status          TEXT NOT NULL,
        trigger_source  TEXT NOT NULL DEFAULT 'mark',
        time_limit_sec  INTEGER,
        rule_json       TEXT NOT NULL,
        exchange_order_ids_json TEXT NOT NULL DEFAULT '{}',
        created_at      REAL NOT NULL,
        updated_at      REAL NOT NULL,
        triggered_at    REAL,
        triggered_kind  TEXT,
        notes           TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_protection_rules_pos ON protection_rules(position_id, status)",
    # Risk evaluations — give every RiskGate decision a stable id so
    # downstream artifacts (reservation, executor, journal entry) can
    # link back to it.
    """
    CREATE TABLE IF NOT EXISTS risk_evaluations (
        risk_evaluation_id TEXT PRIMARY KEY,
        intent_id          TEXT,
        plan_id            TEXT,
        strategy_id        TEXT NOT NULL,
        account_id         TEXT NOT NULL,
        decision           TEXT NOT NULL,
        notional_usd       REAL NOT NULL DEFAULT 0,
        reasons_json       TEXT NOT NULL DEFAULT '[]',
        snapshot_json      TEXT NOT NULL DEFAULT '{}',
        ts                 REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_risk_evaluations_strategy ON risk_evaluations(strategy_id, ts DESC)",
    # Reconciliation reports. Severity-tagged records of each
    # local-projection vs exchange-truth diff run.
    """
    CREATE TABLE IF NOT EXISTS reconciliation_reports (
        report_id   TEXT PRIMARY KEY,
        ts          REAL NOT NULL,
        scope       TEXT NOT NULL,
        account_id  TEXT,
        strategy_id TEXT,
        severity    TEXT NOT NULL,
        summary_json TEXT NOT NULL DEFAULT '{}',
        issues_json TEXT NOT NULL DEFAULT '[]'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reconciliation_reports_ts ON reconciliation_reports(ts DESC)",
)


def _v3_trading_control_plane(con: sqlite3.Connection) -> None:
    """Trading control-plane.

    Introduces the durable tables that back account snapshots, capital
    reservations, the order tracker, executor runs, the position book,
    protection rules, risk evaluations, and reconciliation reports.
    Every statement is idempotent so re-running on an existing DB is
    safe.
    """
    for stmt in _V3_TRADING_CONTROL_PLANE:
        con.execute(stmt)


_V4_STRATEGY_PROMOTION = (
    # promotion gate audit trail. Every requested move
    # along the lifecycle graph (draft -> static_review -> backtested
    # -> paper -> shadow -> canary -> live) writes a row here, even
    # if the gate ultimately rejects it. ``state`` tracks the lifecycle
    # of the request itself (pending / approved / rejected / expired /
    # applied) — *not* the strategy state.
    """
    CREATE TABLE IF NOT EXISTS strategy_promotions (
        promotion_id    TEXT PRIMARY KEY,
        strategy_id     TEXT NOT NULL,
        from_state      TEXT NOT NULL,
        to_state        TEXT NOT NULL,
        state           TEXT NOT NULL,
        reason_blocked  TEXT,
        evidence_json   TEXT NOT NULL DEFAULT '[]',
        approval_id     TEXT,
        ts_requested    REAL NOT NULL,
        ts_resolved     REAL,
        ts_applied      REAL,
        operator        TEXT,
        notes           TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_strategy_promotions_strategy ON strategy_promotions(strategy_id)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_promotions_state ON strategy_promotions(state)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_promotions_ts ON strategy_promotions(ts_requested DESC)",
    # strategy evidence catalogue. Backtest reports, paper
    # window stats, static-review verdicts, protection-rule attestations,
    # subagent reviews — everything the promotion gate inspects ends
    # up as a row here so the dashboard can render an at-a-glance
    # readiness snapshot per strategy.
    """
    CREATE TABLE IF NOT EXISTS strategy_evidence (
        evidence_id     TEXT PRIMARY KEY,
        strategy_id     TEXT NOT NULL,
        kind            TEXT NOT NULL,
        ts              REAL NOT NULL,
        pass            INTEGER NOT NULL,
        payload_json    TEXT NOT NULL DEFAULT '{}',
        artifact_ref    TEXT,
        operator        TEXT,
        expires_at      REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_strategy_evidence_strategy ON strategy_evidence(strategy_id)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_evidence_kind ON strategy_evidence(kind)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_evidence_ts ON strategy_evidence(ts DESC)",
)


def _v4_strategy_promotion(con: sqlite3.Connection) -> None:
    """Strategy promotion gate.

    Adds the durable tables that back the promotion ramp:

    * ``strategy_promotions`` — every promotion request (incl. blocked).
    * ``strategy_evidence``   — backtest / static-review / protection /
      paper-window evidence the gate consumes.

    These are the pieces that prove "Agent strategies don't jump from
    draft straight to live" — without them the rule lives only in
    documentation.
    """
    for stmt in _V4_STRATEGY_PROMOTION:
        con.execute(stmt)


_V5_AGENT_CHAT_SESSIONS = (
    """
    CREATE TABLE IF NOT EXISTS agent_sessions (
        session_id   TEXT PRIMARY KEY,
        strategy_id  TEXT,
        title        TEXT NOT NULL DEFAULT '',
        source       TEXT NOT NULL DEFAULT '',
        created_at   REAL NOT NULL,
        updated_at   REAL NOT NULL,
        meta_json    TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_sessions_updated ON agent_sessions(updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS agent_messages (
        message_id  TEXT PRIMARY KEY,
        session_id  TEXT NOT NULL,
        turn_id     TEXT,
        role        TEXT NOT NULL,
        content     TEXT NOT NULL,
        ts          REAL NOT NULL,
        deleted     INTEGER NOT NULL DEFAULT 0,
        meta_json   TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_messages_session_ts ON agent_messages(session_id, ts)",
    """
    CREATE TABLE IF NOT EXISTS agent_tool_events (
        event_id    TEXT PRIMARY KEY,
        session_id  TEXT NOT NULL,
        turn_id     TEXT,
        call_id     TEXT,
        tool        TEXT NOT NULL,
        phase       TEXT NOT NULL,
        ok          INTEGER,
        ts          REAL NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_tool_events_session_ts ON agent_tool_events(session_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_agent_tool_events_call ON agent_tool_events(call_id)",
)


def _v5_agent_chat_sessions(con: sqlite3.Connection) -> None:
    """Durable shared chat/session tables.

    JSONL remains the full replay log, but every frontend/gateway/curl
    session now has a SQLite row plus queryable message and tool-event
    rows. This gives all gateway clients the same session catalogue and
    keeps intermediate tool calls visible without scanning raw journals.
    """
    for stmt in _V5_AGENT_CHAT_SESSIONS:
        con.execute(stmt)


_V6_POSITION_SHARES_DDL = (
    # Per-strategy slice of a merged (account_id, market) position. Each
    # share keeps that strategy's own avg-entry, realized PnL, fees and
    # funding — so the strategy detail page can still show "my PnL".
    # The parent ``positions`` row remains the source of truth for the
    # merged size_base and unrealized PnL: it equals SUM(shares.size_share_base)
    # but is materialised for cheap reads and to keep mark-price updates
    # cheap (one row per merged position rather than one per strategy).
    """
    CREATE TABLE IF NOT EXISTS position_shares (
        share_id              TEXT PRIMARY KEY,
        position_id           TEXT NOT NULL,
        account_id            TEXT NOT NULL,
        strategy_id           TEXT NOT NULL,
        market                TEXT NOT NULL,
        venue                 TEXT NOT NULL DEFAULT '',
        size_share_base       REAL NOT NULL DEFAULT 0,
        avg_entry_share_price REAL NOT NULL DEFAULT 0,
        realized_pnl_share_usd REAL NOT NULL DEFAULT 0,
        fees_share_usd        REAL NOT NULL DEFAULT 0,
        funding_share_usd     REAL NOT NULL DEFAULT 0,
        opened_at             REAL NOT NULL,
        updated_at            REAL NOT NULL,
        closed_at             REAL,
        meta_json             TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # Exactly one open share per (position, strategy). A second-open by
    # the same strategy must update the existing row, not insert a new
    # one — same invariant the merged ``positions`` row enforces above
    # at (account_id, market).
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_position_shares_open ON position_shares(position_id, strategy_id) WHERE closed_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_position_shares_strategy_open ON position_shares(strategy_id, account_id, market) WHERE closed_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_position_shares_account ON position_shares(account_id, closed_at)",
)


def _v6_position_shares(con: sqlite3.Connection) -> None:
    """Merge same-(account, market) positions across strategies.

    Until now ``positions`` was keyed by ``(account_id, strategy_id, market)``
    so two strategies running on the same exchange + symbol produced two
    independent rows — even though the broker only sees one merged
    position. v6 promotes the merged position to the primary row and
    moves strategy-level attribution into the new ``position_shares``
    table.

    Steps (run as one transaction):

    1. Create ``position_shares``.
    2. For every existing OPEN positions row, create the corresponding
       share row (1:1 backfill so single-strategy callers keep working
       and the merged sums match the prior per-strategy state).
    3. Collapse rows where multiple strategies share an
       (account_id, market) tuple into one merged row, summing
       ``size_base`` (signed), recomputing ``avg_entry_price`` from
       cost-basis, and aggregating realised PnL / fees / funding /
       unrealized. The surviving row keeps the oldest ``position_id``
       so downstream FK-style references (protection_rules.position_id)
       remain valid; the absorbed rows are closed with a marker event.
    4. Install the unique partial index that locks "one open merged row
       per (account_id, market)" going forward.

    Closed-history rows are left untouched — they're audit-only and
    keying them differently is a non-goal.
    """

    for stmt in _V6_POSITION_SHARES_DDL:
        con.execute(stmt)

    # Inspect actual columns once so the backfill survives both fresh
    # databases (v1 schema as-shipped) and any legacy column ordering
    # we might pick up in the wild.
    cur = con.execute("PRAGMA table_info(positions)")
    pos_cols = {row[1] for row in cur.fetchall()}
    if not pos_cols:
        return  # positions table was never created — nothing to backfill

    now = time.time()

    # Step 2: 1:1 share backfill for every still-open positions row.
    # We skip rows where a share already exists so re-applying the
    # migration after a partial run remains safe.
    open_rows = con.execute(
        """
        SELECT position_id, account_id, strategy_id, market, venue,
               size_base, avg_entry_price,
               COALESCE(realized_pnl_usd, 0), COALESCE(fees_usd, 0),
               COALESCE(funding_usd, 0), opened_at, updated_at
          FROM positions
         WHERE closed_at IS NULL
        """
    ).fetchall()

    for row in open_rows:
        (
            position_id, account_id, strategy_id, market, venue,
            size_base, avg_entry_price,
            realized, fees, funding, opened_at, updated_at,
        ) = row
        existing = con.execute(
            "SELECT 1 FROM position_shares WHERE position_id = ? AND strategy_id = ? AND closed_at IS NULL",
            (position_id, strategy_id),
        ).fetchone()
        if existing:
            continue
        share_id = f"shr_{position_id[-12:]}_{strategy_id[:16]}"
        con.execute(
            """
            INSERT INTO position_shares(
                share_id, position_id, account_id, strategy_id, market, venue,
                size_share_base, avg_entry_share_price,
                realized_pnl_share_usd, fees_share_usd, funding_share_usd,
                opened_at, updated_at, closed_at, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '{"source":"v6_backfill"}')
            """,
            (
                share_id, position_id, account_id, strategy_id, market, venue or "",
                float(size_base or 0), float(avg_entry_price or 0),
                float(realized or 0), float(fees or 0), float(funding or 0),
                float(opened_at or now), float(updated_at or now),
            ),
        )

    # Step 3: collapse duplicate (account_id, market) open rows into
    # one merged row. We pick the oldest ``position_id`` as the
    # survivor and re-point all shares to it. Avg entry on the merged
    # row uses cost-basis aggregation so opposite-direction shares net
    # cleanly: avg = SUM(size_i * price_i) / SUM(size_i).
    dupe_groups = con.execute(
        """
        SELECT account_id, market
          FROM positions
         WHERE closed_at IS NULL
         GROUP BY account_id, market
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    for account_id, market in dupe_groups:
        rows = con.execute(
            """
            SELECT position_id, strategy_id, venue, side,
                   size_base, avg_entry_price,
                   mark_price, liquidation_price,
                   realized_pnl_usd, unrealized_pnl_usd,
                   fees_usd, funding_usd, leverage, source,
                   executor_id, protection_id,
                   opened_at, updated_at
              FROM positions
             WHERE account_id = ? AND market = ? AND closed_at IS NULL
             ORDER BY opened_at ASC
            """,
            (account_id, market),
        ).fetchall()

        survivor_id = rows[0][0]
        cost_basis = 0.0
        size_total = 0.0
        notional_total = 0.0
        realized_sum = 0.0
        unrealized_sum = 0.0
        fees_sum = 0.0
        funding_sum = 0.0
        max_leverage = 0.0
        first_opened = rows[0][16]
        last_updated = rows[0][17]
        venue_value = rows[0][2] or ""
        mark_value = rows[0][6]
        source_value = rows[0][13] or "paper"

        for r in rows:
            sz = float(r[4] or 0)
            avg = float(r[5] or 0)
            cost_basis += sz * avg
            size_total += sz
            notional_total += abs(sz) * avg
            realized_sum += float(r[8] or 0)
            unrealized_sum += float(r[9] or 0)
            fees_sum += float(r[10] or 0)
            funding_sum += float(r[11] or 0)
            max_leverage = max(max_leverage, float(r[12] or 1))
            first_opened = min(first_opened, float(r[16] or first_opened))
            last_updated = max(last_updated, float(r[17] or last_updated))

        merged_avg = (cost_basis / size_total) if abs(size_total) > 1e-12 else 0.0
        merged_side = "long" if size_total > 0 else ("short" if size_total < 0 else "long")

        # Re-point every share that used to point at one of the
        # absorbed rows at the surviving merged row. The absorbed
        # row's share already exists from step 2, so this is just a
        # FK rewrite.
        for r in rows[1:]:
            absorbed_id = r[0]
            con.execute(
                "UPDATE position_shares SET position_id = ?, updated_at = ? WHERE position_id = ?",
                (survivor_id, now, absorbed_id),
            )
            # Close the absorbed positions row with a marker so audit
            # history shows the merge — emit a position_event for it.
            con.execute(
                "UPDATE positions SET closed_at = ?, updated_at = ?, meta_json = COALESCE(meta_json, '{}') WHERE position_id = ?",
                (now, now, absorbed_id),
            )
            con.execute(
                """
                INSERT OR IGNORE INTO position_events(
                    event_id, position_id, kind, ts,
                    size_delta, price, pnl_delta, fee_delta,
                    order_id, fill_id, payload_json
                ) VALUES (?, ?, 'merged_into', ?, 0, NULL, 0, 0, NULL, NULL, ?)
                """,
                (
                    f"evt_v6merge_{absorbed_id[-12:]}",
                    absorbed_id,
                    now,
                    f'{{"merged_into":"{survivor_id}"}}',
                ),
            )

        # Update the survivor with merged aggregates. strategy_id on
        # the merged row is set to a sentinel so callers know to read
        # ``position_shares`` for per-strategy attribution.
        con.execute(
            """
            UPDATE positions
               SET strategy_id      = '__merged__',
                   side             = ?,
                   size_base        = ?,
                   avg_entry_price  = ?,
                   realized_pnl_usd = ?,
                   unrealized_pnl_usd = ?,
                   fees_usd         = ?,
                   funding_usd      = ?,
                   leverage         = ?,
                   opened_at        = ?,
                   updated_at       = ?,
                   meta_json        = json_set(COALESCE(meta_json, '{}'), '$.v6_merged', json('true'))
             WHERE position_id = ?
            """,
            (
                merged_side,
                size_total,
                merged_avg,
                realized_sum,
                unrealized_sum,
                fees_sum,
                funding_sum,
                max_leverage or 1.0,
                first_opened,
                last_updated,
                survivor_id,
            ),
        )

    # Step 4: enforce "one open merged position per (account, market)".
    # The partial index makes future inserts that ignore the merge
    # contract fail loudly instead of silently shadowing the merged
    # row.
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_open_unique "
        "ON positions(account_id, market) WHERE closed_at IS NULL"
    )


# Append new migrations to the end of this list. Never renumber; new
# work always becomes a *new* version. The registry validator above
# enforces a contiguous 1..N sequence at startup.
MIGRATIONS: list[Migration] = [
    Migration(version=1, name="initial_tables", up=_v1),
    Migration(version=2, name="hot_path_indexes", up=_v2_idempotent_indexes),
    Migration(version=3, name="trading_control_plane", up=_v3_trading_control_plane),
    Migration(version=4, name="strategy_promotion", up=_v4_strategy_promotion),
    Migration(version=5, name="agent_chat_sessions", up=_v5_agent_chat_sessions),
    Migration(version=6, name="position_shares_merge", up=_v6_position_shares),
]


def apply_migrations(con: sqlite3.Connection) -> list[int]:
    """Apply pending migrations; return the list of versions newly applied."""

    _validate_registry(MIGRATIONS)
    _ensure_schema_table(con)
    already = _applied_versions(con)
    applied_now: list[int] = []
    for mig in MIGRATIONS:
        if mig.version in already:
            continue
        if callable(mig.up):
            mig.up(con)
        else:
            con.execute(mig.up)
        con.execute(
            "INSERT INTO schema_version(version, name, applied_at) VALUES (?, ?, ?)",
            (mig.version, mig.name, time.time()),
        )
        applied_now.append(mig.version)
    con.commit()
    return applied_now


def current_version(con: sqlite3.Connection) -> int:
    """Return the highest applied schema version (0 if untouched)."""

    _ensure_schema_table(con)
    row = con.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0]) if row and row[0] is not None else 0
