from __future__ import annotations

import pytest

from nerya.tools.native.file_ops import edit_file_handler, write_file_handler
from nerya.tools.native.shell import run_shell_handler
from nerya.tools.types import ToolCall, ToolErrorKind


pytestmark = pytest.mark.smoke


def _next_tool(result):
    assert result.is_error
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.PERMISSION_DENIED
    return result.error.recovery_hint["next_required_action"]["tool"]


def test_write_file_refuses_live_config_and_points_to_config_proposal(tmp_path):
    result = write_file_handler(
        ToolCall(
            name="write_file",
            arguments={"path": "nerya.yml", "contents": "llm: {}\n"},
        ),
        root=tmp_path,
        file_state=None,
    )

    assert _next_tool(result) == "evolve_core_config_patch"
    assert not (tmp_path / "nerya.yml").exists()


def test_write_file_refuses_live_news_feed_config_and_points_to_config_proposal(tmp_path):
    result = write_file_handler(
        ToolCall(
            name="write_file",
            arguments={
                "path": "news_feeds.yml",
                "contents": "feeds:\n  - url: https://example.com/feed.xml\n",
            },
        ),
        root=tmp_path,
        file_state=None,
    )

    assert _next_tool(result) == "evolve_core_config_patch"
    assert not (tmp_path / "news_feeds.yml").exists()


def test_write_file_refuses_live_message_channel_config_and_points_to_config_proposal(tmp_path):
    result = write_file_handler(
        ToolCall(
            name="write_file",
            arguments={
                "path": "messages/channels.yml",
                "contents": "channels:\n  discord_critical:\n    kind: discord\n",
            },
        ),
        root=tmp_path,
        file_state=None,
    )

    assert _next_tool(result) == "evolve_core_config_patch"
    assert not (tmp_path / "messages" / "channels.yml").exists()


def test_edit_file_refuses_live_strategy_and_points_to_strategy_proposal(tmp_path):
    strategy_file = tmp_path / "strategies" / "c1" / "main.py"
    strategy_file.parent.mkdir(parents=True)
    strategy_file.write_text("OLD = 1\n", encoding="utf-8")

    result = edit_file_handler(
        ToolCall(
            name="edit_file",
            arguments={
                "path": "strategies/c1/main.py",
                "old_string": "OLD = 1",
                "new_string": "OLD = 2",
            },
        ),
        root=tmp_path,
        file_state=None,
    )

    assert _next_tool(result) == "strategy_generate_proposal"
    assert strategy_file.read_text(encoding="utf-8") == "OLD = 1\n"


def test_run_shell_refuses_proposal_only_config_mutation(tmp_path):
    result = run_shell_handler(
        ToolCall(
            name="run_shell",
            arguments={
                "command": "Set-Content -Path nerya.yml -Value 'llm: {}'",
            },
        ),
        root=tmp_path,
    )

    assert _next_tool(result) == "evolve_core_config_patch"
    assert not (tmp_path / "nerya.yml").exists()


def test_run_shell_refuses_proposal_only_news_feed_config_mutation(tmp_path):
    result = run_shell_handler(
        ToolCall(
            name="run_shell",
            arguments={
                "command": "Set-Content -Path news_feeds.yml -Value 'feeds: []'",
            },
        ),
        root=tmp_path,
    )

    assert _next_tool(result) == "evolve_core_config_patch"
    assert not (tmp_path / "news_feeds.yml").exists()


def test_run_shell_refuses_proposal_only_message_channel_config_mutation(tmp_path):
    result = run_shell_handler(
        ToolCall(
            name="run_shell",
            arguments={
                "command": "Set-Content -Path messages/channels.yml -Value 'channels: {}'",
            },
        ),
        root=tmp_path,
    )

    assert _next_tool(result) == "evolve_core_config_patch"
    assert not (tmp_path / "messages" / "channels.yml").exists()


def test_run_shell_refuses_proposal_only_strategy_mutation(tmp_path):
    strategy_file = tmp_path / "strategies" / "c1" / "main.py"
    strategy_file.parent.mkdir(parents=True)
    strategy_file.write_text("OLD = 1\n", encoding="utf-8")

    result = run_shell_handler(
        ToolCall(
            name="run_shell",
            arguments={
                "command": "python -c \"open('strategies/c1/main.py','w').write('NEW')\"",
            },
        ),
        root=tmp_path,
    )

    assert _next_tool(result) == "strategy_generate_proposal"
    assert strategy_file.read_text(encoding="utf-8") == "OLD = 1\n"
