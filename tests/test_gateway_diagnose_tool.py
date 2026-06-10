from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.tools.native.bootstrap import build_native_tool_deps, register_native_tools
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def _json_payload(result):
    assert not result.is_error, result.text()
    assert result.content
    return result.content[0].data


def test_gateway_diagnose_registered_as_read_only_tool(tmp_path) -> None:
    cfg = _config(tmp_path)
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[tmp_path / "skills"],
        paths=cfg.paths,
        config=cfg,
    )

    register_native_tools(registry, deps)

    descriptor = registry.get("gateway_diagnose")
    assert descriptor.read_only is True
    assert descriptor.auto_approve is True


def test_gateway_diagnose_reports_missing_telegram_token_ref(tmp_path) -> None:
    cfg = _config(tmp_path)
    yaml_io.dump(
        cfg.paths.messages_channels,
        {"channels": {"telegram": {"kind": "telegram"}}},
    )
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[tmp_path / "skills"],
        paths=cfg.paths,
        config=cfg,
    )
    register_native_tools(registry, deps)

    result = registry.get("gateway_diagnose").handler(
        ToolCall(name="gateway_diagnose", arguments={"platform": "telegram"})
    )
    payload = _json_payload(result)

    assert payload["ok"] is False
    assert payload["platform"] == "telegram"
    assert payload["channel"] == "telegram"
    assert payload["channels_file_exists"] is True
    assert payload["channel_configured"] is True
    assert payload["configured"]["bot_token_ref"] is False
    assert payload["error"] == "telegram: missing bot_token_ref"
