"""Provider refresh and direct configuration preserve unrelated state."""
from copy import deepcopy

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.llm import ops
from nerya.llm.model_catalog import ModelCatalog
from nerya.security.secrets import SecretVault

pytestmark = pytest.mark.smoke


@pytest.fixture
def config(tmp_path):
    data = deepcopy(DEFAULT_CONFIG)
    data["llm"]["providers"] = {"fixture": {"base_url": "https://fixture.invalid/v1", "kind": "chat_completions"}}
    cfg = Config(paths=WorkspacePaths(tmp_path), data=data)
    ops.llm_config_set(cfg, providers=[{"provider": "fixture", "provider_key": "fixture-old-key"}])
    catalog = ModelCatalog(workspace=tmp_path)
    catalog.import_models(provider="fixture", models=[{"id": "retired"}])
    catalog.import_models(provider="other", models=[{"id": "untouched"}])
    return cfg


@pytest.mark.parametrize("models", [[{"id": "new-model", "context_length": 128000}], []])
def test_one_click_refresh_uses_saved_connection_and_persists_only_its_catalog(config, monkeypatch, models):
    calls = []
    class Adapter:
        def list_models(self, **kwargs):
            calls.append(kwargs)
            return models
    monkeypatch.setattr(ops, "builtin_providers", lambda: {"compat": Adapter()})
    before = deepcopy(config.data)
    config_bytes = config.paths.config.read_bytes()
    result = ops.models_provider_refresh(config, provider="fixture")
    assert result["ok"], result
    assert calls[0]["api_key"] == "fixture-old-key"
    assert calls[0]["base_url"] == "https://fixture.invalid/v1"
    assert [row["id"] for row in result["models"]] == [row["id"] for row in models]
    persisted = ModelCatalog(workspace=config.paths.root).load()
    assert [row["id"] for row in persisted["providers"]["fixture"]] == [row["id"] for row in models]
    assert persisted["providers"]["other"][0]["id"] == "untouched"
    assert config.data == before
    assert config.paths.config.read_bytes() == config_bytes
    assert "fixture-old-key" not in str(result)


def test_failed_refresh_keeps_previous_models_and_connection(config, monkeypatch):
    class Adapter:
        def list_models(self, **kwargs):
            raise RuntimeError("fixture-old-key must not escape")
    monkeypatch.setattr(ops, "builtin_providers", lambda: {"compat": Adapter()})
    before = deepcopy(config.data)
    path = ModelCatalog(workspace=config.paths.root).cache_path
    catalog_bytes = path.read_bytes()
    result = ops.models_provider_refresh(config, provider="fixture")
    assert not result["ok"]
    assert path.read_bytes() == catalog_bytes
    assert config.data == before
    assert "fixture-old-key" not in str(result)


def test_save_key_url_and_api_format_does_not_require_discovery(config, monkeypatch):
    monkeypatch.setattr(ops, "builtin_providers", lambda: pytest.fail("saving must not discover models"))
    before_tiers = deepcopy(config.get("llm.tiers"))
    catalog_bytes = ModelCatalog(workspace=config.paths.root).cache_path.read_bytes()
    result = ops.llm_config_set(config, providers=[{"provider": "fixture", "base_url": "https://changed.invalid/v1", "kind": "anthropic_messages", "provider_key": "fixture-new-key"}])
    profile = next(row for row in result["provider_profiles"] if row["provider"] == "fixture")
    assert profile["base_url"] == "https://changed.invalid/v1"
    assert profile["kind"] == "anthropic_messages"
    assert profile["provider_key_ref"].startswith("vault://")
    vault = SecretVault.open(config.paths.root / "vault" / "secrets.enc")
    assert vault.resolve(profile["provider_key_ref"].removeprefix("vault://"), required_scope="llm") == "fixture-new-key"
    assert "fixture-new-key" not in str(result)
    assert "fixture-new-key" not in config.paths.config.read_text()
    assert config.get("llm.tiers") == before_tiers
    assert ModelCatalog(workspace=config.paths.root).cache_path.read_bytes() == catalog_bytes


def test_blank_key_draft_preserves_saved_key(config):
    old_ref = config.get("llm.providers.fixture.provider_key_ref")
    ops.llm_config_set(config, providers=[{"provider": "fixture", "base_url": "https://changed.invalid/v1"}])
    assert config.get("llm.providers.fixture.provider_key_ref") == old_ref


def test_refresh_after_edit_uses_new_key_url_and_api_format(config, monkeypatch):
    ops.llm_config_set(config, providers=[{"provider": "fixture", "provider_key": "fixture-new-key", "base_url": "https://new.invalid", "kind": "anthropic_messages"}])
    calls = []
    class Adapter:
        def list_models(self, **kwargs):
            calls.append(kwargs)
            return [{"id": "new"}]
    monkeypatch.setattr(ops, "builtin_providers", lambda: {"anthropic": Adapter()})
    assert ops.models_provider_refresh(config, provider="fixture")["ok"]
    assert calls[0]["api_key"] == "fixture-new-key"
    assert calls[0]["base_url"] == "https://new.invalid"


def test_refresh_supports_legacy_route_credentials(config, monkeypatch):
    monkeypatch.setenv("PROVIDER_LEGACY_KEY", "fixture-env-key")
    config.data["llm"]["providers"] = {}
    config.data["llm"]["tiers"] = {"medium": {"routes": [{"provider": "fixture", "model": "existing", "base_url": "https://legacy.invalid", "provider_key_env": "PROVIDER_LEGACY_KEY", "kind": "chat_completions"}]}}
    calls = []
    class Adapter:
        def list_models(self, **kwargs):
            calls.append(kwargs)
            return [{"id": "legacy"}]
    monkeypatch.setattr(ops, "builtin_providers", lambda: {"compat": Adapter()})
    assert ops.models_provider_refresh(config, provider="fixture")["ok"]
    assert calls[0]["api_key"] == "fixture-env-key"


def test_refresh_endpoint_keeps_configuration_write_permission():
    from nerya.api.route_scopes import authorize
    assert authorize({"read:runtime"}, "POST", "/llm/models/provider-refresh")[0] is False
    assert authorize({"write:config"}, "POST", "/llm/models/provider-refresh")[0] is True


def test_explicit_api_shape_controls_discovery_for_builtin_provider_names(config, monkeypatch):
    class WrongAdapter:
        def list_models(self, **kwargs):
            pytest.fail("saved API format must override the builtin format")
    class RightAdapter:
        def list_models(self, **kwargs):
            return [{"id": "anthropic-shaped"}]
    monkeypatch.setattr(ops, "builtin_providers", lambda: {"openai": WrongAdapter(), "anthropic": RightAdapter()})
    before = deepcopy(config.data)
    result = ops.models_discover(config, provider="openai", base_url="https://compat.invalid", provider_key="fixture-key", api_mode="anthropic_messages", save_connection=False)
    assert result["ok"]
    assert result["models"][0]["id"] == "anthropic-shaped"
    assert config.data == before


def test_provider_edits_update_inherited_connections_and_preserve_model_overrides(config):
    from nerya.llm.gateway import LLMGateway
    old_ref = config.get("llm.providers.fixture.provider_key_ref")
    ops.llm_config_set(config, tiers=[{"tier": "medium", "routes": [
        {"provider": "fixture", "model": "inherited"},
        {"provider": "fixture", "model": "overridden", "base_url": "https://override.invalid", "provider_key_ref": old_ref},
    ]}])
    ops.llm_config_set(config, providers=[{"provider": "fixture", "base_url": "https://changed.invalid", "provider_key": "fixture-new-key"}])
    inherited, overridden = LLMGateway(config)._messages_route_cfgs("medium")
    assert inherited["base_url"] == "https://changed.invalid"
    assert inherited["_resolved_provider_key"] == "fixture-new-key"
    assert overridden["base_url"] == "https://override.invalid"
    assert overridden["_resolved_provider_key"] == "fixture-old-key"


def test_provider_refresh_honors_explicit_vault_passphrase(tmp_path, monkeypatch):
    cfg = Config(paths=WorkspacePaths(tmp_path), data=deepcopy(DEFAULT_CONFIG))
    ops.llm_config_set(cfg, providers=[{"provider": "fixture", "base_url": "https://fixture.invalid", "kind": "chat_completions", "provider_key": "fixture-encrypted-key"}], vault_passphrase="fixture-passphrase")
    class Adapter:
        def list_models(self, **kwargs):
            assert kwargs["api_key"] == "fixture-encrypted-key"
            return [{"id": "encrypted-model"}]
    monkeypatch.setattr(ops, "builtin_providers", lambda: {"compat": Adapter()})
    assert ops.models_provider_refresh(cfg, provider="fixture", vault_passphrase="fixture-passphrase")["ok"]


def test_unknown_provider_without_url_cannot_send_key_to_a_default_service(config, monkeypatch):
    monkeypatch.setattr(ops, "builtin_providers", lambda: pytest.fail("missing endpoint must not call an adapter"))
    before = deepcopy(config.data)
    result = ops.models_discover(config, provider="custom-without-url", provider_key="fixture-sensitive-key", save_connection=False)
    assert result["error"] == "provider_url_required"
    assert "fixture-sensitive-key" not in str(result)
    assert config.data == before
