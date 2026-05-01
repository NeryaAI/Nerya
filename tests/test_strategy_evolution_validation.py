from __future__ import annotations

from nerya.evolution.validation_plan import build_validation_plan
from nerya.strategies.evolution import _filter_changes
import pytest

pytestmark = pytest.mark.smoke


class _Guardrails:
    max_patch_files = 2


class _Cfg:
    forbidden_targets = ["limits.yml", "accounts/*"]
    allowed_targets = ["main.py", "config.yml"]
    guardrails = _Guardrails()


def test_strategy_tuning_requires_validation_plan_when_changes_exist():
    output = {"proposed_changes": [{"file": "main.py", "kind": "code_patch"}]}
    accepted, _dropped, _warnings = _filter_changes(output, _Cfg())
    plan = build_validation_plan(output.get("validation_plan"), source="test", require=bool(accepted))

    assert accepted
    assert plan.status == "blocked"
    assert "validation_plan_required" in plan.blocked_reasons


def test_strategy_tuning_drops_forbidden_targets():
    output = {"proposed_changes": [{"file": "limits.yml", "kind": "config"}]}
    accepted, dropped, _warnings = _filter_changes(output, _Cfg())

    assert accepted == []
    assert dropped[0]["reason"] == "forbidden_target"
