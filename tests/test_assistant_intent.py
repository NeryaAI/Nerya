"""Actual SQLite isolation with fake classification, not model accuracy tests."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from nerya.agent.assistant_intent import (
    INTENT_SCHEMA, AssistantIntentError, assistant_request_digest, prepare_assistant_intent,
)
from nerya.agent.assistant_work import AssistantWork
from nerya.agent.assistants import AssistantStore, assistant_config
from nerya.agent.command_store import CommandStore, encode
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.db.sqlite import connect_readonly
from nerya.harness.cancellation import CancelToken
from nerya.llm.adapters._base import UrllibTransport
from nerya.llm.capability_matrix import capability_of
from nerya.llm.gateway import LLMGateway
from nerya.llm.messages import MessagesResponse
from nerya.llm.route_candidates import configured_routes

pytestmark = pytest.mark.smoke
_REAL_CALL_MESSAGES = LLMGateway.call_messages


def intent(disposition="task_create", **fields):
    return {"disposition": disposition, "title": "Requested work", "target_task_id": None,
            "language": "en", "clarification": "", **fields}


class Gateway:
    def __init__(self, value=None, *, raw=None, effect=None, reasoning=None):
        self.value = intent() if value is None else value
        self.raw, self.effect, self.calls = raw, effect, []
        self.reasoning = reasoning

    def call_messages(self, **kwargs):
        self.calls.append({**deepcopy({key: value for key, value in kwargs.items() if key != "cancel_token"}),
                           "cancel_token": kwargs.get("cancel_token")})
        if self.effect:
            self.effect()
        response = MessagesResponse(
            content=[{"type": "text", "text": self.raw if self.raw is not None else json.dumps(self.value)}],
            stop_reason="end_turn", provider=kwargs["model_provider"], model=kwargs["model_id"])
        if self.reasoning is not None:
            response.content.append(deepcopy(self.reasoning))
        return response


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    config.data["llm"]["tiers"] = {
        "light": {"provider": "openai", "model": "cheap-unused", "allowed_tasks": ["classify"]},
        "medium": {"provider": "openai", "model": "selected-model", "allowed_tasks": ["agent.loop"]},
    }
    assistants = AssistantStore(config.paths)
    sid = assistants.home("main")["session_id"]
    config = assistant_config(config, assistants.get("main"), session_id=sid)
    store = CommandStore(config.paths, "intent-test")
    AssistantWork(config.paths)  # Fixture owns lazy schema creation.
    monkeypatch.setattr(LLMGateway, "call", lambda *a, **kw: pytest.fail("Unexpected real model call"))
    monkeypatch.setattr(LLMGateway, "call_messages", lambda *a, **kw: pytest.fail("Unexpected real messages call"))
    return SimpleNamespace(config=config, assistants=assistants, sid=sid, store=store)


def admit(env, *, cid="command-intent-1", text="  Make a BTC report. Preserve this text.  ", **fields):
    payload = {"session_id": env.sid, "payload": {"text": text}, "_auth_actor_id": "owner",
               "_auth_scope": "execute:strategy", "_auth_scopes": ["execute:strategy"], **fields}
    env.store.accept(cid=cid, sid=env.sid, actor=payload["_auth_actor_id"], kind="send",
                     request=payload, context={"untouched": {"keep": True}}, turn_id="turn-" + cid)
    row = env.store.claim(env.sid, "worker-intent")
    assert row is not None
    payload["turn_id"] = row["turn_id"]
    env.config.data["runtime"].update(command_id=cid, command_session_id=env.sid,
                                      command_turn_id=row["turn_id"], financial_actor_id=payload["_auth_actor_id"])
    return payload


def record(env):
    with closing(connect_readonly(env.config.paths.db)) as con:
        row = con.execute("SELECT * FROM agent_commands WHERE command_id='command-intent-1'").fetchone()
    return dict(row)


def context(env):
    return json.loads(record(env)["context_json"])


def edit_request(env, payload):
    with env.store.transaction() as con:
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='command-intent-1'", (encode(payload),))


def task(env, name="one", *, parent=None, actor="owner", title=None, created_at=1):
    parent, child, tid = parent or env.sid, "taskchat-" + name, "awork_" + name
    env.assistants.bind(child, "main", kind="task")
    with env.store.transaction() as con:
        con.execute("INSERT INTO assistant_work VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, "main", parent, child, title or name, "general", "fingerprint",
                     "taskcmd-" + name, "{}", actor, created_at))
        con.execute("INSERT INTO agent_commands(command_id,session_id,actor_id,kind,fingerprint,request_json,context_json,"
                    "state,turn_id,position,created_at,updated_at,result_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("taskcmd-" + name, child, actor, "send", "fingerprint", "{}", "{}", "succeeded",
                     "taskturn-" + name, 1, 1, 1, encode({"final_text": "SECRET_OLD_MODEL_ANSWER"})))
    return tid


def prepare(env, payload, gateway=None):
    return prepare_assistant_intent(env.config, payload, gateway=gateway or Gateway())


def test_non_coordinator_does_not_open_a_database(tmp_path):
    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    assert prepare_assistant_intent(config, {}) is None
    assert not config.paths.db.exists()


def test_contract_persists_reuses_and_never_rewrites_input(env):
    payload = admit(env, payload={"text": "  原文，请保留。  ", "attachments": [{"id": "f1", "reference": "asset://original"}]})
    before, config_before, gateway = deepcopy(payload), deepcopy(env.config.data), Gateway()
    result = prepare(env, payload, gateway)
    assert result["status"] == "ready" and result["disposition"] == "task_create"
    assert result["protocol"] == "nerya.assistant_request.v1" and result["origin"] == "semantic"
    assert result["binding"] == {"command_id": "command-intent-1", "session_id": env.sid,
                                  "turn_id": payload["turn_id"], "actor_id": "owner",
                                  "input_digest": assistant_request_digest(payload)}
    assert payload == before and env.config.data == config_before
    assert context(env)["assistant_request"] == result and context(env)["untouched"] == {"keep": True}
    persisted = record(env)
    result["title"] = "Mutating a returned object cannot mutate the DB"
    assert prepare(env, payload, gateway) == context(env)["assistant_request"]
    assert len(gateway.calls) == 1 and record(env) == persisted
    sent = json.loads(gateway.calls[0]["messages"][0]["content"].split("Input: ")[1])
    assert sent["text"] == payload["payload"]["text"] and sent["attachments"] == payload["payload"]["attachments"]
    assert assistant_request_digest(json.loads(persisted["request_json"])) == assistant_request_digest(payload)
    with closing(connect_readonly(env.config.paths.db)) as con:
        assert con.execute("SELECT COUNT(*) FROM assistant_work").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM agent_commands").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["text", "attachments", "work_mode", "assistant_intent", "target"])
def test_edited_source_never_reuses_or_overwrites_contract(env, change):
    payload = admit(env)
    gateway = Gateway()
    original = prepare(env, payload, gateway)
    edited = deepcopy(payload)
    if change == "text":
        edited["payload"]["text"] += " "
    elif change == "attachments":
        edited["payload"]["attachments"] = [{"id": "changed", "content": "different"}]
    elif change == "work_mode":
        edited[change] = "plan"
    else:
        edited["assistant_intent"] = {"disposition": "conversation"} if change == "assistant_intent" else {
            "disposition": "task_read", "target_task_id": "awork_other"}
    assert assistant_request_digest(edited) != assistant_request_digest(payload)
    edit_request(env, edited)
    with pytest.raises(AssistantIntentError):
        prepare(env, edited, gateway)
    assert context(env)["assistant_request"] == original and len(gateway.calls) == 1


def test_digest_is_transport_independent_but_attachment_content_sensitive():
    payload = {"payload": {"text": "X", "attachments": [{"id": "one", "content": "a"}]}}
    same = deepcopy(payload)
    same.update(command_id="anything", turn_id="any-turn", _auth_actor_id="owner", model_id="new-default")
    assert assistant_request_digest(payload) == assistant_request_digest(same)
    same["payload"]["attachments"][0]["content"] = "b"
    assert assistant_request_digest(payload) != assistant_request_digest(same)


@pytest.mark.parametrize("place,key", [
    ("config", "command_id"), ("config", "command_session_id"), ("config", "command_turn_id"),
    ("config", "financial_actor_id"), ("payload", "_auth_actor_id"), ("payload", "session_id"),
    ("payload", "turn_id"), ("payload", "command_id"), ("durable", "_auth_actor_id"),
])
def test_binding_mismatch_prevents_classification_and_publication(env, place, key):
    payload = admit(env)
    if place == "config":
        env.config.data["runtime"][key] = "other"
    elif place == "payload":
        payload[key] = "other"
    else:
        edit_request(env, {**payload, key: "other"})
    gateway = Gateway()
    with pytest.raises(AssistantIntentError, match="assistant_intent_unbound"):
        prepare(env, payload, gateway)
    assert not gateway.calls and "assistant_request" not in context(env)


@pytest.mark.parametrize("field,value", [
    ("disposition", "launch"), ("disposition", "task_followup"), ("language", "fr"),
    ("title", 3), ("title", " "), ("title", "x" * 161), ("target_task_id", []),
    ("target_task_id", "awork_invented"), ("clarification", None),
    ("clarification", "Completed!"), ("extra", "unknown"),
])
def test_strict_schema_fails_closed(env, field, value):
    output = intent(**{field: value})
    result = prepare(env, admit(env), Gateway(output))
    assert result["disposition"] == "clarify" and result["status"] == "clarify"
    assert context(env)["assistant_request"] == result


@pytest.mark.parametrize("raw", ["[]", "null", "not JSON", "{}", "Done! {}",
    "```json\n" + json.dumps(intent()) + "\n```", json.dumps(intent()) + " {}",
    '{"disposition":"conversation","disposition":"task_create","title":"a","target_task_id":null,"language":"en","clarification":""}',
])
@pytest.mark.parametrize("reasoning", [None, {"type": "thinking", "thinking": json.dumps(intent())}])
def test_raw_output_is_strict_json(env, raw, reasoning):
    gateway, payload = Gateway(raw=raw, reasoning=reasoning), admit(env)
    result = prepare(env, payload, gateway)
    assert result["status"] == "clarify" and len(gateway.calls) == 2
    assert prepare(env, payload, gateway) == result and len(gateway.calls) == 2


@pytest.mark.parametrize("raw", [
    "private-model-prose {}", '```json\n' + json.dumps(intent()) + '\n```',
    '{"disposition":', json.dumps({"disposition": "conversation"}),
])
def test_format_repair_preserves_source_model_scope_and_binding(env, raw, caplog):
    tid = task(env)
    payload = admit(env, text="什么是复利？一句话解释。",
                    model_provider="agnes-ai", model_id="agnes-3.0-flash")
    before, config_before = deepcopy(payload), deepcopy(env.config.data)
    persisted = record(env)

    class Repair(Gateway):
        def call_messages(self, **kwargs):
            assert record(env) == persisted
            response = super().call_messages(**kwargs)
            if len(self.calls) == 1:
                response.content[0]["text"] = raw
            return response

    gateway = Repair(intent("conversation", language="zh"),
                     reasoning={"type": "thinking", "thinking": "private-reasoning-secret"})
    result = prepare(env, payload, gateway)
    assert result["status"] == "ready" and result["disposition"] == "conversation"
    assert result["binding"]["input_digest"] == assistant_request_digest(before)
    assert payload == before and env.config.data == config_before
    assert len(gateway.calls) == 2
    first, second = gateway.calls
    assert first["model_provider"] == second["model_provider"] == "agnes-ai"
    assert first["model_id"] == second["model_id"] == "agnes-3.0-flash"
    assert first["messages"] == second["messages"]
    assert first["tools"] == second["tools"] == []
    assert first["system"] != second["system"]
    assert "Return a single valid JSON object, no markdown." in second["system"]
    sent = json.loads(second["messages"][0]["content"].split("Input: ")[1])
    assert sent["text"] == before["payload"]["text"]
    assert [row["task_id"] for row in sent["tasks"]] == [tid]
    assert "private-model-prose" not in encode(second) + caplog.text
    assert "private-reasoning-secret" not in encode(second) + caplog.text + encode(result)
    assert "SECRET_OLD_MODEL_ANSWER" not in encode(second)
    assert "reason=invalid_" in caplog.text and "error_type=" in caplog.text
    assert prepare(env, payload, gateway) == result and len(gateway.calls) == 2
    with closing(connect_readonly(env.config.paths.db)) as con:
        assert con.execute("SELECT COUNT(*) FROM assistant_work").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM agent_commands").fetchone()[0] == 2


@pytest.mark.parametrize("failure", ["transport", "cancel", "tool", "truncated", "provider", "model", "target"])
@pytest.mark.parametrize("after_format_error", [False, True])
def test_nonformat_failures_never_retry(env, failure, after_format_error, caplog):
    payload, token = admit(env), CancelToken()
    payload["_task_cancel_token"] = token

    class Failure(Gateway):
        def call_messages(self, **kwargs):
            response = super().call_messages(**kwargs)
            assert kwargs["cancel_token"] is token
            if after_format_error and len(self.calls) == 1:
                response.content[0]["text"] = "private-format-prose"
                return response
            if failure == "transport":
                raise RuntimeError("private-transport-secret")
            if failure == "cancel":
                token.cancel("private-cancel-reason")
            elif failure == "tool":
                response.content.append({"type": "tool_use", "name": "create_task"})
            elif failure == "truncated":
                response.stop_reason = "max_tokens"
            elif failure == "provider":
                response.provider = "other-provider"
            elif failure == "model":
                response.model = "other-model"
            elif failure == "target":
                response.content[0]["text"] = json.dumps(intent("task_read", target_task_id="awork_invented"))
            if failure != "target":
                response.content[0]["text"] = "private-format-prose"
            return response

    gateway = Failure(reasoning={"type": "thinking", "thinking": json.dumps(intent())})
    if failure == "cancel":
        with pytest.raises(AssistantIntentError):
            prepare(env, payload, gateway)
        assert "assistant_request" not in context(env)
    else:
        result = prepare(env, payload, gateway)
        assert result["status"] == "clarify"
        assert prepare(env, payload, gateway) == result
    assert len(gateway.calls) == (2 if after_format_error else 1)
    assert "private-" not in caplog.text
    reason = {"transport": "transport_error", "cancel": "cancelled",
              "tool": "nontext_response", "truncated": "incomplete_response",
              "provider": "model_mismatch", "model": "model_mismatch",
              "target": "invalid_semantics"}[failure]
    assert f"reason={reason}" in caplog.text


@pytest.mark.parametrize("stop_reason", ["tool_use", "max_tokens", "stop_sequence", "", None])
def test_only_completed_messages_can_publish_action(env, stop_reason):
    class Incomplete(Gateway):
        def call_messages(self, **kwargs):
            response = super().call_messages(**kwargs)
            response.stop_reason = stop_reason
            return response
    gateway = Incomplete(reasoning={"type": "thinking", "thinking": json.dumps(intent())})
    assert prepare(env, admit(env), gateway)["status"] == "clarify"
    assert len(gateway.calls) == 1


@pytest.mark.parametrize("block", [
    {"type": "tool_use", "id": "unexpected", "name": "create_task", "input": {}},
    {"type": "thinking"}, {"type": "thinking", "thinking": None},
    {"type": "thinking", "thinking": {}}, {"type": "thinking", "text": json.dumps(intent())},
    {"type": "redacted_thinking"}, {"type": "redacted_thinking", "data": []},
    {"type": "reasoning", "text": json.dumps(intent())},
    {"type": "text_delta", "text": json.dumps(intent())},
    {"type": "thinking_delta", "thinking": "private reasoning"},
    {"type": "tool_result", "content": json.dumps(intent())},
    {"type": "image"}, {"type": "text", "text": None}, {"type": "text"}, "invalid block",
])
def test_nontext_messages_cannot_publish_action_even_with_valid_json(env, block):
    class Nontext(Gateway):
        def call_messages(self, **kwargs):
            response = super().call_messages(**kwargs)
            response.content.append(block)
            return response
    gateway = Nontext()
    assert prepare(env, admit(env), gateway)["status"] == "clarify"
    assert len(gateway.calls) == 1


@pytest.mark.parametrize("reasoning", [
    {"type": "thinking", "thinking": json.dumps(intent("conversation")), "signature": "opaque"},
    {"type": "redacted_thinking", "data": "opaque"},
])
def test_reasoning_metadata_never_supplies_classification_or_changes_source(env, reasoning):
    payload = admit(env, payload={"text": "  原文，请保留。\n",
                                  "attachments": [{"id": "f1", "reference": "asset://original"}]})
    before, config_before, persisted = deepcopy(payload), deepcopy(env.config.data), record(env)
    gateway = Gateway(reasoning=reasoning)
    result = prepare(env, payload, gateway)
    assert result["status"] == "ready" and result["disposition"] == "task_create"
    assert result["binding"]["input_digest"] == assistant_request_digest(before)
    assert payload == before and env.config.data == config_before
    assert record(env)["request_json"] == persisted["request_json"]
    sent = json.loads(gateway.calls[0]["messages"][0]["content"].split("Input: ")[1])
    assert sent["text"] == before["payload"]["text"]
    assert sent["attachments"] == before["payload"]["attachments"]
    assert prepare(env, payload, gateway) == result and len(gateway.calls) == 1


@pytest.mark.parametrize("content", [
    [], None, {}, [{"type": "thinking", "thinking": json.dumps(intent())}],
    [{"type": "redacted_thinking", "data": json.dumps(intent())}],
])
def test_missing_text_fails_closed_without_repair(env, content):
    class MissingText(Gateway):
        def call_messages(self, **kwargs):
            response = super().call_messages(**kwargs)
            response.content = content
            return response
    gateway = MissingText()
    assert prepare(env, admit(env), gateway)["status"] == "clarify"
    assert len(gateway.calls) == 1


def test_split_text_blocks_are_joined_before_strict_validation(env):
    class SplitText(Gateway):
        def call_messages(self, **kwargs):
            response = super().call_messages(**kwargs)
            raw = response.content[0]["text"]
            response.content = [{"type": "text", "text": raw[:17]}, {"type": "text", "text": raw[17:]}]
            response.raw = {"disposition": "invalid"}
            response.content.insert(1, {"type": "thinking", "thinking": json.dumps(intent("conversation"))})
            return response
    assert prepare(env, admit(env), SplitText())["status"] == "ready"


def test_scoped_context_contains_only_ids_titles_states_and_creation_times(env):
    own = task(env, title="Owned title")
    task(env, "other-actor", actor="intruder")
    env.assistants.bind("other-parent", "main")
    task(env, "other-session", parent="other-parent")
    task(env, "deleted")
    with env.store.transaction() as con:
        con.execute("INSERT INTO agent_deleted_sessions VALUES (?,0)", ("taskchat-deleted",))
    gateway = Gateway(intent("task_read", target_task_id=own))
    assert prepare(env, admit(env), gateway)["target_task_id"] == own
    sent = gateway.calls[0]["messages"][0]["content"]
    assert "SECRET_OLD_MODEL_ANSWER" not in sent and "intruder" not in sent
    assert json.loads(sent.split("Input: ")[1])["tasks"] == [
        {"task_id": own, "title": "Owned title", "state": "succeeded", "created_at": 1}]


@pytest.mark.parametrize('text', [
    '继续最新一个 ETH/USDT 均线+Agent确认策略回测任务，不启用交易。',
    'Continue the latest ETH/USDT moving-average strategy with Agent confirmation; no trading.',
])
def test_explicit_latest_context_has_authoritative_task_creation_times(env, text):
    title = 'ETH/USDT 均线+Agent确认策略'
    older = task(env, 'z_old', title=title, created_at=1792900000)
    newest = task(env, 'a_new', title=title, created_at=1792900060)
    task(env, 'foreign_latest', title=title, actor='intruder', created_at=1792900999)
    # Updating the older task's command is not creation of a newer task.
    with env.store.transaction() as con:
        con.execute("UPDATE agent_commands SET created_at=?,updated_at=? WHERE command_id=?",
                    (1792901000, 1792901000, 'taskcmd-z_old'))
    gateway = Gateway(intent('task_follow_up', target_task_id=newest, language='zh'))
    payload = admit(env, text=text)
    result = prepare(env, payload, gateway)
    sent = json.loads(gateway.calls[0]['messages'][0]['content'].split('Input: ')[1])
    assert [(row['task_id'], row['created_at']) for row in sent['tasks']] == [
        (newest, 1792900060), (older, 1792900000)]
    prompt = gateway.calls[0]['system']
    assert 'created_at' in prompt and 'Unix seconds' in prompt
    assert 'explicitly' in prompt and 'latest' in prompt and 'ties' in prompt
    assert result['target_task_id'] == newest and result['status'] == 'ready'
    assert sent['text'] == text


def test_exact_title_followup_keeps_selected_task_even_when_other_task_is_newer(env):
    title = 'ETH/USDT 均线+Agent确认策略回测'
    selected = task(env, 'exact_title', title=title, created_at=100)
    task(env, 'newer_other', title='ETH/USDT 风险报告', created_at=200)
    text = '继续任务“ETH/USDT 均线+Agent确认策略回测”，完成策略和30天回测，不启用交易。'
    payload = admit(env, text=text)
    gateway = Gateway(intent('task_follow_up', target_task_id=selected, language='zh'))
    result = prepare(env, payload, gateway)
    assert result['status'] == 'ready' and result['target_task_id'] == selected
    assert result['disposition'] == 'task_follow_up' and 'reason' not in result
    assert prepare(env, payload, gateway) == result and len(gateway.calls) == 1
    assert json.loads(record(env)['request_json'])['payload']['text'] == text


@pytest.mark.parametrize('same_time', [False, True])
def test_duplicate_targets_never_get_an_automatic_latest_fallback(env, same_time):
    task(env, 'old', title='Same task', created_at=100)
    task(env, 'new', title='Same task', created_at=100 if same_time else 200)
    text = 'Continue the latest task' if same_time else 'Continue that task'
    gateway = Gateway(intent('clarify', clarification='Which one?'))
    result = prepare(env, admit(env, text=text), gateway)
    assert result['disposition'] == 'clarify' and result['target_task_id'] is None
    assert result['reason'] == 'ambiguous_intent_or_target'
    assert prepare(env, json.loads(record(env)['request_json']), gateway) == result
    assert len(gateway.calls) == 1


@pytest.mark.parametrize('failure,reason,calls', [
    ('format', 'invalid_json', 2), ('transport', 'transport_error', 1),
    ('scope', 'invalid_semantics', 1),
])
def test_classifier_failures_are_distinct_from_true_ambiguity(env, failure, reason, calls, caplog):
    task(env, 'one', title='Same task', created_at=100)
    task(env, 'two', title='Same task', created_at=200)
    def fail():
        raise RuntimeError('private-provider-diagnostic')
    gateway = (Gateway(raw='private-invalid-json') if failure == 'format' else
               Gateway(effect=fail) if failure == 'transport' else
               Gateway(intent('task_follow_up', target_task_id='awork_foreign')))
    payload = admit(env, text='继续最新任务')
    result = prepare(env, payload, gateway)
    assert result['status'] == 'clarify' and result['target_task_id'] is None
    assert result['reason'] == reason
    assert '分类未完成' in result['clarification']
    assert 'private-' not in encode(result) + caplog.text
    assert 'command_id=command-intent-1' in caplog.text
    assert prepare(env, payload, gateway) == result and len(gateway.calls) == calls


def test_legacy_saved_clarify_without_reason_remains_reusable(env):
    payload, gateway = admit(env), Gateway(intent('clarify', clarification='Which task?'))
    prepare(env, payload, gateway)
    saved = context(env)
    saved['assistant_request'].pop('reason', None)
    with env.store.transaction() as con:
        con.execute("UPDATE agent_commands SET context_json=? WHERE command_id=?",
                    (encode(saved), 'command-intent-1'))
    assert prepare(env, payload, gateway) == saved['assistant_request']
    assert len(gateway.calls) == 1


def test_model_cannot_supply_host_classification_reason(env):
    gateway = Gateway(intent('clarify', clarification='Which task?', reason='transport_error'))
    result = prepare(env, admit(env), gateway)
    assert result['reason'] == 'invalid_schema' and result['status'] == 'clarify'
    assert result['target_task_id'] is None and len(gateway.calls) == 2


@pytest.mark.parametrize("disposition", ["task_read", "task_follow_up"])
@pytest.mark.parametrize("target", ["awork_missing", "awork_foreign", ["awork_one", "awork_two"]])
def test_invalid_or_multiple_targets_never_authorize_action(env, disposition, target):
    task(env)
    task(env, "two")
    task(env, "foreign", actor="intruder")
    assert prepare(env, admit(env), Gateway(intent(disposition, target_task_id=target)))["status"] == "clarify"


def test_missing_followup_target_requires_clarification_even_with_one_task(env):
    task(env)
    assert prepare(env, admit(env), Gateway(intent("task_follow_up")))["status"] == "clarify"


@pytest.mark.parametrize("explicit", [True, False])
def test_task_overview_read_needs_no_target(env, explicit):
    task(env)
    task(env, "two")
    payload = admit(env, assistant_intent={"disposition": "task_read" if explicit else "auto"})
    gateway = Gateway(intent("task_read"))
    result = prepare(env, payload, gateway)
    assert result["disposition"] == "task_read" and result["status"] == "ready"
    assert result["target_task_id"] is None and len(gateway.calls) == (0 if explicit else 1)


@pytest.mark.parametrize("disposition", ["conversation", "task_create", "task_read", "task_follow_up"])
def test_explicit_object_bypasses_llm_after_target_validation(env, disposition):
    tid = task(env)
    hint = {"disposition": disposition}
    if disposition in ("task_read", "task_follow_up"):
        hint["target_task_id"] = tid
    payload = admit(env, text="  原文" * 60, assistant_intent=hint)
    gateway = Gateway()
    result = prepare(env, payload, gateway)
    assert result["disposition"] == disposition and result["status"] == "ready"
    assert result["origin"] == "explicit" and not gateway.calls
    assert result["title"] == payload["payload"]["text"][:160] and result["language"] == "zh"


def test_nested_explicit_intent_is_not_authority(env):
    payload = admit(env, payload={"text": "hello", "assistant_intent": {"disposition": "task_create"}, "_auth_actor_id": "admin"})
    gateway = Gateway(intent("conversation"))
    result = prepare(env, payload, gateway)
    assert result["origin"] == "semantic" and result["disposition"] == "conversation" and len(gateway.calls) == 1


@pytest.mark.parametrize("value", ["task_create", {}, [], {"disposition": "clarify"},
    {"disposition": "task_create", "title": "extra"}, {"disposition": "task_follow_up", "target_task_id": []}])
def test_invalid_hint_does_not_fall_back_to_semantic_action(env, value):
    gateway = Gateway()
    assert prepare(env, admit(env, assistant_intent=value), gateway)["status"] == "clarify"
    assert not gateway.calls


def test_model_clarification_cannot_publish_completion_claim(env):
    output = intent("clarify", language="zh", title="任务已成功创建", clarification="任务已完成：虚构回执！")
    result = prepare(env, admit(env), Gateway(output))
    assert result["language"] == "zh" and result["title"] == ""
    assert "虚构" not in result["clarification"] and "已完成" not in result["clarification"]
    assert result["status"] == "clarify"


def test_gateway_failure_persists_clarify_without_exception_text(env):
    def fail():
        raise RuntimeError("private-provider-diagnostic")
    gateway, payload = Gateway(effect=fail), admit(env)
    result = prepare(env, payload, gateway)
    assert result["status"] == "clarify" and "private-provider" not in encode(result)
    assert prepare(env, payload, gateway) == result and len(gateway.calls) == 1


@pytest.mark.parametrize("state", ["queued", "stopping", "succeeded", "unconfirmed", "removed"])
def test_inactive_command_never_classifies(env, state):
    payload = admit(env)
    with env.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state=?", (state,))
    gateway = Gateway()
    with pytest.raises(AssistantIntentError):
        prepare(env, payload, gateway)
    assert not gateway.calls and "assistant_request" not in context(env)


@pytest.mark.parametrize("when", ["before", "during", "reuse"])
def test_cancellation_cannot_publish_action(env, when):
    payload, token = admit(env), CancelToken()
    payload["_task_cancel_token"] = token
    gateway = Gateway(effect=(lambda: token.cancel()) if when == "during" else None)
    if when == "reuse":
        assert prepare(env, payload, gateway)["status"] == "ready"
    if when != "during":
        token.cancel()
    with pytest.raises(AssistantIntentError):
        prepare(env, payload, gateway)
    if when == "during":
        assert gateway.calls[0]["cancel_token"] is token
    if when != "reuse":
        assert "assistant_request" not in context(env)


@pytest.mark.parametrize("change", ["stop", "input", "delete-target", "auth"])
def test_no_transaction_during_llm_and_publication_rechecks_state(env, change):
    tid, payload = task(env), admit(env)
    changed = []
    def mutate():
        with closing(sqlite3.connect(env.config.paths.db, timeout=0.05)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            if change == "stop":
                con.execute("UPDATE agent_commands SET state='stopping' WHERE command_id='command-intent-1'")
            elif change in ("input", "auth"):
                edited = deepcopy(payload)
                if change == "input":
                    edited["payload"]["text"] = "edited while model running"
                else:
                    edited["_auth_actor_id"] = "intruder"
                con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='command-intent-1'", (encode(edited),))
            else:
                con.execute("INSERT INTO agent_deleted_sessions VALUES ('taskchat-one',0)")
        changed.append(True)
    with pytest.raises(AssistantIntentError, match="assistant_intent_changed"):
        prepare(env, payload, Gateway(intent("task_follow_up", target_task_id=tid), effect=mutate))
    assert changed and "assistant_request" not in context(env)


def test_concurrent_preparation_first_contract_wins(env):
    payload, winner = admit(env), []
    gateway = Gateway(effect=lambda: winner.append(prepare(env, payload, Gateway(intent("conversation")))))
    result = prepare(env, payload, gateway)
    assert result == winner[0] == context(env)["assistant_request"] and result["disposition"] == "conversation"


def test_contract_binding_tampering_is_not_repaired_or_used(env):
    payload = admit(env)
    prepare(env, payload)
    tampered = context(env)
    tampered["assistant_request"]["binding"]["turn_id"] = "different-turn"
    with env.store.transaction() as con:
        con.execute("UPDATE agent_commands SET context_json=?", (encode(tampered),))
    gateway = Gateway()
    with pytest.raises(AssistantIntentError):
        prepare(env, payload, gateway)
    assert context(env) == tampered and not gateway.calls


def test_direct_route_without_command_fails_without_db_mutation(env):
    gateway = Gateway()
    payload = {"session_id": env.sid, "_auth_actor_id": "owner", "payload": {"text": "Create work"}}
    with pytest.raises(AssistantIntentError, match="assistant_intent_unbound"):
        prepare(env, payload, gateway)
    assert not gateway.calls
    with closing(connect_readonly(env.config.paths.db)) as con:
        assert con.execute("SELECT COUNT(*) FROM agent_commands").fetchone()[0] == 0


@pytest.mark.parametrize("override", [False, True])
def test_selected_model_not_light_intent_tier(env, override):
    env.config.data["agent"]["native"]["assistant_profile"].update(
        model_provider="anthropic", model_id="assistant-model", model_tier="medium")
    fields = {"model_provider": "openai", "model_id": "user-selected"} if override else {}
    gateway = Gateway()
    assert prepare(env, admit(env, **fields), gateway)["status"] == "ready"
    call = gateway.calls[0]
    assert (call["tier"], call["model_provider"], call["model_id"]) == (
        "medium", "openai" if override else "anthropic", "user-selected" if override else "assistant-model")
    assert json.loads(call["system"].split("\nSchema: ", 1)[1]) == INTENT_SCHEMA
    assert call["tools"] == []


@pytest.mark.parametrize("fails", [False, True])
def test_real_gateway_pins_selected_route_without_model_fallback(env, monkeypatch, fails):
    monkeypatch.setattr(LLMGateway, "call_messages", _REAL_CALL_MESSAGES)
    env.config.data["llm"]["tiers"]["medium"]["routes"] = [
        {"provider": "openai", "models": ["selected-model", "other-model"]},
        {"provider": "anthropic", "model": "fallback-model"},
    ]
    captured = []
    token = CancelToken()
    def resolve(self, tier, **kwargs):
        captured.append(kwargs)
        assert list(self.config.get("llm.tiers")) == [tier]
        def dispatch(request):
            assert request.tools == []
            assert request.cancel_token is token
            if fails:
                raise RuntimeError("selected transport unavailable")
            return MessagesResponse(content=[{"type": "text", "text": json.dumps(intent())}],
                                    provider="openai", model="selected-model")
        return dispatch
    monkeypatch.setattr(LLMGateway, "_resolve_messages_backend", resolve)
    payload = admit(env)
    payload["_task_cancel_token"] = token
    assert prepare_assistant_intent(env.config, payload)["status"] == ("clarify" if fails else "ready")
    assert len(captured) == 1
    routes = configured_routes(captured[0]["route_cfg"])
    assert len(routes) == 1 and routes[0]["provider"] == "openai" and routes[0]["models"] == ["selected-model"]


@pytest.mark.parametrize("reasoning", ["", json.dumps(intent("conversation"))])
def test_custom_provider_classifier_uses_real_messages_transport(env, monkeypatch, reasoning):
    monkeypatch.setattr(LLMGateway, "call_messages", _REAL_CALL_MESSAGES)
    env.config.data["llm"]["providers"] = {
        "agnes-ai": {"kind": "chat_completions", "base_url": "https://classifier.invalid/v1",
                     "provider_key_env": "NERYA_CLASSIFIER_TEST_KEY"},
    }
    env.config.data["llm"]["tiers"]["medium"]["routes"] = [
        {"provider": "agnes-ai", "models": ["agnes-3.0-flash", "other-model"]},
        {"provider": "openai", "model": "fallback-model"},
    ]
    env.config.data["llm"]["provider_native_web_search"] = True
    env.config.data["llm"]["tiers"]["medium"]["routes"][0]["provider_native_web_search"] = True
    monkeypatch.setenv("NERYA_CLASSIFIER_TEST_KEY", "test-only-placeholder")
    captured = []
    def post(_transport, url, *, headers, body, timeout):
        captured.append((url, body))
        return 200, {"choices": [{"message": {"content": json.dumps(intent()), "reasoning_content": reasoning},
                                  "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1}}, {}
    monkeypatch.setattr(UrllibTransport, "post_json_with_headers", post)
    assert set(capability_of("agnes-ai").tiers.values()) == {"unsupported"}
    payload = admit(env, text="Build an Aave lending strategy in paper mode.",
                    model_provider="agnes-ai", model_id="agnes-3.0-flash")
    before = deepcopy(env.config.data)
    result = prepare_assistant_intent(env.config, payload)
    assert result["status"] == "ready" and result["disposition"] == "task_create"
    assert env.config.data == before and len(captured) == 1
    url, body = captured[0]
    assert url == "https://classifier.invalid/v1/chat/completions"
    assert body["model"] == "agnes-3.0-flash"
    assert "tools" not in body and "response_format" not in body and "web_search_options" not in body
    assert "Schema: " in body["messages"][0]["content"]
    assert json.loads(body["messages"][1]["content"].split("\nInput: ", 1)[1])["text"] == payload["payload"]["text"]
    journal = [json.loads(line) for line in env.config.paths.journal("llm").read_text().splitlines()]
    assert any(row["caller"] == "assistant_intent" and row["provider"] == "agnes-ai" for row in journal)


def test_no_selected_model_does_not_use_other_model(env):
    env.config.data["llm"]["tiers"]["medium"].pop("model")
    gateway = Gateway()
    assert prepare(env, admit(env), gateway)["status"] == "clarify"
    assert not gateway.calls


def test_inherited_selection_tokens_use_assistant_model(env):
    env.config.data["agent"]["native"]["assistant_profile"].update(
        model_provider="anthropic", model_id="assistant-model", model_tier="medium")
    payload = admit(env, model_tier="auto", model_provider="default", model_id="auto")
    gateway = Gateway()
    assert prepare(env, payload, gateway)["status"] == "ready"
    assert (gateway.calls[0]["model_provider"], gateway.calls[0]["model_id"]) == ("anthropic", "assistant-model")


def test_different_response_model_cannot_publish_action(env):
    class WrongModel(Gateway):
        def call_messages(self, **kwargs):
            result = super().call_messages(**kwargs)
            result.model = "unexpected-fallback"
            return result
    assert prepare(env, admit(env), WrongModel())["status"] == "clarify"


def test_admission_edits_before_first_execution_classify_current_source(env):
    payload = admit(env)
    payload["payload"]["text"] = "A completely edited request"
    payload["work_mode"] = "goal"
    edit_request(env, payload)
    gateway = Gateway()
    result = prepare(env, payload, gateway)
    assert result["status"] == "ready"
    assert result["binding"]["input_digest"] == assistant_request_digest(json.loads(record(env)["request_json"]))
    assert json.loads(gateway.calls[0]["messages"][0]["content"].split("Input: ")[1])["text"] == payload["payload"]["text"]


def test_actual_database_isolation_even_with_same_command_and_session_ids(env, tmp_path):
    payload = admit(env)
    first = prepare(env, payload, Gateway(intent("conversation")))
    paths = WorkspacePaths(tmp_path / "separate-workspace")
    assistants = AssistantStore(paths)
    assistants.bind(env.sid, "main")
    other = SimpleNamespace(config=Config(paths, deepcopy(env.config.data)), assistants=assistants,
                            sid=env.sid, store=CommandStore(paths, "other-runtime"))
    other_payload = admit(other)
    second = prepare(other, other_payload)
    assert first["binding"] == second["binding"]
    assert context(env)["assistant_request"]["disposition"] == "conversation"
    assert context(other)["assistant_request"]["disposition"] == "task_create"
    with closing(connect_readonly(paths.db)) as con:
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='assistant_work'").fetchone()


@pytest.mark.parametrize("field", ["request_json", "context_json"])
def test_corrupt_command_storage_is_not_overwritten(env, field):
    payload = admit(env)
    with env.store.transaction() as con:
        con.execute(f"UPDATE agent_commands SET {field}=? WHERE command_id='command-intent-1'", ("not-json",))
    gateway = Gateway()
    with pytest.raises(AssistantIntentError):
        prepare(env, payload, gateway)
    assert not gateway.calls and record(env)[field] == "not-json"


def test_stopped_context_blocks_reuse_even_if_state_still_running(env):
    payload = admit(env)
    prepare(env, payload)
    stopped = {**context(env), "stop_requested": True}
    with env.store.transaction() as con:
        con.execute("UPDATE agent_commands SET context_json=?", (encode(stopped),))
    gateway = Gateway()
    with pytest.raises(AssistantIntentError):
        prepare(env, payload, gateway)
    assert not gateway.calls and context(env) == stopped


def continue_command(env, original, *, approval=False, context_fields=None):
    with env.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='succeeded'")
        con.execute("UPDATE agent_command_queues SET worker_owner='',paused=0,lease_until=0")
    payload = {**original, "payload": {"text": ""}, "continuation_feedback": "User answer: keep explaining.",
               "resume_turn_id": original["turn_id"]}
    if approval:
        payload.pop("resume_turn_id")
        payload.pop("continuation_feedback")
        payload.update(source="approval_continue", kind="approval.continue", payload={"text": "Approved; continue"})
    turn_id = "turn-approval" if approval else original["turn_id"]
    payload.pop("turn_id", None)
    env.store.accept(cid="continuation-1", sid=env.sid, actor="owner", kind="send" if approval else "resume",
                     request=payload, context=context_fields or {}, turn_id=turn_id)
    row = env.store.claim(env.sid, "continuation-worker")
    assert row and row["command_id"] == "continuation-1"
    env.config.data["runtime"].update(command_id=row["command_id"], command_turn_id=row["turn_id"])
    payload["turn_id"] = row["turn_id"]
    return payload


@pytest.mark.parametrize("approval", [False, True])
def test_conversation_continuation_never_calls_classifier_or_repeats_work(env, approval):
    original = admit(env)
    prior = prepare(env, original, Gateway(intent("conversation")))
    payload = continue_command(env, original, approval=approval,
                               context_fields={"assistant_continuation_source_command_id": "command-intent-1"} if approval else None)
    before, gateway = deepcopy(payload), Gateway()
    result = prepare(env, payload, gateway)
    assert result["status"] == "ready" and result["disposition"] == "conversation"
    assert result["binding"]["command_id"] == "continuation-1"
    assert result["binding"]["input_digest"] == assistant_request_digest(payload)
    assert result["binding"]["input_digest"] != prior["binding"]["input_digest"]
    assert prepare(env, payload, gateway) == result and not gateway.calls and payload == before


@pytest.mark.parametrize("original_disposition", [None, "task_create", "clarify"])
def test_old_or_nonconversation_resume_clarifies_without_dispatch(env, original_disposition):
    original = admit(env)
    if original_disposition:
        output = intent(original_disposition, clarification="Which task?" if original_disposition == "clarify" else "")
        prepare(env, original, Gateway(output))
    payload = continue_command(env, original)
    gateway = Gateway()
    result = prepare(env, payload, gateway)
    assert result["status"] == "clarify" and not gateway.calls


def test_editing_continuation_answer_cannot_reuse_contract(env):
    original = admit(env)
    prepare(env, original, Gateway(intent("conversation")))
    payload = continue_command(env, original)
    saved = prepare(env, payload)
    payload["continuation_feedback"] += " Changed answer"
    assert assistant_request_digest(payload) != saved["binding"]["input_digest"]
    with env.store.transaction() as con:
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='continuation-1'", (encode(payload),))
    with pytest.raises(AssistantIntentError):
        prepare(env, payload)


def test_approval_without_server_source_pointer_clarifies(env):
    original = admit(env)
    prepare(env, original, Gateway(intent("conversation")))
    payload = continue_command(env, original, approval=True)
    gateway = Gateway()
    assert prepare(env, payload, gateway)["status"] == "clarify" and not gateway.calls


def test_resume_hint_cannot_promote_conversation_to_task_creation(env):
    original = admit(env)
    prepare(env, original, Gateway(intent("conversation")))
    payload = continue_command(env, original)
    payload["assistant_intent"] = {"disposition": "task_create"}
    with env.store.transaction() as con:
        con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='continuation-1'", (encode(payload),))
    gateway = Gateway()
    assert prepare(env, payload, gateway)["disposition"] == "conversation" and not gateway.calls
