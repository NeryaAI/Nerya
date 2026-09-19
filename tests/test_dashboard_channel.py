"""Bundled frontend/backend channel setup, without weakening public auth."""
from copy import deepcopy
import pytest

pytestmark = pytest.mark.smoke
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.cli.commands.core import _configure_dashboard_channel


def test_bundled_channel_is_ephemeral_and_shared(tmp_path, monkeypatch):
    monkeypatch.delenv('NERYA_DASHBOARD_INTERNAL_TOKEN', raising=False)
    cfg = Config(paths=WorkspacePaths(tmp_path), data=deepcopy(DEFAULT_CONFIG))
    before = deepcopy(cfg.data)
    _configure_dashboard_channel(cfg)
    import os
    first = os.environ['NERYA_DASHBOARD_INTERNAL_TOKEN']
    assert len(first) >= 40
    _configure_dashboard_channel(cfg)
    assert os.environ['NERYA_DASHBOARD_INTERNAL_TOKEN'] == first
    assert cfg.data == before and not list(tmp_path.iterdir())


def test_explicit_channel_configuration_is_preserved(tmp_path, monkeypatch):
    import os
    monkeypatch.delenv('NERYA_DASHBOARD_INTERNAL_TOKEN', raising=False)
    data = deepcopy(DEFAULT_CONFIG)
    data.setdefault('runtime', {}).setdefault('auth', {})['dashboard_internal_token'] = 'fixture-config'
    cfg = Config(paths=WorkspacePaths(tmp_path), data=data)
    _configure_dashboard_channel(cfg)
    assert os.environ['NERYA_DASHBOARD_INTERNAL_TOKEN'] == 'fixture-config'
    monkeypatch.setenv('NERYA_DASHBOARD_INTERNAL_TOKEN', 'fixture-environment')
    _configure_dashboard_channel(cfg)
    assert os.environ['NERYA_DASHBOARD_INTERNAL_TOKEN'] == 'fixture-environment'
