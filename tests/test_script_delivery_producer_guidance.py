"""Offline Skill loading contracts; no runtime command or producer is executed."""

import json
import socket
from pathlib import Path

import pytest

from nerya.skills.registry import SkillRegistry, load_entry
from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke
ROOT = Path(__file__).resolve().parents[1]
BUILTIN = ROOT / "nerya" / "skills" / "builtin"


@pytest.fixture(params=["strategy_author", "backtest"])
def loaded_skill(request, monkeypatch):
    def deny_network(*args, **kwargs):
        pytest.fail("Network is outside this offline Skill loading contract")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    name = request.param
    registry = SkillRegistry()
    entry = load_entry(BUILTIN / name / "SKILL.md", source="builtin")
    assert entry is not None
    registry.register(entry)
    index = SkillIndex([], registry_provider=lambda: registry)
    result = skill_tool_handler(
        ToolCall(name="Skill", arguments={"skill": name}), skill_index=index,
    )
    assert not result.is_error, result.text()
    payload = next(part.data for part in result.content if part.type == "json")
    assert payload["skill_id"] == name
    workflow = "strategy_authoring" if name == "strategy_author" else "strategy_replay"
    contract = json.loads((BUILTIN / name / "references/delivery-contract.json").read_text())[workflow]
    return " ".join(result.text().split()), contract


def test_current_producer_receipt_owns_script_replay_evidence(loaded_skill):
    text, _ = loaded_skill
    for phrase in (
        "current `strategy_backtest` producer receipt", "`ok:true`",
        "`run_receipt`", "`report_path`", "`requested_window_complete:true`",
        "exact returned", "model prose", "handwritten `coverage_missing` diagnostic",
        "as a required check or required artifact",
        "optional, unverified historical diagnostic",
        "only when the current producer receipt reports a coverage gap",
    ):
        assert phrase in text


def test_artifact_aliases_bind_to_producer_refs_without_promoting_diagnostics(loaded_skill):
    text, contract = loaded_skill
    assert set(contract["required_artifacts"]) <= {"candidate", "report", "run_receipt"}
    for artifact, ref in (
        ("candidate", "source_binding.artifact_id"),
        ("report", "producer-returned report_path"),
        ("run_receipt", "producer-returned run_receipt"),
    ):
        assert f'"artifact_id":"{artifact}"' in text
        assert f'"ref":"<{ref}>"' in text
    assert '"revision":"<source_binding.revision>"' in text
    assert "Keep the same `report` artifact ID" in text
    assert "replace its diagnostic ref with the producer-returned `report_path`" in text
    assert "`report_output`" in text


def test_latest_revision_and_finalize_remain_the_completion_authority(loaded_skill):
    text, contract = loaded_skill
    assert "`delivery_revision`" in text and "`expected_revision`" in text
    assert "latest producer" in text
    assert "`completion.allowed:true`" in text
    assert "never set completion manually" in text
    assert "send model-authored passing checks to force it" in text
    assert {"data_coverage", "report_output"} <= set(contract["required_checks"])

