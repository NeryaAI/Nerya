"""The model interpretation cannot replace the admitted operator request."""
import json

import pytest

from test_task_delivery import _runtime, _command, _stages
from nerya.task_delivery import start_delivery, read_delivery

pytestmark=pytest.mark.smoke


def test_delivery_binds_original_request_not_model_proxy_permission(tmp_path):
    config,runtime=_runtime(tmp_path)
    command=_command(runtime)
    original="Jupiter SOL/USDC, last 30 days, no trading"
    with runtime.store.transaction() as con:
        request=json.loads(con.execute("SELECT request_json FROM agent_commands WHERE command_id=?",(command["command_id"],)).fetchone()[0])
        request["payload"]["text"]=original
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id=?",(json.dumps(request),command["command_id"]))
    result=start_delivery(config.paths,command_id=command["command_id"],session_id=command["session_id"],
        skill_id="research",workflow_id="research_brief",requested_scope={"summary":"Use another venue as a proxy",
        "limitations":["proxy permitted"]},stages=_stages())
    assert result["requested_scope"]["summary"]==original
    assert result["requested_scope"]["model_interpretation"]=="Use another venue as a proxy"
    assert result["requested_scope"]["authority"]=="admitted_request"
    assert read_delivery(config.paths,command_id=command["command_id"],session_id=command["session_id"])["requested_scope"]==result["requested_scope"]
