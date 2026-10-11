"""Independent durable protection/recovery worker; it never invents order retries."""
from dataclasses import fields
import json
import logging
import time
import uuid

from ..db.sqlite import connect
from .locks import trading_lock

log = logging.getLogger(__name__)


def active(config):
    con = connect(config.paths.db)
    try:
        row = con.execute("SELECT lease_until FROM trading_guard_lease WHERE name='primary'").fetchone()
        return bool(row and row[0] > time.time())
    finally:
        con.close()


class TradingGuard:
    def __init__(self, config, *, interval=5):
        self.config = config
        self.interval = max(1, float(interval))
        self.owner = uuid.uuid4().hex

    def claim(self):
        con = connect(self.config.paths.db)
        try:
            now = time.time()
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM trading_guard_lease WHERE name='primary'").fetchone()
            if row and row["owner"] != self.owner and row["lease_until"] > now:
                con.rollback()
                return False
            con.execute("INSERT INTO trading_guard_lease VALUES ('primary',?,?,?) "
                        "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,lease_until=excluded.lease_until,heartbeat=excluded.heartbeat",
                        (self.owner, now + self.interval * 3, now))
            con.commit()
            return True
        finally:
            con.close()

    def release(self):
        con = connect(self.config.paths.db)
        try:
            con.execute("DELETE FROM trading_guard_lease WHERE name='primary' AND owner=?", (self.owner,))
        finally:
            con.close()

    def run_once(self):
        if not self.claim():
            return {"state": "standby", "executors": 0, "financial_actions": 0}
        with trading_lock(self.config.paths, "trading_guard") as owned:
            if not owned:
                return {"state": "busy", "executors": 0, "financial_actions": 0}
            from contextlib import closing
            from .order_polling import poll_active_live_orders
            from .executors.orchestrator import ExecutorOrchestrator
            from ..wallet.swap_approval import reconcile_pending
            poll_active_live_orders(self.config)
            with closing(ExecutorOrchestrator(self.config)) as orchestrator:
                executors = orchestrator.run_once()
            reconcile_pending(self.config)
            from ..financial.contracts import FinancialContext
            from ..financial.gateway import FinancialGateway
            con = connect(self.config.paths.db)
            try:
                rows = con.execute("SELECT action_id,context_json FROM financial_actions "
                                   "WHERE state IN ('submitted','confirming','unconfirmed','needs_recovery') ORDER BY updated_at LIMIT 100").fetchall()
            finally:
                con.close()
            count = 0
            allowed = {field.name for field in fields(FinancialContext)}
            for row in rows:
                raw = json.loads(row["context_json"])
                values = {key: value for key, value in raw.items() if key in allowed}
                values["scopes"] = frozenset(values.get("scopes") or ())
                try:
                    FinancialGateway(self.config).reconcile(FinancialContext(**values), row["action_id"])
                    count += 1
                except Exception:
                    log.exception("financial receipt recovery failed for %s", row["action_id"])
            return {"state": "running", "executors": executors, "financial_actions": count}

    def run(self):
        try:
            while True:
                try:
                    self.run_once()
                except Exception:
                    log.exception("trading guard tick failed; durable state retained")
                time.sleep(self.interval)
        finally:
            self.release()
