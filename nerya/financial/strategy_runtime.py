"""Producer-owned identity and deadlines for ordinary script strategy ticks."""
from copy import deepcopy
import json
import time

from .contracts import FinancialContext, FinancialError
from .task_binding import strategy_security_revision
from ..core.config import Config
from ..db.sqlite import connect


def bind_run(config, *, strategy_id, run_id, mode, session_id=None, max_run_seconds=60):
    if mode != "live" or config.get("agent.native.plan_only", False):
        return config, FinancialContext("strategy:" + strategy_id,frozenset({"read:funds","read:accounts"}),strategy_run_id=run_id, task_kind="strategy_script",
                                        task_id=strategy_id, session_id=session_id, plan_only=True)
    revision = strategy_security_revision(config, strategy_id)
    if config.get("agent.native.plan_only", False):
        mode = "plan"
    actor = "strategy:" + strategy_id
    scopes = frozenset({"write:trade", "write:funds", "read:accounts","read:funds"})
    now = time.time()
    expiry = now + max(1, float(max_run_seconds or 60))
    con = connect(config.paths.db)
    try:
        old = con.execute("SELECT * FROM strategy_financial_runs WHERE run_id=?", (run_id,)).fetchone()
        if old is not None:
            if old["strategy_id"] != strategy_id or old["security_revision"] != revision or old["state"] != "running":
                raise FinancialError("strategy_financial_run_conflict")
        else:
            con.execute("""INSERT INTO strategy_financial_runs VALUES (?,?,?,?,?,'running',?,?,?,?,?)""",
                        (run_id, strategy_id, actor, revision, mode, expiry, session_id, json.dumps(sorted(scopes)), now, now))
    finally:
        con.close()
    data = deepcopy(config.data)
    data.setdefault("runtime", {})["strategy_financial_run_id"] = run_id
    bound = Config(config.paths, data)
    context = FinancialContext(actor, scopes, task_kind="strategy_script", task_id=strategy_id, security_revision=revision, session_id=session_id,
                               strategy_run_id=run_id,
                               plan_only=mode != "live" or bool(config.get("agent.native.plan_only", False)))
    return bound, context


def validate_context(config, context, *, preparing=False):
    con = connect(config.paths.db)
    try:
        row = con.execute("SELECT * FROM strategy_financial_runs WHERE run_id=?", (context.strategy_run_id,)).fetchone()
    finally:
        con.close()
    allowed = {"running"} if preparing else {"running", "awaiting_approval"}
    if (row is None or row["actor_id"] != context.actor_id or row["state"] not in allowed
            or row["lease_until"] <= time.time() or row["mode"] != "live"):
        raise FinancialError("strategy_financial_run_not_active", 403)
    if (row["security_revision"] != context.security_revision
            or strategy_security_revision(config, row["strategy_id"]) != context.security_revision):
        raise FinancialError("financial_task_security_revision_changed", 403)
    if config.get("runtime.task_run_id"):
        from .contracts import context_from_config
        parent_data = deepcopy(config.data)
        parent_data["runtime"].pop("strategy_financial_run_id", None)
        context_from_config(Config(config.paths, parent_data))


def finish_run(config, run_id):
    con = connect(config.paths.db)
    try:
        pending = con.execute("SELECT MAX(json_extract(quote_json,'$.expires_at')) FROM financial_actions "
                              "WHERE strategy_run_id=? AND state='awaiting_approval'", (run_id,)).fetchone()[0]
        now = time.time()
        con.execute("UPDATE strategy_financial_runs SET state=?,lease_until=?,updated_at=? WHERE run_id=?",
                    ("awaiting_approval" if pending and pending > now else "finished", pending or now, now, run_id))
    finally:
        con.close()
