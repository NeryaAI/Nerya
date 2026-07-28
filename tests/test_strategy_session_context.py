from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.agent.kernel import AgentKernel
from nerya.agent.session_profile import render_strategy_context_block
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.trading import strategy_crud


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def _create_strategy(cfg: Config, sid: str = "btc_scalper") -> None:
    strategy_crud.create(
        cfg.paths,
        strategy_crud.CreateRequest(
            strategy_id=sid,
            title="BTC Scalper",
            description="Scalps BTC on 1m candles.",
            markets=("bybit:BTCUSDT",),
            status="paper",
            main_prompt="Trade small and honour the risk gate.",
        ),
    )


def test_render_strategy_context_block_includes_package_files(tmp_path) -> None:
    cfg = _config(tmp_path)
    _create_strategy(cfg)

    block = render_strategy_context_block(cfg.paths, "btc_scalper")

    assert "Strategy Context (strategy_id=btc_scalper)" in block
    assert "- title: BTC Scalper" in block
    assert "strategy.yml:" in block
    assert "authoritative configuration" in block


def test_render_strategy_context_block_missing_strategy_is_empty(tmp_path) -> None:
    cfg = _config(tmp_path)

    assert render_strategy_context_block(cfg.paths, "ghost") == ""
    assert render_strategy_context_block(cfg.paths, None) == ""
    assert render_strategy_context_block(cfg.paths, "  ") == ""


def test_render_strategy_context_block_clips_to_max_chars(tmp_path) -> None:
    cfg = _config(tmp_path)
    _create_strategy(cfg)

    block = render_strategy_context_block(
        cfg.paths, "btc_scalper", max_chars=120,
    )

    assert "…[truncated]" in block
    # The re-read guidance survives clipping so the agent can always
    # recover the full files with tools.
    assert "authoritative configuration" in block


def test_system_prompt_carries_strategy_context_for_bound_sessions(tmp_path) -> None:
    cfg = _config(tmp_path)
    _create_strategy(cfg)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]

    prompt = kernel._build_system_prompt(
        kernel._ensure_registry(),
        strategy_id="btc_scalper",
        session_id="sess_strategy",
    )

    assert "Strategy Context (strategy_id=btc_scalper)" in prompt
    assert "- title: BTC Scalper" in prompt


def test_system_prompt_skips_strategy_context_without_binding(tmp_path) -> None:
    cfg = _config(tmp_path)
    _create_strategy(cfg)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]

    prompt = kernel._build_system_prompt(
        kernel._ensure_registry(),
        session_id="sess_plain",
    )

    assert "Strategy Context (strategy_id=" not in prompt
