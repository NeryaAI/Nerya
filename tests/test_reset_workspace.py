import json
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.smoke


def test_reset_workspace_clear_memory_preserves_vault(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    vault = workspace / "vault"
    approvals = workspace / "approvals"
    memory.mkdir(parents=True)
    vault.mkdir(parents=True)
    approvals.mkdir(parents=True)
    (workspace / "state").mkdir()
    (memory / "global.md").write_text("only trade ETH and SOL\n", encoding="utf-8")
    (memory / "operator_profile.jsonl").write_text('{"facet":"veto"}\n', encoding="utf-8")
    (memory / "profile_capture_state.json").write_text("{}", encoding="utf-8")
    (vault / "secrets.json").write_text("encrypted", encoding="utf-8")
    (approvals / "pending.jsonl").write_text('{"state":"pending"}\n', encoding="utf-8")
    log_path = workspace / "reset-log.json"

    result = subprocess.run(
        [
            sys.executable,
            "tools/reset_workspace.py",
            "--workspace",
            str(workspace),
            "--clear-memory",
            "--log",
            str(log_path),
            "--quiet",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert not (memory / "global.md").exists()
    assert not (memory / "operator_profile.jsonl").exists()
    assert not (memory / "profile_capture_state.json").exists()
    assert not (approvals / "pending.jsonl").exists()
    assert (vault / "secrets.json").read_text(encoding="utf-8") == "encrypted"
    assert json.loads(log_path.read_text(encoding="utf-8"))["clear_memory"] is True


def test_reset_workspace_reseeds_manual_agent_strategy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "state").mkdir(parents=True)
    (workspace / "accounts").mkdir()
    (workspace / "strategies" / "stale").mkdir(parents=True)
    (workspace / "strategies" / "stale" / "strategy.yml").write_text(
        "id: stale\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "tools/reset_workspace.py",
            "--workspace",
            str(workspace),
            "--quiet",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert not (workspace / "strategies" / "stale").exists()
    manual_root = workspace / "strategies" / "manual_agent"
    assert (manual_root / "strategy.yml").exists()
    assert (manual_root / "limits.yml").exists()


def test_reset_workspace_can_sync_default_prompt_bundle_without_deleting_custom_roles(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "state").mkdir(parents=True)
    subagents = workspace / "subagents"
    subagents.mkdir()
    (subagents / "research_manager.agent.md").write_text(
        "你是 A 股研究经理。中文输出。\n",
        encoding="utf-8",
    )
    custom_role = subagents / "china_macro_expert.agent.md"
    custom_role.write_text(
        "Custom operator role that must survive an E2E reset.\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "tools/reset_workspace.py",
            "--workspace",
            str(workspace),
            "--sync-prompt-bundle",
            "--quiet",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    refreshed = (subagents / "research_manager.agent.md").read_text(encoding="utf-8")
    assert "You are the research manager" in refreshed
    assert "A 股" not in refreshed
    assert custom_role.read_text(encoding="utf-8") == (
        "Custom operator role that must survive an E2E reset.\n"
    )
