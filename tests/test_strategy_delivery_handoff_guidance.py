"""Load the real authoring Skill and validate its producer handoff examples offline."""

import json
from pathlib import Path
import re

import pytest

from nerya.task_delivery import (
    DELIVERY_START_SCHEMA,
    DELIVERY_UPDATE_SCHEMA,
    validate_delivery_input,
)
from nerya.skills.registry import SkillRegistry
from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke
BUILTIN_ROOT = Path(__file__).resolve().parents[1] / "nerya" / "skills" / "builtin"


@pytest.fixture
def guidance():
    result = skill_tool_handler(
        ToolCall(name="Skill", arguments={"skill": "strategy_author"}),
        skill_index=SkillIndex([BUILTIN_ROOT]),
    )
    assert not result.is_error, result.text()
    return result.text().split("## Delivery contract\n", 1)[1].split("\n## ", 1)[0]


def test_lp_entry_reads_the_trusted_workflow_before_delivery_start():
    registry = SkillRegistry.load_builtin()
    index = SkillIndex([], registry_provider=lambda: registry)
    call = ToolCall(name="Skill", arguments={"skill": "strategy_author"})
    loaded = skill_tool_handler(call, skill_index=index)
    assert not loaded.is_error, loaded.text()
    entry = loaded.text().split("For LP requests", 1)[0]
    assert 'Skill(skill="strategy_author", file="references/delivery-contract.json")' in entry
    binding = json.loads(re.search(r'`(\{"skill_id":.*?\})`', entry).group(1))
    contract_page = skill_tool_handler(ToolCall(name="Skill", arguments={
        "skill": binding["skill_id"], "file": "references/delivery-contract.json",
    }), skill_index=index)
    assert not contract_page.is_error, contract_page.text()
    floors = json.loads(contract_page.content[0].text)
    trusted = index.delivery_contract(call, **binding)
    assert binding == {"skill_id": "strategy_author", "workflow_id": "strategy_authoring"}
    assert floors[binding["workflow_id"]]["required_checks"] == trusted["required_checks"]


def test_producer_handoff_omits_checks_and_preserves_unmet_evidence(guidance):
    text = " ".join(guidance.split())
    for instruction in (
        "A `source_asset` write's `content_hash` identifies the copy, not readback",
        "If readback is requested, call `read_file` on the saved file AFTER the write",
        "a pre-write read cannot support a final claim of readback verification",
        "Producer checks in `verification.checks` are already persisted",
        "omit `checks` entirely from `delivery_update`, including missing/failed outcomes",
        "retain those blockers in limitations and recovery as needed",
        "On continuation, read the latest producer result's `delivery_revision`",
        "use it as `expected_revision`, not the revision from before validation or replay",
        "Update only existing stages, artifacts, limitations and recovery as needed",
        "Keep workflow `required_checks` and `required_artifacts` intact",
        "Package structure and executed `behavioral_tests` are distinct",
        "structure passed does not satisfy missing, unrun or failed candidate tests",
        "If `strategy_validate` stops at a structural failure, `behavioral_tests` remains missing",
        "report that boundary from the actual result",
        "Completion requires `ok:true` and `completion.allowed:true`",
        "Use the update's returned `delivery_revision` for finalize's `expected_revision`",
    ):
        assert instruction in text, instruction


def test_finish_example_is_bounded_schema_valid_and_reuses_started_stage(guidance):
    examples = re.findall(r"```json\n(.*?)\n```", guidance, re.DOTALL)
    assert len(examples) == 2
    start, update = map(json.loads, examples)
    validate_delivery_input(start, DELIVERY_START_SCHEMA)
    validate_delivery_input(update, DELIVERY_UPDATE_SCHEMA)
    assert len(examples[1]) < 1024
    assert set(update) == {"expected_revision", "stages", "artifacts", "limitations"}
    assert update["limitations"]
    stages = {stage["id"]: stage for stage in start["stages"]}
    for stage in update["stages"]:
        assert stage["id"] in stages
        assert set(stage) == {"id", "state"}
        assert stage["state"] == "completed"
    required_artifacts = {
        artifact for stage in start["stages"] for artifact in stage.get("required_artifacts", [])
    }
    assert update["artifacts"]
    for artifact in update["artifacts"]:
        assert artifact["artifact_id"] in required_artifacts
        assert artifact["ref"] and artifact["kind"]
    assert "checks" not in update
    assert "passed" not in examples[1]
