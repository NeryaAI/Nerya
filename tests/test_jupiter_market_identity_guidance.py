"""Offline Skill guidance and real scope parsing; not model or replay acceptance."""
from copy import deepcopy
import json
from pathlib import Path
import re

import pytest

from nerya.strategies import request_scope as scope
from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke
BUILTINS = Path(__file__).parents[1] / "nerya/skills/builtin"
TOKEN_LOCATOR = "ONCHAIN:solana:So11111111111111111111111111111111111111112"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Network and real model calls are outside this guidance test")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("nerya.llm.gateway.LLMGateway.call_messages", forbidden)


def read_skill(index, **arguments):
    pages, offset = [], 0
    while True:
        result = skill_tool_handler(ToolCall(name="Skill", arguments={
            "skill": "strategy_author", **arguments, "offset": offset,
        }), skill_index=index)
        assert not result.is_error, result.text()
        pages.append(result.content[0].text)
        following = tool_json_data(result).get("next_offset")
        if following is None:
            return "\n".join(pages)
        assert following > offset
        offset = following


@pytest.fixture
def jupiter():
    index = SkillIndex([BUILTINS])
    playbook = read_skill(index)
    reference = "references/mainstream-strategies.md"
    assert reference in playbook
    text = read_skill(index, file=reference)
    return text.split("## Exact-source recovery for Jupiter SOL/USDC", 1)[1].split("\n## ", 1)[0]


def market_manifest(section):
    examples = [json.loads(block) for block in re.findall(r"```json\s*(.*?)\s*```", section, re.S)]
    manifest = next((example for example in examples if "markets" in example), None)
    assert manifest is not None, "Loaded Jupiter reference must supply the supported manifest market"
    return manifest


def contract():
    # Already-admitted typed scope; isolate storage/extraction, not resource parsing.
    return {"constraints": {"resources": [{"venue": "jupiter", "base": "SOL", "quote": "USDC"}],
                            "start_utc": 1735689600, "end_utc": 1738281600}}


def test_loaded_manifest_example_passes_real_resource_parser(jupiter):
    manifest = market_manifest(jupiter)
    assert manifest["markets"] == ["JUPITER:SOL/USDC"]
    assert scope.compare(contract(), manifest) == {
        "start_utc": "2025-01-01T00:00:00Z", "end_utc": "2025-01-31T00:00:00Z",
    }


def test_token_locator_rejected_then_same_request_repaired(jupiter, monkeypatch):
    admitted = contract()
    before = deepcopy(admitted)
    monkeypatch.setattr(scope, "ensure_contract", lambda *args, **kwargs: admitted)
    manifest = {"markets": [TOKEN_LOCATOR]}
    call = ToolCall(name="strategy_draft_proposal", arguments={"markets": [TOKEN_LOCATOR]})
    failure, pinned = scope.guard(None, call, manifest=manifest)
    assert failure.is_error and pinned == {}
    payload = tool_json_data(failure)
    assert payload["reason"] == "request_scope_mismatch"
    assert payload["diagnostic_code"] == "unverified_scope"
    hint = payload["next_required_action"]
    assert hint["action"] == "repair_candidate_resource"
    assert hint["preserve_candidate"] is True
    assert hint["differences"] == [
        {"field": "venue", "expected": "jupiter", "actual": None},
        {"field": "base", "expected": "SOL", "actual": None},
        {"field": "quote", "expected": "USDC", "actual": None},
    ]
    manifest.update(market_manifest(jupiter))
    call.arguments.update(manifest)
    failure, pinned = scope.guard(None, call, manifest=manifest)
    assert failure is None and pinned == scope.compare(admitted, manifest)
    assert admitted == before


@pytest.mark.parametrize("boundaries", [
    ("data locator", "token/USD", "USDC≈USD", "aggregator route"),
    ("first_ts", "last_ts", "pinned", "typed producers", "row count", "warmup"),
    ("candidate_market_unresolved", "same candidate request", "CCXT symbol enumeration", "workspace-wide"),
])
def test_loaded_identity_coverage_and_recovery_guardrails(jupiter, boundaries):
    # Instruction regression only: these assertions cannot prove model compliance.
    for boundary in boundaries:
        assert boundary.casefold() in jupiter.casefold()


def test_loaded_discovery_closes_established_alternative_source_mismatch():
    # Offline instruction/loadability regression, not proof of model compliance.
    text = read_skill(SkillIndex([BUILTINS]), skill="research",
                      file="references/onchain-data-discovery.md")
    disposition = text.split("### Discovery disposition:", 1)[1].split("\n## 4.", 1)[0]
    for requirement in (
        "close discovery immediately", "Orca pool", "Jupiter historical routes or fills",
        "Preserve the original protocol, market and", "window",
        "capture refs as artifacts", "coverage finding", "limitations",
        "only a\nproducer receipt", "`blocked`", "historical_observation_unavailable",
        "retain its required checks/artifacts", "`delivery_finalize`",
        "not a completed replay", "adapter/normalization and replay",
        "access authorization", "End the turn", "Resume discovery only when new",
    ):
        assert requirement in disposition
    assert "reloading a Skill does not reset this budget" in text
