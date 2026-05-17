from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import routes_strategy
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core import yaml_io
from nerya.strategies.scheduler_bridge import (
    TRADING_KIND,
    TRADING_TARGET,
    trading_schedule_id,
)
from nerya.trading import strategy_crud
from nerya.trading.position_book import PositionBook
from nerya.triggers.schedule import ScheduleEntry, load_schedules, save_schedules

pytestmark = pytest.mark.smoke


def _client(tmp_path):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    return SimpleNamespace(config=cfg)


def _create_strategy(paths: WorkspacePaths, strategy_id: str) -> None:
    strategy_crud.create(
        paths,
        strategy_crud.CreateRequest(
            strategy_id=strategy_id,
            title=f"{strategy_id} title",
            account_id="paper_main",
            markets=("BINANCE:BTCUSDT",),
            trigger_kinds=("schedule",),
            status="paper",
        ),
    )


def _create_paper_account(paths: WorkspacePaths) -> None:
    yaml_io.dump(
        paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "paper_main",
                    "exchange": "mock",
                    "venue": "mock",
                    "mode": "paper",
                    "status": "active",
                    "initial_balance_usd": 10_000,
                    "permissions": {
                        "read_balances": True,
                        "place_order": True,
                        "cancel_order": True,
                    },
                }
            ]
        },
    )


def test_strategy_delete_removes_package_and_schedules(tmp_path):
    client = _client(tmp_path)
    paths = client.config.paths
    _create_strategy(paths, "alpha")
    save_schedules(paths, [
        ScheduleEntry(
            id=trading_schedule_id("alpha"),
            kind=TRADING_KIND,
            target=TRADING_TARGET,
            strategy_id="alpha",
            every_seconds=60,
            payload={"strategy_id": "alpha"},
        ),
    ])

    route_map = {(method, path): handler for method, path, handler in routes_strategy.routes()}
    handler = route_map[("POST", "/strategy/delete")]
    res = handler(client, {"strategy_id": "alpha"})

    assert res["ok"] is True
    assert res["deleted"] is True
    assert trading_schedule_id("alpha") in res["removed_schedules"]
    assert not paths.strategy("alpha").exists()
    assert load_schedules(paths) == []


def test_strategy_delete_blocks_on_open_positions_unless_forced(tmp_path):
    client = _client(tmp_path)
    paths = client.config.paths
    _create_strategy(paths, "beta")
    PositionBook(paths).apply_fill(
        account_id="paper_main",
        strategy_id="beta",
        market="BINANCE:BTCUSDT",
        side="buy",
        price=100.0,
        size_base=1.0,
        source="paper",
    )

    route_map = {(method, path): handler for method, path, handler in routes_strategy.routes()}
    handler = route_map[("POST", "/strategy/delete")]

    blocked = handler(client, {"strategy_id": "beta"})
    assert blocked["ok"] is False
    assert blocked["error"] == "strategy_has_active_state"
    assert blocked["state"]["open_positions"] == 1
    assert paths.strategy("beta").exists()

    forced = handler(client, {"strategy_id": "beta", "force": True})
    assert forced["ok"] is True
    assert forced["deleted"] is True
    assert not paths.strategy("beta").exists()


def test_strategy_close_positions_previews_and_flattens_before_delete(tmp_path):
    client = _client(tmp_path)
    paths = client.config.paths
    _create_paper_account(paths)
    _create_strategy(paths, "gamma")
    limits_path = paths.strategy("gamma") / "limits.yml"
    limits = yaml_io.load(limits_path, default={})
    limits["approval_threshold_usd"] = 1
    yaml_io.dump(limits_path, limits)
    opened = PositionBook(paths).apply_fill(
        account_id="paper_main",
        strategy_id="gamma",
        market="BINANCE:BTCUSDT",
        side="buy",
        price=100.0,
        size_base=1.0,
        source="paper",
    )
    strategy_crud.set_status(paths, "gamma", "paused", reason="test")

    route_map = {(method, path): handler for method, path, handler in routes_strategy.routes()}
    close_handler = route_map[("POST", "/strategy/close_positions")]
    delete_handler = route_map[("POST", "/strategy/delete")]

    preview = close_handler(client, {"strategy_id": "gamma", "dry_run": True})
    assert preview["ok"] is True
    assert preview["count"] == 1
    assert preview["positions"][0]["position_id"] == opened.position_id

    blocked = delete_handler(client, {"strategy_id": "gamma"})
    assert blocked["ok"] is False
    assert blocked["state"]["open_positions"] == 1

    closed = close_handler(client, {"strategy_id": "gamma"})
    assert closed["ok"] is True
    assert closed["submitted"][0]["status"] == "filled"
    assert closed["remaining_state"]["open_positions"] == 0
    assert PositionBook(paths).open_positions(strategy_id="gamma") == []

    deleted = delete_handler(client, {"strategy_id": "gamma"})
    assert deleted["ok"] is True
    assert deleted["deleted"] is True
