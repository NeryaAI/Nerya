"""Execution is selected by one explicit manifest field, not names or permissions."""
from types import SimpleNamespace

import pytest

from nerya.strategies.agent_task_mode import agent_task_requested, agent_team_roles

pytestmark = pytest.mark.smoke


def _manifest(extras=None, roles=()):
    return SimpleNamespace(
        strategy_id="equity_agent_team", title="Agent Team valuations", description="fundamental earnings",
        markets=["YAHOO:EXAMPLE"], subagents=list(roles), extras=extras or {},
        agent_profile=SimpleNamespace(allowed_tools=["team_run"]),
    )


def test_names_and_tool_permissions_do_not_select_execution_mode():
    assert not agent_task_requested(_manifest(roles=["researcher"]))
    assert not agent_task_requested(_manifest({"agent_task": {"enabled": False}}))
    assert agent_task_requested(_manifest({"agent_task": {"enabled": True}}))


def test_roles_are_only_the_explicit_assignment():
    assert agent_team_roles(_manifest()) == []
    assert agent_team_roles(_manifest(roles=["researcher", "researcher", "risk_reviewer"])) == ["researcher", "risk_reviewer"]


@pytest.mark.parametrize("value", ["agent", True, {"enabled": "false"}])
def test_noncanonical_execution_contract_is_rejected(value):
    with pytest.raises(ValueError):
        agent_task_requested(_manifest({"agent_task": value}))
