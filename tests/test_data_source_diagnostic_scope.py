from __future__ import annotations

from types import SimpleNamespace

import pytest
from jsonschema import validate

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.data_sources import sync_state
from nerya.tools.native.data_sources import (
    DATA_SOURCE_STATUS_SCHEMA,
    data_source_status_handler,
)
from nerya.tools.types import ToolCall


@pytest.fixture
def config(tmp_path, monkeypatch):
    def no_sync(*args, **kwargs):
        pytest.fail("status inventory must not perform a live sync")

    monkeypatch.setattr(sync_state, "sync_now", no_sync)
    return Config(paths=WorkspacePaths(root=tmp_path), data={})


def _status(config, **arguments):
    result = data_source_status_handler(
        ToolCall(name="data_source_status", arguments=arguments), config=config
    )
    assert result.is_error is False
    return next(part.data for part in result.content if part.type == "json")


def test_unfiltered_stale_inventory_declares_only_ledger_observations(config):
    data = _status(config)
    scope = data["diagnostic_scope"]

    assert data["stale_count"] > 0
    assert scope["kind"] == "historical_sync_ledger"
    assert scope["live_probe_performed"] is False
    assert scope["provider_health"] == "not_checked"
    assert scope["observed_source_ids"] == sorted(
        row["source_id"] for row in data["sources"]
    )
    assert scope["observed_providers"] == ["ccxt", "filesystem", "paper", "registry"]
    assert scope["requested_provider"] is None
    assert scope["requested_provider_source_ids"] == []
    assert data["sources"] == data["summary"]["sources"]
    assert data["total"] == data["summary"]["total"]
    assert data["stale_count"] == data["summary"]["stale_count"]
    assert "events" not in data


def test_ccxt_error_cannot_establish_defillama_health(config):
    client = SimpleNamespace(config=config)
    sync_state.mark_attempt(client, "market:public_ccxt", provider="ccxt")
    sync_state.mark_error(client, "market:public_ccxt", message="transport failed")

    data = _status(config, provider="defillama", include_events=True, limit=1)
    scope = data["diagnostic_scope"]

    assert "market:public_ccxt" in data["summary"]["stale_ids"]
    assert scope["requested_provider"] == "defillama"
    assert scope["requested_provider_source_ids"] == []
    assert "defillama" not in scope["observed_providers"]
    assert scope["provider_health"] == "not_checked"
    assert scope["live_probe_performed"] is False
    assert len(data["events"]) == 1
    assert sync_state.get(client, "market:public_ccxt")["last_error"] == "transport failed"
    assert not any(row["provider"] == "defillama" for row in data["sources"])


@pytest.mark.parametrize("fresh", [False, True])
def test_present_provider_still_has_no_live_health_assessment(config, fresh):
    client = SimpleNamespace(config=config)
    source_id = "data_api:defillama:yield_pools"
    sync_state.mark_attempt(client, source_id, provider="defillama")
    if fresh:
        sync_state.mark_success(client, source_id)
    else:
        sync_state.mark_error(client, source_id, message="historical timeout")

    data = _status(config, provider="defillama")
    scope = data["diagnostic_scope"]

    assert "defillama" in scope["observed_providers"]
    assert scope["requested_provider_source_ids"] == [source_id]
    assert scope["provider_health"] == "not_checked"
    assert scope["live_probe_performed"] is False
    row = next(row for row in data["sources"] if row["source_id"] == source_id)
    assert row["stale"] is not fresh


def test_provider_matching_is_exact_and_case_insensitive(config):
    data = _status(config, provider="  CCXT  ")
    assert data["diagnostic_scope"]["requested_provider"] == "ccxt"
    assert data["diagnostic_scope"]["requested_provider_source_ids"] == [
        "market:public_ccxt"
    ]
    assert _status(config, provider="cc")["diagnostic_scope"][
        "requested_provider_source_ids"
    ] == []


def test_schema_and_result_warn_against_cross_provider_causal_claims(config):
    validate({}, DATA_SOURCE_STATUS_SCHEMA)
    validate({"provider": "defillama"}, DATA_SOURCE_STATUS_SCHEMA)
    assert "provider" in DATA_SOURCE_STATUS_SCHEMA["properties"]
    guidance = _status(config)["interpretation_guidance"]
    assert DATA_SOURCE_STATUS_SCHEMA["description"] == guidance
    for text in (
        "not a live endpoint probe",
        "stale_count",
        "CCXT",
        "DefiLlama",
        "not_checked",
        "transport failure",
    ):
        assert text in guidance
