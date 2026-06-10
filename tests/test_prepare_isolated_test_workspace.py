from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from tools.prepare_isolated_test_workspace import curate_config


pytestmark = pytest.mark.smoke


def test_prepare_isolated_workspace_defaults_permission_mode_to_yolo(tmp_path: Path) -> None:
    src = tmp_path / "source.yml"
    dst = tmp_path / "target" / "nerya.yml"
    src.write_text(
        yaml.safe_dump(
            {
                "runtime": {"live_trading_enabled": True},
                "api": {"host": "0.0.0.0", "port": 18317},
                "dashboard": {"host": "0.0.0.0", "port": 18380},
                "llm": {"tiers": {}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    curate_config(src, dst, api_port=18318, dashboard_port=3001)

    data = yaml.safe_load(dst.read_text(encoding="utf-8"))
    assert data["runtime"]["live_trading_enabled"] is False
    assert data["runtime"]["paper_trading_enabled"] is True
    assert data["runtime"]["permission_mode"] == "yolo"
    assert data["llm"]["context_log_mode"] == "full"


def test_prepare_isolated_workspace_strips_legacy_planner_routes(tmp_path: Path) -> None:
    src = tmp_path / "source.yml"
    dst = tmp_path / "target" / "nerya.yml"
    src.write_text(
        yaml.safe_dump(
            {
                "agent": {
                    "planner": {
                        "routes": {
                            "price_signal": {
                                "match": ["price.*"],
                                "skills": ["market_data"],
                            }
                        },
                        "fallback": "legacy",
                    }
                },
                "llm": {"tiers": {}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    curate_config(src, dst, api_port=18318, dashboard_port=3001)

    data = yaml.safe_load(dst.read_text(encoding="utf-8"))
    planner = data["agent"]["planner"]
    assert planner["manifest"] == "trading-v1"
    assert planner["routes"] == {}
    assert planner["fallback"] == "generic"
