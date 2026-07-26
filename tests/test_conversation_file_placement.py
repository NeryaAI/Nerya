from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.agent.kernel import AgentKernel
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.tools.native.conversation_files import conversation_files_dir
from nerya.tools.native.bootstrap import build_native_tool_deps, register_native_tools
from nerya.tools.native.file_ops import (
    classify_file_mutation_risk,
    write_file_handler,
)
from nerya.tools.native.shell import classify_shell_risk, run_shell_handler
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import RiskLevel, ToolCall, ToolErrorKind


pytestmark = pytest.mark.smoke


def _json_part(result) -> dict:
    for part in result.content:
        if part.type == "json" and isinstance(part.data, dict):
            return part.data
    raise AssertionError("tool result did not contain a JSON part")


def test_new_conversation_file_is_rerouted_out_of_workspace_root(tmp_path) -> None:
    result = write_file_handler(
        ToolCall(
            name="write_file",
            arguments={"path": "notes.md", "contents": "session notes\n"},
        ),
        root=tmp_path,
        file_state=None,
        session_id="sess-1",
    )

    target = tmp_path / "artifacts" / "conversations" / "sess-1" / "notes.md"
    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "session notes\n"
    assert not (tmp_path / "notes.md").exists()
    payload = _json_part(result)
    assert payload["path"] == "artifacts/conversations/sess-1/notes.md"
    assert payload["kind"] == "create"
    assert payload["placement"] == "conversation_reroute"
    assert payload["bytes"] == 14
    assert payload["lines"] == 2
    assert payload["requested_path"] == "notes.md"
    assert len(payload["content_hash"]) == 64


def test_existing_file_keeps_its_canonical_path(tmp_path) -> None:
    target = tmp_path / "README.md"
    target.write_text("old\n", encoding="utf-8")

    result = write_file_handler(
        ToolCall(
            name="write_file",
            arguments={"path": "README.md", "contents": "new\n"},
        ),
        root=tmp_path,
        file_state=None,
        session_id="sess-1",
    )

    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "new\n"
    assert result.metadata["placement"] == "existing"
    assert not conversation_files_dir(tmp_path, "sess-1").exists()


def test_proposal_staging_path_is_not_rerouted(tmp_path) -> None:
    relative = "evolution/proposals/prp_1/after/report.md"

    result = write_file_handler(
        ToolCall(
            name="write_file",
            arguments={"path": relative, "contents": "proposal\n"},
        ),
        root=tmp_path,
        file_state=None,
        session_id="sess-1",
    )

    assert result.is_error is False
    assert (tmp_path / relative).read_text(encoding="utf-8") == "proposal\n"
    assert result.metadata["placement"] == "canonical"


def test_new_file_can_use_an_explicit_audited_exception(tmp_path) -> None:
    result = write_file_handler(
        ToolCall(
            name="write_file",
            arguments={
                "path": "official-report.md",
                "contents": "deliverable\n",
                "allow_outside_conversation": True,
                "outside_conversation_reason": "The user requested this exact path.",
            },
        ),
        root=tmp_path,
        file_state=None,
        session_id="sess-1",
    )

    assert result.is_error is False
    assert (tmp_path / "official-report.md").exists()
    assert result.metadata["placement"] == "explicit_exception"
    assert result.metadata["outside_conversation_reason"].startswith("The user")
    assert classify_file_mutation_risk(
        {"path": "official-report.md", "allow_outside_conversation": True}
    ) is RiskLevel.DANGEROUS


def test_explicit_exception_requires_a_reason(tmp_path) -> None:
    result = write_file_handler(
        ToolCall(
            name="write_file",
            arguments={
                "path": "official-report.md",
                "contents": "deliverable\n",
                "allow_outside_conversation": True,
            },
        ),
        root=tmp_path,
        file_state=None,
        session_id="sess-1",
    )

    assert result.is_error
    assert result.error is not None
    assert result.error.kind is ToolErrorKind.SCHEMA_VALIDATION
    assert "outside_conversation_reason is required" in result.error.message
    assert not (tmp_path / "official-report.md").exists()


def test_untrusted_session_id_cannot_escape_conversation_root(tmp_path) -> None:
    result = write_file_handler(
        ToolCall(
            name="write_file",
            arguments={"path": "notes.md", "contents": "safe\n"},
        ),
        root=tmp_path,
        file_state=None,
        session_id="../../outside",
    )

    target = result.metadata["path"]
    assert result.is_error is False
    assert target.startswith("artifacts/conversations/")
    assert (tmp_path / target).resolve().is_relative_to(tmp_path.resolve())
    assert not (tmp_path.parent / "outside" / "notes.md").exists()


def test_shell_write_defaults_to_conversation_directory(tmp_path) -> None:
    result = run_shell_handler(
        ToolCall(
            name="run_shell",
            arguments={"command": "printf 'hello' > shell-note.txt"},
        ),
        root=tmp_path,
        session_id="sess-1",
    )

    target = conversation_files_dir(tmp_path, "sess-1") / "shell-note.txt"
    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "hello"
    assert not (tmp_path / "shell-note.txt").exists()
    assert result.metadata["placement"] == "conversation_reroute"


def test_shell_write_rejects_explicit_cwd_and_parent_escape(tmp_path) -> None:
    explicit_root = run_shell_handler(
        ToolCall(
            name="run_shell",
            arguments={
                "command": "printf 'hello' > shell-note.txt",
                "cwd": ".",
            },
        ),
        root=tmp_path,
        session_id="sess-1",
    )
    parent_escape = run_shell_handler(
        ToolCall(
            name="run_shell",
            arguments={"command": "printf 'hello' > ../shell-note.txt"},
        ),
        root=tmp_path,
        session_id="sess-1",
    )

    for result in (explicit_root, parent_escape):
        assert result.is_error
        assert result.error is not None
        assert result.error.kind is ToolErrorKind.PERMISSION_DENIED
        assert result.error.detail["reason"] == "conversation_file_placement"
    assert not (tmp_path / "shell-note.txt").exists()


def test_shell_write_rejects_absolute_targets_outside_its_allowed_root(tmp_path) -> None:
    outside_workspace = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    workspace_root_target = tmp_path / "root-note.txt"

    workspace_escape = run_shell_handler(
        ToolCall(
            name="run_shell",
            arguments={"command": f"printf 'x' > '{outside_workspace}'"},
        ),
        root=tmp_path,
    )
    conversation_escape = run_shell_handler(
        ToolCall(
            name="run_shell",
            arguments={"command": f"printf 'x' > '{workspace_root_target}'"},
        ),
        root=tmp_path,
        session_id="sess-1",
    )

    for result in (workspace_escape, conversation_escape):
        assert result.is_error
        assert result.error is not None
        assert result.error.kind is ToolErrorKind.PERMISSION_DENIED
    assert not outside_workspace.exists()
    assert not workspace_root_target.exists()


def test_shell_write_can_use_an_explicit_audited_exception(tmp_path) -> None:
    result = run_shell_handler(
        ToolCall(
            name="run_shell",
            arguments={
                "command": "printf 'hello' > shell-note.txt",
                "cwd": ".",
                "allow_outside_conversation": True,
                "outside_conversation_reason": "A build requires the workspace root.",
            },
        ),
        root=tmp_path,
        session_id="sess-1",
    )

    assert result.is_error is False
    assert (tmp_path / "shell-note.txt").read_text(encoding="utf-8") == "hello"
    assert result.metadata["placement"] == "explicit_exception"
    assert classify_shell_risk(
        {"command": "printf x > y", "allow_outside_conversation": True}
    ) is RiskLevel.DANGEROUS


def test_system_prompt_names_the_active_conversation_directory(tmp_path) -> None:
    config = Config(
        paths=WorkspacePaths(root=tmp_path),
        data=deepcopy(DEFAULT_CONFIG),
    )
    kernel = AgentKernel(config=config, skills=None)  # type: ignore[arg-type]

    prompt = kernel._build_system_prompt(
        kernel._ensure_registry(),
        session_id="sess-1",
    )

    assert "Conversation file policy:" in prompt
    assert "artifacts/conversations/sess-1/" in prompt
    assert "test and build commands may keep the project cwd" in prompt
    assert "allow_outside_conversation=true" in prompt
    assert "outside_conversation_reason" in prompt


def test_registered_runtime_tools_use_the_active_turn_when_session_is_missing(
    tmp_path,
) -> None:
    registry = ToolRegistry()
    deps = build_native_tool_deps(workspace_root=tmp_path, skill_roots=[])
    register_native_tools(registry, deps)
    deps.active_conversation_id = "turn-1"

    descriptor = registry.get("write_file")
    result = descriptor.handler(
        ToolCall(
            name="write_file",
            arguments={"path": "turn-notes.md", "contents": "turn scoped\n"},
        )
    )

    target = tmp_path / "artifacts" / "conversations" / "turn-1" / "turn-notes.md"
    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "turn scoped\n"
    assert "allow_outside_conversation" in descriptor.input_schema["properties"]
