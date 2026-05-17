from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import routes_operator
from nerya.core import yaml_io
from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core.paths import WorkspacePaths
from nerya.strategies import package as package_mod


pytestmark = pytest.mark.smoke


def _client(tmp_path):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    return SimpleNamespace(config=cfg)


def test_operator_strategy_package_count_does_not_load_full_packages(
    tmp_path,
    monkeypatch,
) -> None:
    paths = WorkspacePaths(root=tmp_path)
    yaml_io.dump(
        paths.strategy("alpha") / "strategy.yml",
        {
            "strategy_id": "alpha",
            "title": "Alpha",
            "mode": "paper",
        },
    )
    (paths.strategies / "scratch").mkdir(parents=True)

    def fail_load_packages(_paths):
        raise AssertionError("operator count must not validate/hash packages")

    monkeypatch.setattr(package_mod, "load_packages", fail_load_packages)

    assert routes_operator._strategy_package_count(_client(tmp_path)) == 1
