"""Provider-boundary contracts; these do not substitute for a live model retest."""
from __future__ import annotations

import copy
import json

import pytest

from nerya.agent import kernel as kernel_module
from nerya.agent.assistant_work import AssistantWork
from nerya.agent.assistants import AssistantStore, assistant_config
from nerya.agent.command_runtime import CommandRuntime
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.llm.messages import MessagesRequest, MessagesResponse, OpenAIMessagesBackend
from nerya.sdk.internal_client import InternalClient
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.native.tasks import assistant_task_collaboration_handler, assistant_task_create_handler
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke

REQUESTS = (
    "帮我做一个 BTC/USDT 1小时均线趋势策略，回测最近30天，不启用交易。",
    "帮我做一个 Jupiter 上 SOL/USDC 的趋势策略，回测最近30天，不启用交易。",
    "帮我做一个展示 BTC、ETH 行情和账户概览的面板。",
)


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    config = Config(paths=WorkspacePaths(tmp_path), data=copy.deepcopy(DEFAULT_CONFIG))
    store = AssistantStore(config.paths)
    sid = store.home("main")["session_id"]
    manager = CommandRuntime(config, lambda *_: {"final_text": "done"})
    monkeypatch.setattr(manager, "kick", lambda _sid: None)
    monkeypatch.setattr("nerya.agent.command_runtime.runtime", lambda _: manager)
    captured = []

    class Gateway:
        def __init__(self, *_a, **_kw):
            pass

        def effective_model_metadata(self, *_a, **_kw):
            return "fixture", "test", {}

        def call_messages(self, **kwargs):
            captured.append(copy.deepcopy({key: kwargs[key] for key in ("system", "tools", "messages")}))
            # Exercise actual lazy loading and task creation in the first turn.
            # Later turns probe exposure after history exists, without asking a
            # scripted model to pretend it reproduces a model's hallucination.
            n = len(captured)
            if n == 1:
                content = [{"type": "tool_use", "id": "load-tasks",
                            "name": "Skill", "input": {"skill": "tasks"}}]
            elif n == 2:
                content = [{"type": "tool_use", "id": "create-first",
                            "name": "assistant_task_create",
                            "input": {"title": "BTC trend", "kind": "strategy",
                                      "prompt": REQUESTS[0]}}]
            else:
                content = [{"type": "text", "text": "Provider-boundary probe complete."}]
            return MessagesResponse(content=content, stop_reason="tool_use" if n < 3 else "end_turn",
                                    usage={"input_tokens": 10, "output_tokens": 10},
                                    provider="fixture", model="test")

    monkeypatch.setattr(kernel_module, "LLMGateway", Gateway)
    kernels = []
    for i, request in enumerate(REQUESTS):
        # HTTP admission builds a fresh kernel for each command.
        kernel = kernel_module.AgentKernel(config=config, skills=InternalClient.from_config(config).skills)
        kernel.run_turn(trigger={"kind": "user.chat", "source": "user_chat",
                                 "payload": {"text": request}},
                        session_id=sid, turn_id=f"contract-turn-{i}")
        kernels.append(kernel)
    assert len(captured) == 5
    return config, sid, captured, kernels[-1]


def test_following_turns_keep_coordinator_identity_and_actual_tool_exposure(conversation):
    config, sid, captured, _ = conversation
    for request in (captured[0], captured[3], captured[4]):
        names = {tool["name"] for tool in request["tools"]}
        assert {"Skill", "assistant_task_create", "assistant_task_list",
                "assistant_task_read", "assistant_task_message"} <= names
        assert not {"write_file", "run_shell", "strategy_draft_proposal"} & names
        assert "communication and coordination conversation" in request["system"]
        assert "<name>tasks</name>" in request["system"]
    tasks = AssistantWork(config.paths).tasks(sid=sid)
    assert len(tasks) == 1
    assert tasks[0]["task_id"] in json.dumps(captured[2]["messages"])
    assert REQUESTS[0] in json.dumps(captured[3]["messages"], ensure_ascii=False)


def test_actual_provider_context_has_durable_task_identity_after_prior_turn(conversation):
    # Assert the actual boundary, not a scripted model's choice of action.
    config, sid, captured, _ = conversation
    receipt = AssistantWork(config.paths).tasks(sid=sid)[0]
    for request in captured[3:]:
        assert '"protocol": "nerya.assistant_coordination.v1"' in request["system"]
        assert receipt["task_id"] in request["system"]
        assert receipt["command_id"] in request["system"]
        assert '"hard_status": "missing"' in request["system"]


def test_following_turns_keep_tools_and_role_in_chat_completions_wire_body(conversation):
    _, _, captured, _ = conversation
    bodies = []

    class Transport:
        def post_json(self, _url, *, body, **_kwargs):
            bodies.append(copy.deepcopy(body))
            return 200, {"choices": [{"message": {"role": "assistant", "content": "probe"},
                                      "finish_reason": "stop"}], "usage": {}}

    backend = OpenAIMessagesBackend(api_key="fixture", model="agnes-3.0-flash",
                                   provider_name="agnes-ai", transport=Transport())
    for request in (captured[0], captured[3], captured[4]):
        backend(MessagesRequest(**request))
    for body in bodies:
        names = {tool["function"]["name"] for tool in body["tools"]}
        assert {"Skill", "assistant_task_create", "assistant_task_list",
                "assistant_task_read", "assistant_task_message"} <= names
        assert body["tool_choice"] == "auto"
        assert body["messages"][0]["role"] == "system"
        assert "communication and coordination conversation" in body["messages"][0]["content"]


def test_each_provider_request_gets_receipt_and_artifact_evidence_contract(conversation):
    _, _, captured, _ = conversation
    for request in captured:
        prompt = request["system"]
        assert 'Skill(skill="tasks")' in prompt
        assert "Every distinct requested task" in prompt
        assert "successful assistant_task_create invocation" in prompt
        assert "Never derive task IDs or copy a previous receipt" in prompt
        assert "artifact creation requires tool evidence" in prompt
        assert "Never put API keys or secrets in HTML" in prompt


def test_create_descriptor_advertises_fresh_receipts_before_skill_is_loaded(conversation):
    _, _, captured, _ = conversation
    for request in (captured[0], captured[3], captured[4]):
        descriptor = next(t for t in request["tools"] if t["name"] == "assistant_task_create")
        description = descriptor["description"]
        assert "Every distinct requested task" in description
        assert "successful receipt" in description
        assert "Never derive" in description
        assert "assistant_task_list/read/message" in description


def test_tasks_lazy_metadata_and_body_route_coordinator_work_before_domain_work(conversation):
    _, _, _, kernel = conversation
    index = kernel._ensure_registry().skill_index
    assert "assistant task coordination" in index.get("tasks").description
    result = skill_tool_handler(ToolCall(id="read-tasks", name="Skill", arguments={"skill": "tasks"}),
                                skill_index=index)
    assert not result.is_error
    body = result.content[0].text
    assert "Every distinct requested task" in body
    assert "Never derive task IDs or copy a previous receipt" in body
    assert "task_id" in body and "task_session_id" in body
    assert "No successful receipt means no confirmed task creation" in body
    assert "artifact creation requires tool evidence" in body
    assert "Never put API keys or secrets in HTML" in body
    assert "execution conversation" in body
    assert body.index("assistant_task_create") < body.index("strategy_author")


def test_runtime_role_guidance_preserves_profile_and_execution_boundary(tmp_path):
    config = Config(paths=WorkspacePaths(tmp_path), data=copy.deepcopy(DEFAULT_CONFIG))
    store = AssistantStore(config.paths)
    profile = store.save({"name": "Scout", "instructions": "Reply concisely."})
    original = copy.deepcopy(profile)
    sid = store.home(profile["id"])["session_id"]
    for _ in range(2):
        config = assistant_config(config, profile, session_id=sid)
        assert config.get("agent.native.assistant_profile.instructions") == "Reply concisely."
        role = config.get("agent.native.assistant_profile.role")
        assert role.count("Every distinct requested task") == 1
    store.bind("execution-task", profile["id"], kind="task")
    execution = assistant_config(config, profile, session_id="execution-task")
    role = execution.get("agent.native.assistant_profile.role")
    assert "Every distinct requested task" not in role
    assert "Never put API keys or secrets in HTML" in role
    assert profile == original == store.get(profile["id"])


def test_distinct_turns_get_independent_durable_receipts_and_authoritative_reads(conversation):
    config, sid, _, kernel = conversation
    deps = kernel._ensure_registry()
    receipts = []
    for i, prompt in enumerate(REQUESTS[1:], start=1):
        # Providers can reuse a call ID on a new turn; it is not the task ID.
        call = ToolCall(id="create-first", turn_id=f"contract-turn-{i}",
                        name="assistant_task_create", arguments={"title": f"Task {i}", "prompt": prompt})
        result = assistant_task_create_handler(call, config=config, skills=deps.skills,
                                               store=deps.task_store, session_id=sid,
                                               actor_id=deps.active_actor_id)
        assert not result.is_error
        receipt = json.loads(result.text())
        receipts.append(receipt)
        replay = assistant_task_create_handler(call, config=config, skills=deps.skills,
                                               store=deps.task_store, session_id=sid,
                                               actor_id=deps.active_actor_id)
        assert json.loads(replay.text())["task_id"] == receipt["task_id"]
    listed = assistant_task_collaboration_handler(ToolCall(name="assistant_task_list"),
        config=config, session_id=sid, actor_id=deps.active_actor_id, operation="list")
    tasks = json.loads(listed.text())["tasks"]
    assert len(tasks) == 3
    for key in ("task_id", "task_session_id", "command_id"):
        assert len({task[key] for task in tasks}) == 3
    for receipt in receipts:
        read = assistant_task_collaboration_handler(
            ToolCall(name="assistant_task_read", arguments={"task_id": receipt["task_id"]}),
            config=config, session_id=sid, actor_id=deps.active_actor_id, operation="read")
        assert not read.is_error
        authoritative = json.loads(read.text())
        assert authoritative["command_id"] == receipt["command_id"]
        assert authoritative["state"] == "queued"
    fabricated = assistant_task_collaboration_handler(
        ToolCall(name="assistant_task_read", arguments={"task_id": "awork_99d9495e5a13e6d34c04640f"}),
        config=config, session_id=sid, actor_id=deps.active_actor_id, operation="read")
    assert fabricated.is_error
