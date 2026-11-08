"""Offline producer -> model/event compaction acceptance regression."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nerya.agent.loop import WorkspaceNativeAgentLoop
from nerya.agent.tool_projection import project_tool_results
from nerya.data_api.registry import DataApiRegistry
from nerya.data_api.types import DataActionSpec
from nerya.data_api.uniswap_history import uniswap_specs
from nerya.tools.native.data_api import data_api_handler
from nerya.tools.result_contracts import parse_compacted_kept_jsonish
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke


@pytest.fixture
def project(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("network/provider execution forbidden")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("nerya.runtime.feature_flags.is_enabled", lambda *args: True)
    monkeypatch.setattr("nerya.llm.tool_raw_store.write_default", lambda **kwargs: "raw:fake")
    loop = object.__new__(WorkspaceNativeAgentLoop)
    loop.config = SimpleNamespace(workspace_root=str(tmp_path))

    def render(specs, arguments, *, add_padding=True):
        registry = DataApiRegistry()
        for spec in specs:
            registry.register_action(replace(spec, handler=forbidden))
        result = data_api_handler(ToolCall(name="data_api", arguments=arguments), registry=registry)
        assert not result.is_error, result.text()
        payload = result.content[0].data
        if add_padding:
            # Force the real threshold without changing the schema under test.
            payload.update(padding="x" * 3000, api_key="private-credential-sentinel",
                           provider_body={"error": "private-body-sentinel",
                                          "authorization": "private-body-sentinel"})
        before = deepcopy(payload)
        projected = project_tool_results(
            [result], render_tool_result=loop._render_tool_result,
            rendered_tool_result_text=loop._rendered_tool_result_text,
        )
        block = projected.transcript_blocks[0]
        text = block["content"][0]["text"]
        assert block.get("compaction"), "must exercise real compaction"
        assert projected.event_blocks[0]["ok"] is True
        assert projected.event_blocks[0]["result"] == text
        assert "private-credential-sentinel" not in text
        assert "private-body-sentinel" not in text
        assert "provider_body" not in text
        assert payload == before
        return payload, parse_compacted_kept_jsonish(text), block["compaction"]

    return render


def generic_spec():
    return DataActionSpec(
        provider="generic_fixture", action="bounded_read", title="Bounded read",
        description="Inspect required fields before calling.",
        input_schema={
            "type": "object", "additionalProperties": False,
            "required": ["scope", "mode", "samples", "filters"],
            "properties": {
                "scope": {"type": "string", "pattern": "^[a-z]+$"},
                "mode": {"type": "string", "enum": [f"mode_{i}" for i in range(20)]},
                "samples": {"type": "integer", "minimum": 1, "maximum": 128},
                "filters": {"type": "array", "minItems": 1, "maxItems": 24,
                            "items": {"type": "object", "required": ["token_address"],
                                      "properties": {"token_address": {"type": "string"}}}},
                **{f"option_{i}": {"type": "number", "exclusiveMinimum": 0,
                                    "maximum": i + 1} for i in range(24)},
            },
            "oneOf": [{"required": ["option_0"]}, {"required": ["option_1"]}],
        },
        handler=lambda *args: pytest.fail("not a data call"), replay_input=True,
    )


@pytest.mark.parametrize("actual_provider", [True, False])
@pytest.mark.parametrize("add_padding", [True, False])
def test_complete_schema_survives_real_producer_model_and_event_compaction(project, actual_provider, add_padding):
    spec = uniswap_specs()[0] if actual_provider else generic_spec()
    payload, kept, compaction = project([spec], {
        "op": "schema", "provider": spec.provider, "action": spec.action,
    }, add_padding=add_padding)
    assert payload["input_schema"] == spec.input_schema
    assert kept.get("input_schema") == spec.input_schema
    assert compaction["rule_id"] == "data_api.schema"
    assert kept["provider"] == spec.provider
    assert kept["action"] == spec.action
    assert kept["title"] == spec.title
    assert kept["description"] == spec.description
    assert kept["output_kind"] == spec.output_kind
    assert kept["replay_input"] is True
    assert "data" not in kept


def test_list_retains_generic_action_metadata_without_executing_provider(project):
    spec = generic_spec()
    _, kept, compaction = project([spec], {"op": "list", "provider": spec.provider})
    assert compaction["rule_id"] == "data_api.catalog"
    row = kept["actions_sample"][0]
    assert row["provider"] == spec.provider
    assert row["action"] == spec.action
    assert row["title"] == spec.title
    assert row["output_kind"] == spec.output_kind
    assert row["replay_input"] is True
