from __future__ import annotations

from nerya.evolution.asset_policy import (
    validate_mutation_scope,
    validate_validation_command,
)
import pytest

pytestmark = pytest.mark.smoke


def test_asset_policy_rejects_protected_scope():
    result = validate_mutation_scope(["accounts/accounts.yml"])

    assert result.ok is False
    assert "protected_scope:accounts/accounts.yml" in result.reasons


def test_validation_command_allowlist():
    assert validate_validation_command("python -m pytest tests/test_x.py -q").ok
    blocked = validate_validation_command("powershell Remove-Item -Recurse state")
    assert blocked.ok is False
    assert blocked.reasons
