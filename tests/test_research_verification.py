"""One offline native-producer -> durable delivery -> stale readback flow."""
from contextlib import closing
from copy import deepcopy
import json
import time

import pytest

from nerya.agent.tool_projection import project_tool_results
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.data_api.aave import aave_specs, METADATA_QUERY
from nerya.data_api.registry import DataApiRegistry
from nerya.db.sqlite import connect
from nerya.llm.tool_compaction import compact_tool_result
from nerya.strategies.tool_verification import current_command_delivery_receipts
from nerya.skills.registry import SkillRegistry
from nerya.tools.native.skill import SkillIndex
from nerya.task_delivery import start_delivery, read_delivery, update_delivery, finalize_delivery, completion_is_current
from nerya.tools.native.data_api import data_api_handler
from nerya.tools.native.file_ops import write_file_handler
from nerya.tools.native.research_verification import research_verify_handler, research_source_versions
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


def test_native_capture_report_delivery_and_reopened_invalidation(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("network/model forbidden")
    monkeypatch.setenv("NERYA_SQLITE_JOURNAL_MODE", "delete")
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("nerya.llm.gateway.LLMGateway.call_messages", forbidden)
    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    with closing(connect(config.paths.db)) as con, con:
        con.execute("INSERT INTO agent_commands(command_id,session_id,actor_id,kind,fingerprint,request_json,"
            "context_json,state,turn_id,position,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("command", "session", "owner", "send", "command", "{}", "{}", "running",
             "turn", 1, time.time() - 30, time.time()))
    config.data.setdefault("runtime", {}).update(command_id="command",
        command_session_id="session", command_turn_id="turn")
    stages = [dict(id="research", state="completed", required_artifacts=["evidence", "report"],
        required_checks=["source_coverage", "freshness", "report_readback"])]
    index = SkillIndex([], registry_provider=SkillRegistry.load_builtin)
    contract = index.delivery_contract(ToolCall(name="delivery_start"), "research", "research_brief")
    start_delivery(config.paths, command_id="command", session_id="session", skill_id="research",
        workflow_id="research_brief", requested_scope={"summary": "Historical Aave research; no trading"},
        stages=stages, workflow_contract=contract)
    pid = "11111111-2222-4333-8444-555555555555"
    market, token, atoken = ("0x" + c * 40 for c in ("1", "2", "3"))

    class FakeHTTP:
        def request(self, method, url, **kwargs):
            if method == "GET":
                return 200, {"status": "success", "data": [dict(config_id=pid, pool=atoken + "-reserve",
                    token=atoken, project="aave-v3", chain="Ethereum", symbol="USDX", underlyingTokens=[token])]}
            if kwargs["body"]["query"] == METADATA_QUERY:
                return 200, {"data": {"markets": [dict(name="Market", address=market,
                    chain=dict(name="Ethereum", chainId=1), reserves=[dict(
                        underlyingToken=dict(address=token, symbol="USDX"), aToken=dict(address=atoken))])]}}
            return 200, {"data": {"supplyAPYHistory": [dict(date="2025-02-03T00:00:00Z",
                avgRate=dict(value="0.0325"))]}}

    registry = DataApiRegistry()
    for spec in aave_specs(FakeHTTP()):
        registry.register_action(spec)
    data_call = ToolCall(id="capture", name="data_api", arguments=dict(op="call", provider="aave",
        action="supply_apy_history", args=dict(pool_id=pid, chain_id=1, market=market, underlying_token=token,
        start="2025-02-03", end="2025-02-04", window="LAST_YEAR")))
    captured = data_api_handler(data_call, config_like=config, registry=registry)
    assert not captured.is_error, captured.text()

    def persist(call, result, compact=False):
        def render(value):
            kept = compact_tool_result(call.name, tool_json_data(value), size_threshold=0).kept if compact else None
            return {"content": "[compacted_kept]\n" + json.dumps(kept) if compact else value.text()}
        block = project_tool_results([result], render_tool_result=render,
            rendered_tool_result_text=lambda rendered: rendered["content"]).event_blocks[0]
        block.update(kind="tool.complete", session_id="session", turn_id="turn", payload=call.arguments)
        with closing(connect(config.paths.db)) as con, con:
            con.execute("INSERT INTO agent_command_events(command_id,event_id,payload_json) VALUES (?,?,?)",
                ("command", call.id, json.dumps(block)))
        return block

    persist(data_call, captured, compact=True)
    report = "artifacts/conversations/session/report.md"
    write = ToolCall(id="write", name="write_file", arguments=dict(path=str(tmp_path / report),
        contents="# Historical APY brief\nResearch only, no trading.\n" +
        "\n".join(s["url"] for s in tool_json_data(captured)["_envelope"]["sources"])))
    written = write_file_handler(write, root=tmp_path, file_state=None, session_id="session")
    assert not written.is_error, written.text()
    write_event = persist(write, written)
    assert write_event["ok"] and write_event["result"] == written.text()
    assert write_event["result"].startswith("--- /dev/null\n+++ b/")
    call = ToolCall(id="verify", name="research_verify", arguments=dict(report_path=report))
    verified = research_verify_handler(call, config=config)
    assert not verified.is_error, verified.text()
    receipt = verified.metadata["verification"]
    assert verified.semantic_success and all(c["status"] == "passed" for c in receipt["checks"])
    assert receipt["artifact_id"] == "research:command"
    assert "point_in_time_replay" in receipt["unverified"]
    artifacts = tool_json_data(verified)["artifacts"]
    manifest = json.loads((tmp_path / tool_json_data(verified)["manifest_path"]).read_bytes())
    assert [e["call_id"] for e in manifest["evidence"]] == [data_call.id]
    assert len(manifest["evidence"][0]["sources"]) == 3
    assert all(set(s) == {"url", "sha256", "retrieved_at"} for s in manifest["evidence"][0]["sources"])
    assert research_verify_handler(call, config=config).metadata["verification"] == receipt
    assert research_verify_handler(ToolCall(name="research_verify", arguments=dict(
        report_path=report, evidence_call_ids=[data_call.id])), config=config).metadata["verification"] == receipt
    persist(call, verified)
    receipts, versions = current_command_delivery_receipts(config, "command", "session")
    assert versions["research:command"] == receipt["revision"]
    current = read_delivery(config.paths, command_id="command", session_id="session")
    saved = update_delivery(config.paths, command_id="command", session_id="session",
        expected_revision=current["delivery_revision"], artifacts=artifacts, producer_receipts=receipts, source_versions=versions)
    done = finalize_delivery(config.paths, command_id="command", session_id="session",
        expected_revision=saved["delivery_revision"], producer_receipts=receipts, source_versions=versions)
    assert done["completion"]["allowed"], done["completion"]
    reopened = Config(paths=config.paths)
    assert completion_is_current(reopened.paths, done)
    (tmp_path / report).write_text("Changed after verification\n")
    assert research_source_versions(reopened, done) == {"research:command": "research_stale"}
    assert not completion_is_current(reopened.paths, done)
    config.data["runtime"]["command_turn_id"] = "foreign-turn"
    assert research_verify_handler(call, config=config).is_error
