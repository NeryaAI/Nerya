from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import routes_memory
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.memory import memsearch_index

pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def test_memsearch_vector_index_is_disabled_by_default(tmp_path):
    cfg = _config(tmp_path)

    out = memsearch_index.status(cfg)

    assert out["ok"] is True
    assert out["enabled"] is False
    assert out["backend"] == "memsearch"


def test_memsearch_install_refuses_when_disabled(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("install should not run while disabled")

    monkeypatch.setattr(memsearch_index.subprocess, "run", fake_run)

    out = memsearch_index.install_dependency(cfg)

    assert out["ok"] is False
    assert out["error"] == "vector_search_disabled"
    assert called is False


def test_memory_vector_config_route_enables_without_installing(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    client = SimpleNamespace(config=cfg)
    route_map = {(method, path): handler for method, path, handler in routes_memory.routes()}
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("configure must not install dependencies")

    monkeypatch.setattr(memsearch_index.subprocess, "run", fake_run)

    out = route_map[("POST", "/memory/vector/config")](
        client,
        {"enabled": True, "paths": ["memory"]},
    )

    assert out["ok"] is True
    assert out["enabled"] is True
    assert cfg.get("memory.vector_search.enabled") is True
    assert called is False

