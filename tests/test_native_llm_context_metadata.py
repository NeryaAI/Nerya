from __future__ import annotations

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.tools.native import llm as native_llm
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(
        name=name,
        arguments=args,
        id="toolu-llm",
        turn_id="turn-native-llm",
        iteration=7,
        metadata={
            "session_id": "sess-native-llm",
            "strategy_id": "strategy-llm",
            "trigger_event_id": "trigger-llm",
            "team_run_id": "team-native-llm",
        },
    )


def test_llm_complete_handler_passes_parent_tool_metadata(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[dict] = []

    class FakeGateway:
        def __init__(self, _config):  # noqa: ANN001
            pass

        def call(self, **kwargs):  # noqa: ANN001
            calls.append(kwargs)

            class Result:
                raw = "ok"
                parsed = {}
                tier = "light"
                usd = 0.0

            return Result()

    monkeypatch.setattr(native_llm, "LLMGateway", FakeGateway)
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})

    result = native_llm.llm_complete_handler(
        _call("llm_complete", {"task": "compress", "prompt": "summarize"}),
        config=cfg,
    )

    assert result.is_error is False
    metadata = calls[0]["metadata"]
    assert metadata["session_id"] == "sess-native-llm"
    assert metadata["turn_id"] == "turn-native-llm"
    assert metadata["iteration"] == 7
    assert metadata["strategy_id"] == "strategy-llm"
    assert metadata["trigger_event_id"] == "trigger-llm"
    assert metadata["parent_call_id"] == "toolu-llm"
    assert metadata["team_run_id"] == "team-native-llm"
    assert metadata["context_scope"] == "native_llm_complete"


def test_llm_classify_handler_passes_parent_tool_metadata(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[dict] = []

    class FakeGateway:
        def __init__(self, _config):  # noqa: ANN001
            pass

        def classify(self, **kwargs):  # noqa: ANN001
            calls.append(kwargs)
            return {"label": "risk"}

    monkeypatch.setattr(native_llm, "LLMGateway", FakeGateway)
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})

    result = native_llm.llm_classify_handler(
        _call(
            "llm_classify",
            {"text": "risky", "labels": ["safe", "risk"], "tier": "light"},
        ),
        config=cfg,
    )

    assert result.is_error is False
    metadata = calls[0]["metadata"]
    assert metadata["session_id"] == "sess-native-llm"
    assert metadata["turn_id"] == "turn-native-llm"
    assert metadata["iteration"] == 7
    assert metadata["parent_call_id"] == "toolu-llm"
    assert metadata["team_run_id"] == "team-native-llm"
    assert metadata["context_scope"] == "native_llm_classify"


def test_llm_extract_json_handler_passes_parent_tool_metadata(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[dict] = []

    class FakeGateway:
        def __init__(self, _config):  # noqa: ANN001
            pass

        def extract_json(self, **kwargs):  # noqa: ANN001
            calls.append(kwargs)
            return {"value": 1}

    monkeypatch.setattr(native_llm, "LLMGateway", FakeGateway)
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})

    result = native_llm.llm_extract_json_handler(
        _call("llm_extract_json", {"text": "{\"value\": 1}"}),
        config=cfg,
    )

    assert result.is_error is False
    metadata = calls[0]["metadata"]
    assert metadata["turn_id"] == "turn-native-llm"
    assert metadata["iteration"] == 7
    assert metadata["team_run_id"] == "team-native-llm"
    assert metadata["context_scope"] == "native_llm_extract_json"


def test_llm_compress_handler_passes_parent_tool_metadata(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[dict] = []

    class FakeGateway:
        def __init__(self, _config):  # noqa: ANN001
            pass

        def compress(self, **kwargs):  # noqa: ANN001
            calls.append(kwargs)
            return "short"

    monkeypatch.setattr(native_llm, "LLMGateway", FakeGateway)
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})

    result = native_llm.llm_compress_handler(
        _call("llm_compress", {"text": "long text"}),
        config=cfg,
    )

    assert result.is_error is False
    metadata = calls[0]["metadata"]
    assert metadata["turn_id"] == "turn-native-llm"
    assert metadata["iteration"] == 7
    assert metadata["team_run_id"] == "team-native-llm"
    assert metadata["context_scope"] == "native_llm_compress"
