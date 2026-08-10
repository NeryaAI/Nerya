"""Tests for the tiered-autonomy auto-apply lane and the new
real-money crypto-readiness / auto-approve guards."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.core.config import Config
from nerya.core.errors import ProtectedScopeViolation
from nerya.core.paths import WorkspacePaths
from nerya.evolution.auto_apply import (
    auto_apply_tick,
    evaluate_auto_apply,
)
from nerya.evolution.patch_proposal import create_proposal, list_proposals
from nerya.evolution.self_config import propose_core_config_patch
from nerya.evolution.validation_plan import (
    build_validation_plan,
    run_validation_plan,
    write_validation_plan,
)
from nerya.trading.submit import _real_money_execution_blocker

pytestmark = pytest.mark.smoke


def _config(tmp_path, *, auto_apply_enabled: bool) -> Config:
    return Config(
        paths=WorkspacePaths(tmp_path),
        data={
            "runtime": {"live_trading_enabled": True, "kill_switch": False},
            "evolution": {"auto_apply": {"enabled": auto_apply_enabled}},
        },
    )


def _passed_plan_id(paths: WorkspacePaths, tmp_path) -> str:
    sample = tmp_path / "test_auto_apply_sample.py"
    sample.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    plan = build_validation_plan(
        [{"type": "unit_test", "command": f"python -m pytest {sample} -q"}],
        source="test",
    )
    plan_id = write_validation_plan(paths, plan)
    result = run_validation_plan(paths, plan_id=plan_id, dry_run=False)
    assert result["status"] == "passed", result
    return plan_id


def _prompt_proposal(paths: WorkspacePaths, plan_id: str | None = None):
    return create_proposal(
        paths,
        kind="prompt_patch",
        summary="Reword main agent prompt",
        initial_state="pending_review",
        validation_plan_id=plan_id,
        extra_files={
            "after/agents/main.agent.md": "# Main Agent\n\nBe concise.\n",
        },
    )


def test_auto_apply_requires_opt_in_flag(tmp_path):
    config = _config(tmp_path, auto_apply_enabled=False)
    plan_id = _passed_plan_id(config.paths, tmp_path)
    prop = _prompt_proposal(config.paths, plan_id)
    verdict = evaluate_auto_apply(config.paths, config, prop)
    assert not verdict["eligible"]
    assert "auto_apply_disabled" in verdict["reasons"]


def test_auto_apply_requires_passed_validation(tmp_path):
    config = _config(tmp_path, auto_apply_enabled=True)
    prop = _prompt_proposal(config.paths, plan_id=None)
    verdict = evaluate_auto_apply(config.paths, config, prop)
    assert not verdict["eligible"]
    assert any("validation" in r for r in verdict["reasons"])


def test_auto_apply_rejects_paths_outside_allowlist(tmp_path):
    config = _config(tmp_path, auto_apply_enabled=True)
    plan_id = _passed_plan_id(config.paths, tmp_path)
    prop = create_proposal(
        config.paths,
        kind="prompt_patch",
        summary="Sneak a strategy file through the prose lane",
        initial_state="pending_review",
        validation_plan_id=plan_id,
        extra_files={
            "after/strategies/alpha/main.py": "def run(ctx):\n    return {}\n",
        },
    )
    verdict = evaluate_auto_apply(config.paths, config, prop)
    assert not verdict["eligible"]
    assert any(r.startswith("path_not_allowed:") for r in verdict["reasons"])


def test_auto_apply_rejects_declared_deletion_outside_allowlist(tmp_path):
    config = _config(tmp_path, auto_apply_enabled=True)
    target = tmp_path / "strategies" / "alpha" / "main.py"
    target.parent.mkdir(parents=True)
    target.write_text("def run(ctx):\n    return {}\n", encoding="utf-8")
    plan_id = _passed_plan_id(config.paths, tmp_path)
    prop = create_proposal(
        config.paths,
        kind="prompt_patch",
        summary="Hide a strategy deletion behind an allowed prompt patch",
        initial_state="pending_review",
        validation_plan_id=plan_id,
        metadata={"deleted_files": ["strategies/alpha/main.py"]},
        extra_files={"after/agents/main.agent.md": "# Main Agent\n"},
    )

    verdict = evaluate_auto_apply(config.paths, config, prop)

    assert not verdict["eligible"]
    assert "path_not_allowed:strategies/alpha/main.py" in verdict["reasons"]


def test_auto_apply_counts_declared_deletions_toward_file_limit(tmp_path):
    config = _config(tmp_path, auto_apply_enabled=True)
    target = tmp_path / "agents" / "old.agent.md"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    plan_id = _passed_plan_id(config.paths, tmp_path)
    prop = create_proposal(
        config.paths,
        kind="prompt_patch",
        summary="Five touched prompt files",
        initial_state="pending_review",
        validation_plan_id=plan_id,
        metadata={"deleted_files": ["agents/old.agent.md"]},
        extra_files={
            f"after/agents/prompt-{index}.md": f"prompt {index}\n"
            for index in range(4)
        },
    )

    verdict = evaluate_auto_apply(config.paths, config, prop)

    assert not verdict["eligible"]
    assert "too_many_files:5>4" in verdict["reasons"]


def test_auto_apply_counts_declared_deletion_in_diff_limit(tmp_path):
    config = _config(tmp_path, auto_apply_enabled=True)
    target = tmp_path / "agents" / "old.agent.md"
    target.parent.mkdir(parents=True)
    target.write_text("".join(f"line {index}\n" for index in range(201)), encoding="utf-8")
    plan_id = _passed_plan_id(config.paths, tmp_path)
    prop = create_proposal(
        config.paths,
        kind="prompt_patch",
        summary="Large prompt deletion",
        initial_state="pending_review",
        validation_plan_id=plan_id,
        metadata={"deleted_files": ["agents/old.agent.md"]},
        extra_files={"after/agents/main.agent.md": "# Main Agent\n"},
    )

    verdict = evaluate_auto_apply(config.paths, config, prop)

    assert not verdict["eligible"]
    assert "diff_too_large:202>200" in verdict["reasons"]


def test_auto_apply_rejects_staged_symlink_before_reading_it(tmp_path):
    config = _config(tmp_path, auto_apply_enabled=True)
    plan_id = _passed_plan_id(config.paths, tmp_path)
    prop = _prompt_proposal(config.paths, plan_id)
    outside = tmp_path / "outside-prompt.md"
    outside.write_text("secret\n", encoding="utf-8")
    staged = prop.path / "after" / "agents" / "leak.agent.md"
    staged.symlink_to(outside)

    verdict = evaluate_auto_apply(config.paths, config, prop)

    assert not verdict["eligible"]
    assert "candidate_bundle_symlink" in verdict["reasons"]


def test_auto_apply_tick_applies_eligible_prompt_patch(tmp_path):
    config = _config(tmp_path, auto_apply_enabled=True)
    plan_id = _passed_plan_id(config.paths, tmp_path)
    prop = _prompt_proposal(config.paths, plan_id)

    result = auto_apply_tick(config.paths, config)

    assert result["applied"] and result["applied"][0]["proposal_id"] == prop.id
    assert result["applied"][0]["ok"], result
    target = config.paths.root / "agents" / "main.agent.md"
    assert target.read_text(encoding="utf-8").startswith("# Main Agent")
    states = {p.id: p.state for p in list_proposals(config.paths)}
    assert states[prop.id] == "applied"


def test_auto_apply_lane_flag_is_protected_scope(tmp_path):
    paths = WorkspacePaths(tmp_path)
    with pytest.raises(ProtectedScopeViolation):
        propose_core_config_patch(
            paths,
            target="nerya.yml",
            summary="widen my own autonomy",
            config_after={"evolution": {"auto_apply": {"enabled": True}}},
            current_config={},
        )


def test_skills_enabled_is_a_valid_self_config_target(tmp_path):
    paths = WorkspacePaths(tmp_path)
    prop = propose_core_config_patch(
        paths,
        target="skills/enabled.yml",
        summary="enable the research skill",
        config_after={"enabled": ["trading", "research", "self_modify"]},
        current_config={"enabled": ["trading"]},
    )
    assert prop.kind == "core_config_patch"
    assert (prop.path / "after" / "skills" / "enabled.yml").exists()


def test_skills_enabled_direct_write_requires_proposal_tool():
    from nerya.tools.native.file_ops import _proposal_required_tools

    assert _proposal_required_tools("skills/enabled.yml") == [
        "evolve_core_config_patch"
    ]


# ------------------------------------------------------------------ blockers


def _real_money_profile():
    return SimpleNamespace(is_real_money=True, can_place_order=True)


def test_real_money_blocked_without_vault_passphrase(tmp_path, monkeypatch):
    monkeypatch.delenv("NERYA_VAULT_PASSPHRASE", raising=False)
    config = _config(tmp_path, auto_apply_enabled=False)
    blocker = _real_money_execution_blocker(config, _real_money_profile())
    assert blocker == "vault_passphrase_not_set"


def test_real_money_allowed_with_vault_passphrase(tmp_path, monkeypatch):
    from nerya.security import encryption

    if not encryption.has_strong_crypto():
        pytest.skip("cryptography not installed")
    monkeypatch.setenv("NERYA_VAULT_PASSPHRASE", "unit-test-passphrase")
    config = _config(tmp_path, auto_apply_enabled=False)
    assert _real_money_execution_blocker(config, _real_money_profile()) is None


def test_paper_profile_never_hits_crypto_blocker(tmp_path, monkeypatch):
    monkeypatch.delenv("NERYA_VAULT_PASSPHRASE", raising=False)
    config = _config(tmp_path, auto_apply_enabled=False)
    paper = SimpleNamespace(is_real_money=False, can_place_order=True)
    assert _real_money_execution_blocker(config, paper) is None
