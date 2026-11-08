"""Runtime receipt authority against the real migration schema in temporary DBs."""
import importlib
import json
import sqlite3

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.db.sqlite import connect
from nerya.strategies.context import StrategyTrading

module = importlib.import_module("nerya.strategies.execution_status")
pytestmark = pytest.mark.smoke


@pytest.fixture
def runtime(tmp_path):
    config = Config(paths=WorkspacePaths(root=tmp_path), data={})
    con = connect(config.paths.db)  # actual migrations, fixture writes only
    trading = StrategyTrading(config, "mine", None, ("paper", "other_allowed"))
    yield trading, con
    con.close()


def execution(runtime, suffix="1", *, strategy="mine", account="paper"):
    trading, con = runtime
    ids = {"order_id": f"o{suffix}", "executor_id": f"e{suffix}",
           "intent_id": f"i{suffix}", "plan_id": f"p{suffix}", "session_id": f"s{suffix}"}
    con.execute("""INSERT INTO executor_runs
        (executor_id,kind,account_id,strategy_id,market,state,intent_id,plan_id,
         order_ids_json,created_at,updated_at,terminal_at) VALUES (?,?,?,?,?,?,?,?,?,1,2,2)""",
        (ids["executor_id"], "market_order", account, strategy, "MOCK:BTCUSDT",
         "done", ids["intent_id"], ids["plan_id"], json.dumps([ids["order_id"]])))
    order(runtime, ids, strategy=strategy, account=account)
    path = trading.config.paths.strategy_history(strategy) / "intents.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        out.write(json.dumps({"session_id": ids["session_id"],
            "intent": {"intent_id": ids["intent_id"], "plan_id": ids["plan_id"],
                       "strategy_id": strategy, "account_id": account}}) + "\n")
    return ids


def order(runtime, ids, *, strategy="mine", account="paper", state="filled", filled=1):
    runtime[1].execute("""INSERT INTO orders
        (order_id,client_order_id,account_id,strategy_id,executor_id,market,side,order_type,
         size_base,state,filled_size,intent_id,plan_id,created_at,updated_at,terminal_at)
        VALUES (?,?,?,?,?, 'MOCK:BTCUSDT','buy','market',1,?,?,?,?,1,2,2)""",
        (ids["order_id"], ids["order_id"], account, strategy, ids["executor_id"],
         state, filled, ids["intent_id"], ids["plan_id"]))


def unknown(trading, receipt):
    result = trading.execution_status(receipt)
    assert result["status"] == "unknown" and result["terminal"] is False
    assert result["reconciled"] is False and result["recovery"] == "verify_execution"
    json.dumps(result, allow_nan=False)
    return result


@pytest.mark.parametrize("anchor", [None, "order_id", "intent_id", "executor_id", "session_id"])
def test_exact_receipt_and_individual_ids(runtime, anchor):
    ids = execution(runtime)
    receipt = {anchor: ids[anchor]} if anchor else {**ids, "status": "rejected"}
    assert runtime[0].execution_status(receipt) == {
        "status": "filled", "terminal": True, "reconciled": True,
        "has_open_orders": False, "recovery": None}


@pytest.mark.parametrize("key", ["order_id", "intent_id", "executor_id", "plan_id", "session_id"])
def test_all_ids_must_match_one_execution(runtime, key):
    first, second = execution(runtime), execution(runtime, "2")
    unknown(runtime[0], {**first, key: second[key]})
    unknown(runtime[0], {**first, key: "missing"})


@pytest.mark.parametrize("owner", [{"strategy": "foreign"}, {"account": "foreign"}])
def test_foreign_records_cannot_be_spoofed(runtime, owner):
    ids = execution(runtime, **owner)
    unknown(runtime[0], {"order_id": ids["order_id"], "strategy_id": "mine",
                         "account_id": "paper", "status": "filled"})


@pytest.mark.parametrize("key,value", [("account_id", "other_allowed"), ("plan_id", "other_plan")])
def test_session_ledger_identity_must_match_database(runtime, key, value):
    ids = execution(runtime)
    path = runtime[0].config.paths.strategy_history("mine") / "intents.jsonl"
    row = json.loads(path.read_text())
    row["intent"][key] = value
    path.write_text(json.dumps(row) + "\n")
    unknown(runtime[0], {"session_id": ids["session_id"], "order_id": ids["order_id"]})


@pytest.mark.parametrize("state,filled", [("partially_filled", .5), ("open", .5),
    ("filled", .5), ("canceled", .5), ("lost", 0), ("failed", 0)])
def test_uncertain_and_partial_orders_stay_unknown(runtime, state, filled):
    ids = execution(runtime)
    runtime[1].execute("UPDATE orders SET state=?,filled_size=?", (state, filled))
    unknown(runtime[0], ids)


def test_open_order_is_nonterminal(runtime):
    ids = execution(runtime)
    runtime[1].execute("UPDATE orders SET state='open',filled_size=0,terminal_at=NULL")
    runtime[1].execute("UPDATE executor_runs SET state='working',terminal_at=NULL")
    assert runtime[0].execution_status(ids) == {
        "status": "submitted", "terminal": False, "reconciled": True,
        "has_open_orders": True, "recovery": "wait"}


@pytest.mark.parametrize("state", ["rejected", "canceled"])
def test_confirmed_zero_fill_terminals(runtime, state):
    ids = execution(runtime)
    runtime[1].execute("UPDATE orders SET state=?,filled_size=0", (state,))
    runtime[1].execute("UPDATE executor_runs SET state=?", (state,))
    result = runtime[0].execution_status(ids)
    assert result["status"] == state and result["terminal"] is True


@pytest.mark.parametrize("state,filled", [("open", 0), ("partially_filled", .5), ("lost", 0)])
def test_unlisted_sibling_prevents_filled(runtime, state, filled):
    ids = execution(runtime)
    order(runtime, {**ids, "order_id": "sibling"}, state=state, filled=filled)
    unknown(runtime[0], {"order_id": ids["order_id"]})


def test_unrelated_filled_order_is_not_evidence(runtime):
    ids, other = execution(runtime), execution(runtime, "2")
    runtime[1].execute("UPDATE orders SET state='open',filled_size=0,terminal_at=NULL WHERE order_id=?", (ids["order_id"],))
    runtime[1].execute("UPDATE executor_runs SET state='working',terminal_at=NULL WHERE executor_id=?", (ids["executor_id"],))
    assert runtime[0].execution_status(ids)["terminal"] is False
    unknown(runtime[0], {**ids, "orders": [{"order_id": other["order_id"]}]})


def test_missing_run_and_missing_linkage_fail_closed(runtime):
    ids = execution(runtime)
    runtime[1].execute("DELETE FROM executor_runs")
    unknown(runtime[0], {"order_id": ids["order_id"]})
    runtime[1].execute("UPDATE orders SET executor_id=NULL,intent_id=NULL,plan_id=NULL")
    order(runtime, {"order_id": "unrelated", "executor_id": None, "intent_id": None, "plan_id": None})
    unknown(runtime[0], {"order_ids": [ids["order_id"], "unrelated"]})


def test_plan_sibling_without_executor_or_intent_prevents_false_fill(runtime):
    ids = execution(runtime)
    runtime[1].execute("DELETE FROM executor_runs")
    runtime[1].execute("UPDATE orders SET executor_id=NULL,intent_id=NULL")
    order(runtime, {**ids, "order_id": "sibling", "executor_id": None, "intent_id": None},
          state="open", filled=0)
    unknown(runtime[0], {"order_id": ids["order_id"]})


@pytest.mark.parametrize("key", ["order_id", "intent_id", "executor_id", "plan_id", "session_id"])
def test_absent_id_cannot_borrow_an_unrelated_execution(runtime, key):
    execution(runtime)
    unknown(runtime[0], {key: "absent", "status": "filled"})


@pytest.mark.parametrize("receipt", [None, [], "o1", {}, {"order_id": []},
    {"order_id": "o1", "intent": {"order_id": "o2"}},
    {"order_id": "o1", "approval_id": "approval"},
    {"order_id": "o1", "orders": [1]}, {"order_id": "o1", "executor": {"order_ids": [None]}}])
def test_malformed_and_approval_receipts(runtime, receipt):
    execution(runtime)
    unknown(runtime[0], receipt)


def test_accessor_uses_query_only_snapshot(runtime, monkeypatch):
    ids = execution(runtime)
    statements = []
    original = module.connect_readonly
    def observed(path):
        con = original(path)
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1
        con.set_trace_callback(statements.append)
        return con
    monkeypatch.setattr(module, "connect_readonly", observed)
    before = runtime[1].total_changes
    assert runtime[0].execution_status(ids)["terminal"]
    assert statements[0] == "BEGIN"
    assert all(s == "BEGIN" or s.startswith("SELECT ") for s in statements)
    assert runtime[1].total_changes == before


def test_concurrent_writer_cannot_mix_order_and_executor_snapshots(runtime, monkeypatch):
    ids = execution(runtime)
    original = module.connect_readonly
    changed = []
    def observed(path):
        con = original(path)
        def trace(sql):
            if "FROM executor_runs" in sql and not changed:
                changed.append(True)
                runtime[1].execute("UPDATE orders SET state='open',filled_size=0,terminal_at=NULL")
                runtime[1].execute("UPDATE executor_runs SET state='working',terminal_at=NULL")
        con.set_trace_callback(trace)
        return con
    monkeypatch.setattr(module, "connect_readonly", observed)
    assert runtime[0].execution_status(ids)["status"] == "filled"
    assert changed
    assert runtime[0].execution_status(ids)["status"] == "submitted"


@pytest.mark.parametrize("missing", ["database", "orders", "executor_runs", "capital_reservations", "approvals"])
def test_missing_database_or_tables_are_not_created(tmp_path, missing):
    config = Config(paths=WorkspacePaths(root=tmp_path), data={})
    trading = StrategyTrading(config, "mine", None, ("paper",))
    if missing != "database":
        with connect(config.paths.db) as con:
            con.execute(f"DROP TABLE {missing}")
        before = config.paths.db.read_bytes()
    unknown(trading, {"order_id": "o1"})
    if missing == "database":
        assert not config.paths.db.exists()
    else:
        assert config.paths.db.read_bytes() == before
        with sqlite3.connect(config.paths.db) as con:
            assert con.execute("SELECT name FROM sqlite_master WHERE name=?", (missing,)).fetchone() is None


def reservation(runtime, ids, *, state="reserved", strategy="mine", account="paper"):
    runtime[1].execute("""INSERT INTO capital_reservations
        (reservation_id,account_id,strategy_id,intent_id,plan_id,executor_id,market,side,
         notional_usd,state,created_at,updated_at,expires_at)
        VALUES ('r1',?,?,?,?,?,'MOCK:BTCUSDT','buy',100,?,1,2,1)""",
        (account,strategy,ids["intent_id"],ids["plan_id"],ids["executor_id"],state))


@pytest.mark.parametrize("state", ["reserved", "proposed", "unknown"])
def test_terminal_executor_does_not_override_active_reservation(runtime, state):
    ids = execution(runtime)
    reservation(runtime, ids, state=state)
    unknown(runtime[0], ids)
    assert runtime[1].execute("SELECT state FROM capital_reservations").fetchone()[0] == state


@pytest.mark.parametrize("state", ["consumed", "released", "rejected", "expired"])
def test_settled_reservation_allows_confirmed_fill(runtime, state):
    ids = execution(runtime)
    reservation(runtime, ids, state=state)
    runtime[1].execute("UPDATE orders SET reservation_id='r1'")
    runtime[1].execute("UPDATE executor_runs SET reservation_ids_json='[\"r1\"]'")
    assert runtime[0].execution_status({**ids, "reservation_id": "r1"})["status"] == "filled"
    unknown(runtime[0], {**ids, "reservation_id": "other"})


@pytest.mark.parametrize("owner", [{"strategy": "foreign"}, {"account": "other_allowed"}])
def test_reservation_owner_must_match_execution(runtime, owner):
    ids = execution(runtime)
    reservation(runtime, ids, state="consumed", **owner)
    unknown(runtime[0], ids)


def test_missing_referenced_reservation_is_unknown(runtime):
    ids = execution(runtime)
    runtime[1].execute("UPDATE executor_runs SET reservation_ids_json='[\"missing\"]'")
    unknown(runtime[0], ids)


@pytest.mark.parametrize("state", ["pending", "approved", "resuming", "resume_failed", "unknown"])
@pytest.mark.parametrize("link", ["intent_id", "resumed_intent_id", "plan_id"])
def test_unresolved_approval_blocks_terminal_without_receipt_approval_id(runtime, state, link):
    ids = execution(runtime)
    if link == "plan_id":
        payload = {"frozen_plan": {"plan_id": ids["plan_id"]}}
    else:
        payload = {link: ids["intent_id"]}
    runtime[1].execute("INSERT INTO approvals VALUES ('a1','trade_intent',?,1,1,?)",
                       (state,json.dumps(payload)))
    unknown(runtime[0], ids)
    assert runtime[1].execute("SELECT state FROM approvals").fetchone()[0] == state


def test_unrelated_pending_reservation_and_approval_do_not_block(runtime):
    ids = execution(runtime)
    other = execution(runtime, "2")
    reservation(runtime, other)
    runtime[1].execute("INSERT INTO approvals VALUES ('a2','trade_intent','pending',1,1,?)",
                       (json.dumps({"intent": {"intent_id": other["intent_id"]}}),))
    assert runtime[0].execution_status(ids)["status"] == "filled"


def test_missing_authority_tables_block_existing_filled_execution(runtime):
    ids = execution(runtime)
    runtime[1].execute("DROP TABLE approvals")
    unknown(runtime[0], ids)
