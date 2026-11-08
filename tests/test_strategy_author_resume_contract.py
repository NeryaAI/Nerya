"""Offline instruction regression; producer implementation evidence is tested separately."""

from pathlib import Path

import pytest

from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke
BUILTIN_ROOT = Path(__file__).parents[1] / "nerya" / "skills" / "builtin"


def test_resume_reconciles_scaffold_before_replay_or_completion():
    index = SkillIndex([BUILTIN_ROOT])
    loaded = {}
    for file in (None, "references/authoring-workflow.md"):
        arguments = {"skill": "strategy_author"}
        if file:
            arguments["file"] = file
        result = skill_tool_handler(
            ToolCall(name="Skill", arguments=arguments), skill_index=index,
        )
        assert not result.is_error, result.text()
        loaded[file] = result.text()

    entry = " ".join(loaded[None].split())
    assert (
        "On continuation after interruption or compaction, read **Resume and completion "
        "boundary** in `references/authoring-workflow.md`"
    ) in entry

    # All recovery decisions must be loaded together, ahead of the normal sequence.
    recovery = loaded["references/authoring-workflow.md"].split(
        "## Resume and completion boundary\n", 1,
    )[1].split("\n## ", 1)[0]
    recovery = " ".join(recovery.split())
    for requirement in (
        "Delivery is command-bound",
        "Call `delivery_start` once for the current command using the original scope",
        "required evidence floor before any edit, validation or replay",
        "a prior command's delivery does not attach itself to the new command",
        "Read-only inspection does not call delivery_start/update/finalize or repeat producers",
        "Re-read the SAME `proposal_id` at its returned `proposal_paths`",
        "successful producer receipts in this task's admitted command history",
        "persisted delivery artifacts",
        "persisted delivery artifacts only to locate a candidate",
        "only a successful same-task `strategy_draft_proposal` producer receipt authorizes staged edits",
        "`ownership.same_task_producer`",
        "`ownership.recoverable`",
        "then call `strategy_draft_proposal` once",
        "Delivery updates cannot authorize edits",
        "global proposal search is not task ownership",
        "If every draft failed before creating a candidate",
        "reconcile that original operation before any new draft",
        "current `main.py`, helpers, `strategy.yml`, tests",
        "latest producer validation/replay receipts, source revision/package hash",
        "Resume the earliest unmet work in that candidate",
        "Passing scaffold structural checks or safe-hold tests do not establish",
        "`data_ready=False` / `decision=False`",
        "implement the requested data and decision branches in `run(ctx)`",
        "Missing producer implementation evidence remains unverified",
        "Finish implementation before replay",
        "Preserve the original requested history window",
        "trading/replay policy, including model-decision policy",
        "Reuse the native `data_api` prepared input and its returned path/manifest",
        "Do not guess URLs or substitute `run_shell`",
        "redirected shell output into `replay_inputs.json`",
        "`awaiting_approval`",
        "leave the pending approval untouched",
        "A request to continue is not approval",
        "no self-authorized retry loop, duplicate write or alternate tool route",
        "Producer-owned checks cannot be inserted as passed by `delivery_update`",
        "Missing, failed or stale checks stay unmet",
        "both `ok:true` and `completion.allowed:true`",
    ):
        assert requirement in recovery, requirement


def test_rate_recovery_requires_ownership_before_edit_or_redraft():
    index = SkillIndex([BUILTIN_ROOT])
    result = skill_tool_handler(ToolCall(name='Skill', arguments={
        'skill': 'strategy_author', 'file': 'references/rate-policy.md'}), skill_index=index)
    assert not result.is_error, result.text()
    words = ' '.join(result.text().split())
    for requirement in (
        'delivery artifacts only locate a candidate',
        'a successful same-task `strategy_draft_proposal` producer receipt authorizes staged edits',
        '`ownership.same_task_producer` and `ownership.recoverable`',
        'If no draft succeeded in this task, repair `invalid_resource_shape`',
        'then draft once',
        'Reconcile an uncertain operation before any new draft or edit',
    ):
        assert requirement in words


def test_lp_entry_pointer_names_real_loadable_assets():
    index = SkillIndex([BUILTIN_ROOT])
    entry = skill_tool_handler(ToolCall(name='Skill',arguments={'skill':'strategy_author'}),
                               skill_index=index).text()
    assert 'before drafting or recovering a candidate' in entry
    for name in ('lp_range_strategy','lp_range_policy','risk_rules','lp_rebalance'):
        asset = f'templates/{name}.py.j2'
        assert asset in entry
        result = skill_tool_handler(ToolCall(name='Skill',arguments={
            'skill':'strategy_author','file':asset}),skill_index=index)
        assert not result.is_error, result.text()
