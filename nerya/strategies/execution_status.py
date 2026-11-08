"""Local receipt observations. No submit, polling, reconciliation or store boot."""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from typing import Any

from ..db.sqlite import connect_readonly


def status_result(status: str = "unknown", *, has_open_orders: bool | None = None) -> dict[str, Any]:
    terminal = status in {"filled", "rejected", "canceled"}
    return {"status": status, "terminal": terminal, "reconciled": status != "unknown",
            "has_open_orders": False if terminal else True if status == "submitted" else has_open_orders,
            "recovery": None if terminal else "wait" if status == "submitted" else "verify_execution"}


def receipt_ids(receipt: Any) -> dict[str, set[str]] | None:
    """Extract identities only; conflicting or malformed envelopes fail closed."""
    if not isinstance(receipt, dict):
        return None
    keys = ("strategy_id", "account_id", "intent_id", "order_id", "executor_id", "session_id", "plan_id", "approval_id", "reservation_id")
    ids: dict[str, set[str]] = {key: set() for key in keys}
    records = [receipt]
    for key in ("intent", "order", "executor", "approval"):
        value = receipt.get(key)
        if value is not None:
            if not isinstance(value, dict):
                return None
            records.append(value)
    orders = receipt.get("orders", [])
    if not isinstance(orders, list) or len(orders) > 1000 or any(not isinstance(o, dict) for o in orders):
        return None
    records.extend(orders)
    for record in records:
        for key in keys:
            value = record.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                return None
            ids[key].add(value)
        if "order_ids" in record:
            values = record["order_ids"]
            if not isinstance(values, list) or len(values) > 1000:
                return None
            if any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in values):
                return None
            ids["order_id"].update(values)
    if any(len(values) > 1 for key, values in ids.items() if key != "order_id"):
        return None
    if not any(ids[k] for k in ("intent_id", "order_id", "executor_id", "session_id")):
        return None
    return ids


def scoped_ids(receipt: Any, strategy_id: str, accounts: tuple[str, ...]) -> dict[str, set[str]] | None:
    ids = receipt_ids(receipt)
    if ids is None or ids["strategy_id"] - {strategy_id} or ids["account_id"] - set(accounts):
        return None
    # Approval-only/ambiguous receipts cannot establish a settled execution.
    if ids["approval_id"]:
        return None
    return ids


def execution_status(config: Any, strategy_id: str, accounts: tuple[str, ...], receipt: Any) -> dict[str, Any]:
    ids = scoped_ids(receipt, strategy_id, accounts)
    if ids is None or not accounts:
        return status_result()
    try:
        # A trade session is bound through its persisted intent ledger, not
        # through the caller's current tick/session or a receipt status label.
        if ids["session_id"]:
            path = config.paths.strategy_history(strategy_id) / "intents.jsonl"
            matches = []
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if row.get("session_id") in ids["session_id"]:
                    intent = row.get("intent") or {}
                    if intent.get("strategy_id") != strategy_id or intent.get("account_id") not in accounts:
                        return status_result()
                    matches.append(intent)
            if not matches:
                return status_result()
            for key in ("intent_id", "account_id", "plan_id", "executor_id"):
                values = {row.get(key) for row in matches}
                if values == {None} and key in {"plan_id", "executor_id"}:
                    continue
                if (len(values) != 1 or any(not isinstance(v, str) or not v.strip() for v in values)
                        or (ids[key] and ids[key] != values)):
                    return status_result()
                ids[key] = values
        # Existing helper opens query_only=ON and does not migrate/create tables.
        with closing(connect_readonly(config.paths.db)) as con:
            con.execute("BEGIN")  # one consistent read snapshot
            return _read_execution(con, strategy_id, accounts, ids)
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        return status_result()


def _read_execution(con: sqlite3.Connection, strategy_id: str, accounts: tuple[str, ...],
                    ids: dict[str, set[str]]) -> dict[str, Any]:
    # Columns are from migrations v3. Never instantiate a writing store here.
    order_columns = ("order_id,account_id,strategy_id,executor_id,intent_id,plan_id,"
                     "state,filled_size,size_base,terminal_at,reservation_id")
    run_columns = ("executor_id,account_id,strategy_id,intent_id,plan_id,state,"
                   "order_ids_json,terminal_at,reservation_ids_json")
    orders: dict[str, dict] = {}
    runs: dict[str, dict] = {}
    for key in ("order_id", "intent_id", "executor_id", "plan_id"):
        for value in ids[key]:
            rows = con.execute(f"SELECT {order_columns} FROM orders WHERE {key}=?", (value,)).fetchall()
            orders.update((row["order_id"], dict(row)) for row in rows)
            if key != "order_id":
                rows = con.execute(f"SELECT {run_columns} FROM executor_runs WHERE {key}=?", (value,)).fetchall()
                runs.update((row["executor_id"], dict(row)) for row in rows)
    for key in ("intent_id", "plan_id"):
        for value in {row[key] for row in orders.values() if row[key]}:
            rows = con.execute(f"SELECT {order_columns} FROM orders WHERE {key}=?", (value,)).fetchall()
            orders.update((row["order_id"], dict(row)) for row in rows)
            rows = con.execute(f"SELECT {run_columns} FROM executor_runs WHERE {key}=?", (value,)).fetchall()
            runs.update((row["executor_id"], dict(row)) for row in rows)
    # An order id alone must still include its executor and sibling orders.
    executor_ids = {row["executor_id"] for row in orders.values() if row["executor_id"]} | set(runs)
    for executor_id in executor_ids:
        row = con.execute(f"SELECT {run_columns} FROM executor_runs WHERE executor_id=?", (executor_id,)).fetchone()
        if row is None:
            return status_result()
        runs[executor_id] = dict(row)
    for run in runs.values():
        rows = con.execute(f"SELECT {order_columns} FROM orders WHERE executor_id=? OR intent_id=?",
                           (run["executor_id"], run["intent_id"])).fetchall()
        orders.update((row["order_id"], dict(row)) for row in rows)
        expected = json.loads(run["order_ids_json"])
        if not isinstance(expected, list) or any(not isinstance(v, str) for v in expected) or set(expected) - set(orders):
            return status_result()
    records = [*orders.values(), *runs.values()]
    if not records or len(runs) > 1:
        return status_result()
    if any(row["strategy_id"] != strategy_id or row["account_id"] not in accounts for row in records):
        return status_result()
    for key in ("account_id", "intent_id", "plan_id", "executor_id"):
        values = {row[key] for row in records}
        if len(values) > 1 or (ids[key] and values != ids[key]):
            return status_result()
    if len(orders) > 1 and not runs and not all(row["intent_id"] for row in orders.values()):
        return status_result()
    if ids["order_id"] - set(orders):
        return status_result()
    if not _obligations_settled(con, records, orders, runs, ids):
        return status_result()
    states = {row["state"] for row in orders.values()}
    active = {"created", "reserved", "submitted", "accepted", "open", "cancel_requested"}
    if states & {"partially_filled", "lost", "failed"}:
        return status_result(has_open_orders=True if "partially_filled" in states else None)
    if states & active:
        no_fills = all(float(row["filled_size"]) == 0 for row in orders.values())
        unfinished = all(row["terminal_at"] is None for row in records)
        return status_result("submitted") if states <= active and no_fills and unfinished else status_result(has_open_orders=True)
    run = next(iter(runs.values()), None)
    if run and run["state"] not in {"done", "rejected", "canceled"}:
        return status_result()
    if not orders:
        if run and run["state"] in {"rejected", "canceled"} and run["terminal_at"] is not None:
            return status_result(run["state"])
        return status_result()
    if any(row["terminal_at"] is None for row in records):
        return status_result()
    if states == {"filled"} and (not run or run["state"] == "done"):
        for order in orders.values():
            filled, size = float(order["filled_size"]), order["size_base"]
            if not math.isfinite(filled) or filled <= 0 or (size is not None and (not math.isfinite(float(size)) or filled + 1e-12 < float(size))):
                return status_result()
        return status_result("filled")
    if len(states) == 1 and states <= {"rejected", "canceled"}:
        state = next(iter(states))
        if all(float(order["filled_size"]) == 0 for order in orders.values()) and (not run or run["state"] == state):
            return status_result(state)
    return status_result()


def _obligations_settled(con, records, orders, runs, ids):
    """A terminal order cannot waive an outstanding reservation or approval."""
    expected = set(ids["reservation_id"]) | {row["reservation_id"] for row in orders.values() if row["reservation_id"]}
    for run in runs.values():
        values = json.loads(run["reservation_ids_json"])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            return False
        expected.update(values)
    reservations = {}
    for key in ("intent_id", "plan_id", "executor_id"):
        for value in {row[key] for row in records if row[key]}:
            for row in con.execute(f"SELECT * FROM capital_reservations WHERE {key}=?", (value,)):
                reservations[row["reservation_id"]] = dict(row)
    if expected - set(reservations):
        return False
    for row in reservations.values():
        if row["state"] not in {"consumed", "released", "rejected", "expired"}:
            return False
        for key in ("strategy_id", "account_id", "intent_id", "plan_id", "executor_id"):
            if row[key] is not None and row[key] not in {record[key] for record in records}:
                return False
    # Query known JSON linkage fields in the same snapshot; never log payloads.
    intents = {row["intent_id"] for row in records if row["intent_id"]}
    plans = {row["plan_id"] for row in records if row["plan_id"]}
    for row in con.execute("SELECT state,payload FROM approvals WHERE kind='trade_intent'"):
        payload = json.loads(row["payload"])
        if not isinstance(payload, dict):
            return False
        intent = payload.get("intent") or {}
        plan = payload.get("frozen_plan") or {}
        if not isinstance(intent, dict) or not isinstance(plan, dict):
            return False
        linked = bool(intents & {payload.get("intent_id"), payload.get("resumed_intent_id"), intent.get("intent_id")}
                      or plans & {payload.get("plan_id"), plan.get("plan_id")})
        if linked and row["state"] not in {"resumed", "rejected", "expired"}:
            return False
    return True
