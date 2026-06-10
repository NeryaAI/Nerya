from __future__ import annotations

import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import routes_security
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.mcp.connectors.bootstrap import VaultResolver, _build_stdio_client
from nerya.mcp.connectors.config import AuthConfig, MCPServerConfig, StdioTransportConfig
from nerya.security.runtime_env import (
    build_process_env,
    put_runtime_env,
    runtime_env_values,
)
from nerya.tools.native.shell import run_shell_handler
from nerya.tools.native.skill import SkillIndex, script_run_handler
from nerya.tools.types import ToolCall, ToolErrorKind


pytestmark = pytest.mark.smoke


def _client(tmp_path):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    return SimpleNamespace(config=cfg)


def _routes():
    return {(method, path): handler for method, path, handler in routes_security.routes()}


def _shell_stdout(result) -> str:
    part = next(p for p in result.content if p.type == "shell")
    return str((part.data or {}).get("stdout") or "")


def test_runtime_env_values_are_vault_backed(tmp_path):
    row = put_runtime_env(tmp_path, name="nerya_test_api_key", value="secret-value")

    assert row["name"] == "NERYA_TEST_API_KEY"
    assert row["ref"] == "vault://env.nerya_test_api_key"
    assert runtime_env_values(tmp_path)["NERYA_TEST_API_KEY"] == "secret-value"
    assert b"secret-value" not in (tmp_path / "vault" / "secrets.enc").read_bytes()


def test_security_env_routes_manage_env_without_revealing_plaintext(tmp_path):
    routes = _routes()
    client = _client(tmp_path)

    put = routes[("POST", "/security/env/put")](
        client,
        {"name": "RUNTIME_TOKEN", "value": "top-secret"},
    )
    assert put["ok"] is True
    assert put["env"]["name"] == "RUNTIME_TOKEN"
    assert "top-secret" not in str(put)

    listed = routes[("POST", "/security/env/list")](client, {})
    assert listed["count"] == 1
    assert listed["env"][0]["ref"] == "vault://env.runtime_token"
    assert "top-secret" not in str(listed)

    deleted = routes[("POST", "/security/env/delete")](client, {"name": "RUNTIME_TOKEN"})
    assert deleted == {"ok": True, "name": "RUNTIME_TOKEN"}
    assert routes[("POST", "/security/env/list")](client, {})["count"] == 0


def test_run_shell_loads_vault_runtime_env(tmp_path):
    put_runtime_env(tmp_path, name="NERYA_SHELL_ENV", value="shell-loaded")
    command = (
        f'"{sys.executable}" -c "import os;'
        "print(os.getenv('NERYA_SHELL_ENV', ''))\""
    )

    result = run_shell_handler(
        ToolCall(name="run_shell", arguments={"command": command}),
        root=tmp_path,
    )

    assert result.is_error is False
    assert _shell_stdout(result).strip() == "shell-loaded"


def test_run_shell_redirects_native_strategy_data_discovery(tmp_path):
    command = (
        "python -c \"from nerya.data import data_api; "
        "data_api(op='call', provider='wallet', action='capability_catalog')\""
    )

    result = run_shell_handler(
        ToolCall(
            name="run_shell",
            arguments={
                "command": command,
                "description": "Check wallet capability catalog structure",
            },
        ),
        root=tmp_path,
    )

    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind is ToolErrorKind.PERMISSION_DENIED
    assert "strategy_author" in result.text()
    assert "strategy_generate_proposal" in result.text()


def test_run_shell_redirects_workspace_file_enumeration(tmp_path):
    result = run_shell_handler(
        ToolCall(
            name="run_shell",
            arguments={
                "command": 'dir "C:\\Users\\Ricky\\.nerya" /s /b',
                "description": "List workspace files",
            },
        ),
        root=tmp_path,
    )

    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind is ToolErrorKind.PERMISSION_DENIED
    assert result.error.retryable is False
    assert result.error.recovery_hint["reason"] == "tool_redirect"
    assert result.error.recovery_hint["preferred_tools"] == ["glob", "list_dir", "read_file"]
    assert result.error.detail["reason"] == "tool_redirect"
    assert "glob" in result.text()
    assert "list_dir" in result.text()


def test_run_shell_refuses_absolute_read_path_outside_workspace(tmp_path):
    result = run_shell_handler(
        ToolCall(
            name="run_shell",
            arguments={"command": "cat /etc/passwd"},
        ),
        root=tmp_path,
    )

    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind is ToolErrorKind.PERMISSION_DENIED
    text = result.text()
    assert "permission_denied" in text
    assert "workspace sandbox" in text
    assert "/etc/passwd" in text


def test_script_run_loads_vault_runtime_env(tmp_path):
    put_runtime_env(tmp_path, name="NERYA_SCRIPT_ENV", value="script-loaded")
    skill_dir = tmp_path / "skills" / "demo"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nid: demo\ndescription: test skill\n---\n# Demo\n",
        encoding="utf-8",
    )
    (scripts_dir / "print_env.py").write_text(
        "import os\nprint(os.getenv('NERYA_SCRIPT_ENV', ''))\n",
        encoding="utf-8",
    )
    index = SkillIndex([tmp_path / "skills"])

    result = script_run_handler(
        ToolCall(
            name="script_run",
            arguments={"skill_id": "demo", "name": "print_env.py"},
        ),
        skill_index=index,
        cwd=tmp_path,
    )

    assert result.is_error is False
    part = next(p for p in result.content if p.type == "json")
    assert "script-loaded" in str((part.data or {}).get("stdout") or "")


def test_script_run_preserves_parsed_stdout_json_before_tail_truncation(tmp_path):
    skill_dir = tmp_path / "skills" / "demo"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nid: demo\ndescription: test skill\n---\n# Demo\n",
        encoding="utf-8",
    )
    (scripts_dir / "emit_json.py").write_text(
        "import json\n"
        "items = [{'title': f'Headline {i}', 'url': f'https://example.com/{i}'} for i in range(200)]\n"
        "print(json.dumps({'ok': True, 'count': len(items), 'items': items}))\n",
        encoding="utf-8",
    )
    index = SkillIndex([tmp_path / "skills"])

    result = script_run_handler(
        ToolCall(
            name="script_run",
            arguments={"skill_id": "demo", "name": "emit_json.py"},
        ),
        skill_index=index,
        cwd=tmp_path,
    )

    assert result.is_error is False
    part = next(p for p in result.content if p.type == "json")
    assert part.data["stdout_json"]["count"] == 200
    assert part.data["stdout_json"]["items"][0]["title"] == "Headline 0"


def test_stdio_mcp_client_receives_vault_runtime_env(tmp_path):
    put_runtime_env(tmp_path, name="NERYA_MCP_ENV", value="mcp-loaded")
    cfg = MCPServerConfig(
        id="local",
        enabled=True,
        namespace="local",
        transport=StdioTransportConfig(command=("fake-mcp",)),
        auth=AuthConfig(kind="none"),
    )

    client = _build_stdio_client(
        cfg,
        vault=VaultResolver(paths=WorkspacePaths(root=tmp_path)),
    )

    assert client.env["NERYA_MCP_ENV"] == "mcp-loaded"
    assert build_process_env({}, tmp_path)["NERYA_WORKSPACE"] == str(tmp_path)
