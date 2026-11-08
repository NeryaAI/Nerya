"""Offline first-call diagnostics; no runtime, model, or live workspace."""

from copy import deepcopy
import json

import pytest

from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core.paths import WorkspacePaths
from nerya.tools.native import workspace_ui as native_ui
from nerya.tools.types import ToolCall, ToolErrorKind
from nerya.workspace import ui

pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("diagnostics must not access network or apply a proposal")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr(ui, "apply", forbidden)


def _call(page_id="_market_panel"):
    # Reported failure shape: base 0, page + ticker/chart, venue/market, no source.
    return ToolCall(
        id="childacmd_39b28f24833a55d8d2764505",
        name="workspace_ui_propose",
        arguments={
            "summary": "Minimal market panel",
            "base_revision": 0,
            "operations": [
                {"op": "add_page", "page": {"id": page_id}},
                *[
                    {"op": "add_widget", "page": page_id, "widget": {
                        "id": widget_id, "kind": kind,
                        "config": {"venue": "binance", "market": "BTC/USDT"},
                    }}
                    for widget_id, kind in [("btc_quote", "market_ticker"),
                                            ("btc_chart", "chart")]
                ],
            ],
        },
    )


def _config(tmp_path):
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def test_first_malformed_proposal_has_actionable_diagnostics_without_retry(tmp_path):
    config = _config(tmp_path)
    call = _call()
    original = deepcopy(call.arguments)
    before = ui.read(config.paths)
    result = native_ui.workspace_ui_propose_handler(call, config=config)

    assert result.is_error
    error = result.error
    assert error.kind is ToolErrorKind.SCHEMA_VALIDATION
    assert error.retryable is False
    assert "manifest.pages[0].id" in error.message
    assert "must match [a-z][a-z0-9_-]*" in error.message
    assert result.text() == error.message
    assert error.detail["validation_errors"] == [{
        "field": "manifest.pages[0].id",
        "reason": "must match [a-z][a-z0-9_-]*",
    }]
    assert error.detail["supported_widget_kinds"] == sorted(ui.WIDGET_KINDS)
    assert "market_ticker" in error.message and "chart" in error.message
    assert "native-panels.md" in error.message
    assert "_safe_value" in error.detail["config_schema_pointer"]
    assert error.recovery_hint["action"] == "revise_operations_against_schema"
    assert "refresh_layout_and_retry" not in json.dumps(result.asdict())
    assert call.arguments == original
    assert ui.read(config.paths) == before
    assert not list(tmp_path.rglob("proposal.yml"))
    assert not (tmp_path / ui.UI_RELATIVE_PATH).exists()


def test_actual_dotted_widget_identifier_reports_field_not_stale_layout(tmp_path):
    call = _call('market_panel')
    call.arguments['operations'][1]['widget']['id'] = 'market_panel.btc_ticker'
    result = native_ui.workspace_ui_propose_handler(call, config=_config(tmp_path))
    assert result.is_error
    assert 'manifest.pages[0].widgets[0].id' in result.error.message
    assert 'must match [a-z][a-z0-9_-]*' in result.error.message
    assert 'market_panel.btc_ticker' not in result.error.message
    assert result.error.recovery_hint['action'] == 'revise_operations_against_schema'
    assert 'dots and leading' in native_ui.WORKSPACE_UI_PROPOSE_SCHEMA['properties']['operations']['description']


def test_manifest_diagnostics_redact_values_keys_and_bound_issue_count(tmp_path):
    call = _call("market_panel")
    secret = "private-user-content-token-XYZ"
    widgets = call.arguments["operations"][1:]
    widgets[0]["widget"]["kind"] = secret
    widgets[1]["widget"]["config"] = "private-config-value"
    call.arguments["operations"][0]["page"].update({
        f"{secret}-{i}": "private-value" for i in range(20)
    })
    result = native_ui.workspace_ui_propose_handler(call, config=_config(tmp_path))
    serialized = json.dumps(result.asdict())
    assert result.is_error
    assert secret not in serialized
    assert "private-config-value" not in serialized
    assert "private-value" not in serialized
    assert len(result.error.detail["validation_errors"]) <= 8
    assert result.error.detail["validation_errors_truncated"] is True
    assert len(result.error.message) < 3000
    assert len(serialized) < 6000
    assert not list(tmp_path.rglob("proposal.yml"))


@pytest.mark.parametrize("changes,field,reason", [
    ({"kind": "private-unknown-kind"}, "kind", "is not registered"),
    ({"config": "private-config-value"}, "config", "must be a mapping"),
    ({"config": {"private-unknown-key": {"html": "private-value"}}},
     "config.[redacted]", "is not allowed"),
    ({"source": {}}, "source", "needs id, action, or capability"),
    ({"read_only": False}, "read_only", "must be true"),
    ({"span": 99}, "span", "must be between 1 and 12"),
])
def test_generic_manifest_field_reasons_are_safe(tmp_path, changes, field, reason):
    call = _call("market_panel")
    call.arguments["operations"][1]["widget"].update(changes)
    original = deepcopy(call.arguments)
    result = native_ui.workspace_ui_propose_handler(call, config=_config(tmp_path))
    assert result.is_error
    assert result.error.detail["validation_errors"] == [{
        "field": f"manifest.pages[0].widgets[0].{field}", "reason": reason,
    }]
    assert "private-" not in json.dumps(result.asdict())
    assert call.arguments == original
    assert not list(tmp_path.rglob("proposal.yml"))


@pytest.mark.parametrize("revision,digest", [(99, None), (0, "wrong-digest")])
def test_conflict_keeps_refresh_recovery_and_cas(tmp_path, revision, digest):
    call = _call()
    call.arguments["base_revision"] = revision
    if digest:
        call.arguments["base_digest"] = digest
    result = native_ui.workspace_ui_propose_handler(call, config=_config(tmp_path))
    assert result.is_error
    assert result.error.kind is ToolErrorKind.CONFLICT
    assert result.error.retryable is True
    assert result.error.recovery_hint["action"] == "refresh_layout_and_retry"
    assert "validation_errors" not in result.error.detail
    assert not list(tmp_path.rglob("proposal.yml"))


def test_supported_kinds_config_and_internal_underscores_remain_review_only(tmp_path):
    config = _config(tmp_path)
    result = native_ui.workspace_ui_propose_handler(_call("market_panel"), config=config)
    assert not result.is_error, result.text()
    data = result.content[0].data
    assert data["state"] == "pending_review"
    assert data["applied"] is False
    assert data["preview_href"].endswith(data["proposal_id"])
    assert data["data_validation"]["status"] == "not_checked"
    assert not (tmp_path / ui.UI_RELATIVE_PATH).exists()
