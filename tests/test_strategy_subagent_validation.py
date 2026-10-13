from __future__ import annotations

import pytest

from nerya.strategies.validator import validate_proposal_files


pytestmark = pytest.mark.smoke


def _files(*, mode: str = "paper", require_subagent: bool = False) -> dict[str, str]:
    require_text = "true" if require_subagent else "false"
    return {
        "strategy.yml": f"""
version: 1
strategy_id: no_subagent_required
title: No Subagent Required
mode: {mode}
entrypoint: main.py:run
markets: ["paper:BTCUSDT"]
accounts: [paper_main]
schedule:
  type: cron
  cron: "*/5 * * * *"
  enabled: true
policy:
  allow_direct_order: true
  require_subagent_before_order: {require_text}
  max_single_order_usd: 100
  min_confidence: 0.5
llm_policy:
  default_tier: light
  allowed_tiers: [light]
  max_calls_per_run: 1
subagents: []
""",
        "main.py": "def run(ctx):\n    return {'decision': 'HOLD'}\n",
    }


def _files_with_main(main_py: str, *, agent_task: bool = False) -> dict[str, str]:
    files = _files()
    if agent_task:
        files["strategy.yml"] += "\nagent_task:\n  enabled: true\n"
    files["main.py"] = main_py
    return files


def test_missing_subagent_is_not_a_validation_blocker() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files(require_subagent=True),
    )

    assert result.ok
    codes = {issue.code for issue in result.issues}
    assert "missing_required_subagent" not in codes


def test_live_strategy_without_subagent_is_not_warned() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files(mode="live"),
    )

    assert result.ok
    codes = {issue.code for issue in result.issues}
    assert "live_without_subagent" not in codes


def test_backtest_incompatible_strategy_context_surfaces_are_blocked() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files_with_main(
            "\n".join(
                [
                    "def run(context):",
                    "    candles = context.market_data.get_candles(market='paper:BTCUSDT', interval='1h', count=50)",
                    "    account_id = context.account_id",
                    "    account = context.portfolio.get_account(account_id)",
                    "    positions = context.portfolio.get_positions('paper:BTCUSDT')",
                    "    return {'decision': 'HOLD', 'rows': len(candles), 'positions': len(positions), 'account': account_id}",
                ]
            )
        ),
    )

    assert not result.ok
    blockers = [issue for issue in result.blockers if issue.code == "unsupported_strategy_context_surface"]
    assert len(blockers) == 4
    message = "\n".join(issue.message for issue in blockers)
    assert "ctx.market.candles" in message
    assert "ctx.portfolio.positions" in message
    assert "ctx.config.accounts[0]" in message


def test_strategy_config_mapping_and_parameter_accessors_are_supported() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files_with_main(
            "\n".join(
                [
                    "def run(ctx):",
                    "    params = ctx.config.get('params') or {}",
                    "    tf = ctx.config.timeframe",
                    "    p2 = ctx.config.params",
                    "    return ctx.result.hold(reason=str((params, tf, p2)))",
                ]
            )
        ),
    )

    assert result.ok, [issue.message for issue in result.blockers]


def test_mock_facade_configuration_is_not_misclassified_as_runtime_code():
    from nerya.strategies.validator import _walk_ast
    import ast
    source = "ctx.portfolio.positions.return_value = []\nctx.market.features.return_value = {}\n"
    assert not _walk_ast(ast.parse(source), where="tests/test_contract.py")
    assert _walk_ast(ast.parse(source), where="main.py")
    unsafe = _walk_ast(ast.parse("import requests\n"), where="tests/test_contract.py")
    assert any(issue.code == "forbidden_import" for issue in unsafe)


def test_self_relative_stop_that_cannot_trigger_is_blocked() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files_with_main(
            "\n".join(
                [
                    "def run(ctx):",
                    "    last_close = 100.0",
                    "    atr_now = 2.0",
                    "    atr_stop = last_close - 1.5 * atr_now",
                    "    if last_close <= atr_stop:",
                    "        return ctx.result.ok(reason='stop')",
                    "    return ctx.result.hold(reason='wait')",
                ]
            )
        ),
    )

    assert not result.ok
    issue = next(i for i in result.blockers if i.code == "self_relative_stop_condition")
    assert "can never trigger" in issue.message
    assert "entry price or persisted high-water" in issue.message


def test_generated_backtest_source_snapshots_do_not_revalidate_as_current_source() -> None:
    files = _files_with_main(
        "def run(ctx):\n"
        "    return ctx.result.hold(reason='current-source-ok')\n"
    )
    files[
        "backtests/20260927_060241/source/no_subagent_required/main.py"
    ] = (
        "def run(ctx):\n"
        "    params = ctx.config.get('params') or {}\n"
        "    last_close = 100.0\n"
        "    atr_stop = last_close - 2.0\n"
        "    if last_close <= atr_stop:\n"
        "        return ctx.result.ok(reason=str(params))\n"
        "    return ctx.result.hold(reason='old-broken-snapshot')\n"
    )

    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=files,
    )

    assert result.ok
    assert not any("backtests/20260927_060241" in issue.where for issue in result.issues)


def test_authored_freeform_backtest_script_is_still_static_scanned() -> None:
    files = _files_with_main(
        "def run(ctx):\n"
        "    return ctx.result.hold(reason='current-source-ok')\n"
    )
    files["backtests/research_backtest.py"] = (
        "import subprocess\n"
        "subprocess.run(['echo', 'bad'])\n"
    )

    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=files,
    )

    assert not result.ok
    assert any(
        issue.code in {"forbidden_import", "subprocess"}
        and issue.where == "backtests/research_backtest.py"
        for issue in result.blockers
    )


def test_invalid_candidate_backtest_defaults_fail_strategy_validation() -> None:
    files = _files_with_main(
        "def run(ctx):\n"
        "    return ctx.result.hold(reason='fixture')\n"
    )
    files["strategy.yml"] += """
backtest:
  warmup_bars: -1
  unknown_replay_knob: 42
"""

    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=files,
    )

    assert not result.ok
    issues = [issue for issue in result.blockers if issue.code == "backtest_config_invalid"]
    assert len(issues) == 1
    assert "unknown backtest setting" in issues[0].message


def test_backtest_incompatible_candle_facade_aliases_are_blocked() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files_with_main(
            "\n".join(
                [
                    "def run(ctx):",
                    "    candles = ctx.market.candles(interval='1d', count=100)",
                    "    closes = [c.close for c in candles]",
                    "    return {'decision': 'HOLD', 'close': closes[-1] if closes else 0}",
                ]
            )
        ),
    )

    assert not result.ok
    codes = {issue.code for issue in result.blockers}
    assert "unsupported_strategy_context_surface" in codes
    messages = "\n".join(issue.message for issue in result.blockers)
    assert "ctx.market.candles(market, timeframe=..., limit=...)" in messages
    assert "candle rows are dicts" in messages


def test_backtest_incompatible_indexed_candle_facade_aliases_are_blocked() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files_with_main(
            "\n".join(
                [
                    "def run(ctx):",
                    "    candles = ctx.market.candles('YAHOO:TSLA', timeframe='1d', limit=120)",
                    "    last = candles[-1]",
                    "    prev = candles[-2]",
                    "    return {'decision': 'HOLD', 'close': last.close, 'prev': prev.close}",
                ]
            )
        ),
    )

    assert not result.ok
    messages = "\n".join(issue.message for issue in result.blockers)
    assert "candle rows are dicts" in messages
    assert "last['close']" in messages or "last[\"close\"]" in messages
    assert "prev['close']" in messages or "prev[\"close\"]" in messages


def test_portfolio_positions_singleton_attribute_access_is_blocked() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files_with_main(
            "\n".join(
                [
                    "def run(ctx):",
                    "    pos = ctx.portfolio.positions('YAHOO:TSLA')",
                    "    qty = float(pos.qty) if pos else 0.0",
                    "    return {'decision': 'HOLD', 'qty': qty}",
                ]
            )
        ),
    )

    assert not result.ok
    messages = "\n".join(issue.message for issue in result.blockers)
    assert "ctx.portfolio.positions(market) returns a list" in messages
    assert "iterate positions or select a row" in messages


def test_result_builder_positional_arguments_are_blocked() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files_with_main(
            "\n".join(
                [
                    "def run(ctx):",
                    "    if not ctx.trigger.get('ready'):",
                    "        return ctx.result.skip('insufficient_history')",
                    "    return ctx.result.hold('no_signal', {'source': 'gate'})",
                ]
            )
        ),
    )

    assert not result.ok
    messages = "\n".join(issue.message for issue in result.blockers)
    assert "ctx.result.skip" in messages
    assert "ctx.result.hold" in messages
    assert "reason=" in messages
    assert "metadata=" in messages


def test_result_builder_trade_methods_are_blocked() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files_with_main(
            "\n".join(
                [
                    "def run(ctx):",
                    "    if ctx.trigger.get('exit'):",
                    "        return ctx.result.flatten(reason='event_window')",
                    "    if ctx.trigger.get('short'):",
                    "        return ctx.result.sell(size=1)",
                    "    return ctx.result.buy(size=1)",
                ]
            )
        ),
    )

    assert not result.ok
    messages = "\n".join(issue.message for issue in result.blockers)
    assert "ctx.result.buy" in messages
    assert "ctx.result.sell" in messages
    assert "ctx.result.flatten" in messages
    assert "ctx.trading.open_position" in messages
    assert "ctx.trading.close_position" in messages


def test_backtest_incompatible_market_features_property_is_blocked() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files_with_main(
            "\n".join(
                [
                    "def run(ctx):",
                    "    features = ctx.market.features",
                    "    funding = features.get('funding_rate')",
                    "    return {'decision': 'HOLD', 'funding': funding}",
                ]
            )
        ),
    )

    assert not result.ok
    blockers = [
        issue
        for issue in result.blockers
        if issue.code == "unsupported_strategy_context_surface"
    ]
    assert blockers
    messages = "\n".join(issue.message for issue in blockers)
    assert "ctx.market.features(market, timeframe=..., lookback=...)" in messages


def test_backtest_incompatible_market_facade_aliases_are_blocked() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files_with_main(
            "\n".join(
                [
                    "def run(ctx):",
                    "    market = ctx.market",
                    "    ticker = market.ticker(symbol='BTCUSDT')",
                    "    candles_1h = market.candles('1h', limit=24)",
                    "    closes = [c.close for c in candles_1h]",
                    "    features = market.features()",
                    "    funding = market.features(ctx.config.markets[0], symbol='BTCUSDT', feature='funding_history')",
                    "    for pos in ctx.portfolio.positions or []:",
                    "        pass",
                    "    return {'decision': 'HOLD', 'close': closes[-1] if closes else 0, 'features': features, 'funding': funding, 'ticker': ticker}",
                ]
            )
        ),
    )

    assert not result.ok
    blockers = [
        issue
        for issue in result.blockers
        if issue.code == "unsupported_strategy_context_surface"
    ]
    assert blockers
    messages = "\n".join(issue.message for issue in blockers)
    assert "ctx.market.ticker(market" in messages
    assert "ctx.market.candles(market, timeframe=..., limit=...)" in messages
    assert "ctx.market.features(market, timeframe=..., lookback=...)" in messages
    assert "ctx.portfolio.positions(market)" in messages
    assert "candle rows are dicts" in messages


def test_agent_task_constructor_unknown_keyword_is_validation_blocker() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files_with_main(
            "\n".join(
                [
                    "from nerya.strategies import StrategyAgentTask",
                    "",
                    "def run(ctx):",
                    "    return StrategyAgentTask(",
                    "        prompt='decide whether to trade the breakout',",
                    "        symbol='BTCUSDT',",
                    "        timeframe='15m',",
                    "    )",
                ]
            ),
            agent_task=True,
        ),
    )

    assert not result.ok
    messages = "\n".join(issue.message for issue in result.blockers)
    assert "StrategyAgentTask" in messages
    assert "dispatch" in messages
    assert "symbol" in messages
    assert "timeframe" in messages
    assert "unsupported keyword" in messages


def test_agent_task_skip_error_metadata_must_be_keyword_argument() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files_with_main(
            "\n".join(
                [
                    "from nerya.strategies import StrategyAgentTask",
                    "",
                    "def run(ctx):",
                    "    if ctx.trigger.get('bad_data'):",
                    "        return StrategyAgentTask.error('bad_data', {'source': 'market'})",
                    "    return StrategyAgentTask.skip('no_signal', {'close': 100})",
                ]
            ),
            agent_task=True,
        ),
    )

    assert not result.ok
    messages = "\n".join(issue.message for issue in result.blockers)
    assert "StrategyAgentTask.error" in messages
    assert "StrategyAgentTask.skip" in messages
    assert "metadata=" in messages
    assert "positional arguments" in messages


def test_proposal_validation_normalizes_crlf_before_temp_write() -> None:
    files = _files_with_main(
        "def run(ctx):\r\n"
        "    assert True, \\\r\n"
        "        'continued assertion'\r\n"
        "    return {'decision': 'HOLD'}\r\n"
    )
    files["tests/test_main.py"] = (
        "def test_contract():\r\n"
        "    assert True, \\\r\n"
        "        'continued assertion'\r\n"
    )

    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=files,
    )

    assert result.ok, result.asdict()


def test_private_result_import_error_points_to_public_sdk_surface() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files_with_main(
            "from nerya.strategies.result import StrategyAgentTask\n\n"
            "def run(ctx):\n"
            "    return StrategyAgentTask.dispatch(prompt='inspect evidence')\n"
        ),
    )

    assert not result.ok
    messages = "\n".join(issue.message for issue in result.blockers)
    assert "from nerya.strategies import StrategyContext" in messages
    assert "StrategyResult, StrategyAgentTask" in messages


def test_strategy_sdk_import_is_validation_blocker() -> None:
    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=_files_with_main(
            "from __future__ import annotations\n\n"
            "from typing import TYPE_CHECKING\n\n"
            "if TYPE_CHECKING:\n"
            "    from strategy_sdk import StrategyContext\n\n"
            "def run(ctx: 'StrategyContext'):\n"
            "    from strategy_sdk import StrategyAgentTask\n"
            "    return StrategyAgentTask.dispatch(prompt='inspect evidence')\n"
        ),
    )

    assert not result.ok
    blockers = [
        issue
        for issue in result.blockers
        if issue.code == "unsupported_strategy_sdk_import"
    ]
    assert blockers
    messages = "\n".join(issue.message for issue in blockers)
    assert "from nerya.strategies import StrategyContext" in messages
    assert "StrategyResult, StrategyAgentTask" in messages


def test_placeholder_market_is_validation_blocker() -> None:
    files = _files()
    files["strategy.yml"] = files["strategy.yml"].replace(
        'markets: ["paper:BTCUSDT"]',
        'markets: ["BYREAL_ONCHAIN:solana:UNKNOWN"]',
    )

    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=files,
    )

    assert not result.ok
    assert any(issue.code == "placeholder_market" for issue in result.blockers)


def test_provider_universe_market_is_allowed_for_runtime_scanner() -> None:
    files = _files()
    files["strategy.yml"] = files["strategy.yml"].replace(
        'markets: ["paper:BTCUSDT"]',
        'markets: ["BYREAL_ONCHAIN:solana"]',
    )

    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=files,
    )

    assert result.ok, result.asdict()


def test_agent_task_dispatch_prompt_can_offer_skip_as_agent_decision() -> None:
    files = _files_with_main(
        "from nerya.strategies import StrategyAgentTask\n\n"
        "def run(ctx):\n"
        "    return StrategyAgentTask.dispatch(\n"
        "        prompt='Decide whether this setup is long, short, skip, or error.'\n"
        "    )\n"
    )
    files["strategy.yml"] += """
execution_mode: agent
agent_task:
  enabled: true
  mode: agent
"""

    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=files,
    )

    assert result.ok, result.asdict()


def test_agent_task_non_dispatch_branch_must_use_skip_status() -> None:
    files = _files_with_main(
        "from nerya.strategies import StrategyAgentTask\n\n"
        "def run(ctx):\n"
        "    if not ctx.market.candles('paper:BTCUSDT', timeframe='1h', limit=50):\n"
        "        return ctx.result.hold()\n"
        "    return StrategyAgentTask.dispatch(prompt='conditions passed')\n"
    )
    files["strategy.yml"] += """
execution_mode: agent
agent_task:
  enabled: true
  mode: agent
"""

    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=files,
    )

    assert not result.ok
    assert any(issue.code == "agent_task_skip_status" for issue in result.blockers)
    messages = "\n".join(issue.message for issue in result.blockers)
    assert "StrategyAgentTask.skip" in messages
    assert "ctx.result.hold" in messages


def test_context_agent_task_attribute_is_validation_blocker() -> None:
    files = _files_with_main(
        "_ctx = None\n\n"
        "def run(ctx):\n"
        "    global _ctx\n"
        "    _ctx = ctx\n"
        "    agent_task = _ctx.agent_task\n"
        "    return agent_task.dispatch(prompt='decide funding arb')\n"
    )
    files["strategy.yml"] += """
execution_mode: agent
agent_task:
  enabled: true
  mode: agent
"""

    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=files,
    )

    assert not result.ok
    assert any(
        issue.code == "unsupported_strategy_context_surface"
        for issue in result.blockers
    )
    messages = "\n".join(issue.message for issue in result.blockers)
    assert "ctx.agent_task is not part of StrategyContext" in messages
    assert "StrategyAgentTask.dispatch" in messages


def test_backtest_incompatible_context_feature_helpers_are_blocked() -> None:
    files = _files_with_main(
        "\n".join(
            [
                "def run(ctx):",
                "    if not ctx.market_available('paper:BTCUSDT'):",
                "        return ctx.result.skip(reason='missing_market')",
                "    close = ctx.feature('close')",
                "    if close:",
                "        return ctx.result.ok(reason='ready')",
                "    return ctx.result.hold(reason='no_edge')",
            ]
        )
    )

    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=files,
    )

    assert not result.ok
    blockers = [
        issue
        for issue in result.blockers
        if issue.code == "unsupported_strategy_context_surface"
    ]
    assert blockers
    messages = "\n".join(issue.message for issue in blockers)
    assert "ctx.market_available is not part of StrategyContext" in messages
    assert "ctx.feature is not part of StrategyContext" in messages
    assert "ctx.market.features" in messages


def test_agent_task_dispatch_unknown_keyword_is_validation_blocker() -> None:
    files = _files_with_main(
        "from nerya.strategies import StrategyAgentTask\n\n"
        "def run(ctx):\n"
        "    return StrategyAgentTask.dispatch(\n"
        "        prompt='conditions passed',\n"
        "        on_response=lambda decision, _ctx: {'decision': decision},\n"
        "    )\n"
    )
    files["strategy.yml"] += """
execution_mode: agent
agent_task:
  enabled: true
  mode: agent
"""

    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=files,
    )

    assert not result.ok
    blockers = [
        issue
        for issue in result.blockers
        if issue.code == "unsupported_agent_task_dispatch_argument"
    ]
    assert blockers
    assert "on_response" in blockers[0].message


def test_backtest_incompatible_portfolio_and_trading_surfaces_are_blocked() -> None:
    files = _files_with_main(
        "\n".join(
            [
                "def run(ctx):",
                "    for pos in ctx.portfolio.positions(ctx.config.markets[0]):",
                "        sym = pos.get('symbol', '')",
                "    ctx.portfolio.recent_trades(limit=20)",
                "    qty = ctx.market.round_qty(ctx.config.markets[0], 1.0)",
                "    ctx.trading.place_market_order(symbol=ctx.config.markets[0], side='sell', qty=qty)",
                "    return {'decision': 'HOLD'}",
            ]
        )
    )

    result = validate_proposal_files(
        strategy_id="no_subagent_required",
        files=files,
    )

    assert not result.ok
    blockers = [
        issue
        for issue in result.blockers
        if issue.code == "unsupported_strategy_context_surface"
    ]
    assert blockers
    messages = "\n".join(issue.message for issue in blockers)
    assert "StrategyPosition rows are dataclasses" in messages
    assert "ctx.portfolio.recent_trades is not part of StrategyContext" in messages
    assert "ctx.market.round_qty is not part of StrategyContext" in messages
    assert "ctx.trading.place_market_order is not part of StrategyContext" in messages

def test_research_sdk_only_allowed_in_recognized_replay_entrypoint():
    source = "from nerya.strategies.research import fetch_candles, write_json"+chr(10)
    files = _files_with_main("def run(ctx): return ctx.result.hold(reason='test')"+chr(10))
    files['scripts/freeform_backtest.py'] = source
    result = validate_proposal_files(strategy_id='no_subagent_required',files=files)
    assert result.ok
    files['main.py'] = source + files['main.py']
    result = validate_proposal_files(strategy_id='no_subagent_required',files=files)
    assert any(x.code == 'research_sdk_outside_replay' for x in result.blockers)


def test_research_sdk_rejects_live_context_and_artifact_escape(tmp_path, monkeypatch):
    from nerya.strategies.research import write_json
    monkeypatch.delenv('NERYA_FREEFORM_BACKTEST',raising=False)
    with pytest.raises(RuntimeError,match='replay'):write_json('result.json',{})
    monkeypatch.setenv('NERYA_FREEFORM_BACKTEST','1')
    monkeypatch.setenv('NERYA_STRATEGY_ROOT',str(tmp_path))
    monkeypatch.setenv('NERYA_BACKTEST_OUT_DIR',str(tmp_path/'backtests/run'))
    monkeypatch.setenv('NERYA_WORKSPACE',str(tmp_path))
    with pytest.raises(ValueError,match='escapes'):write_json('../escape.json',{})
    write_json('result.json',{'ok':True})
    assert (tmp_path/'backtests/run/result.json').exists()


def test_research_sdk_reads_only_declared_market_and_saved_entrypoint(tmp_path, monkeypatch):
    from nerya.strategies import research
    from nerya.data import candles
    from nerya.core import yaml_io
    monkeypatch.setenv('NERYA_FREEFORM_BACKTEST','1')
    monkeypatch.setenv('NERYA_STRATEGY_ROOT',str(tmp_path))
    monkeypatch.setenv('NERYA_BACKTEST_OUT_DIR',str(tmp_path/'backtests/run'))
    monkeypatch.setenv('NERYA_WORKSPACE',str(tmp_path))
    yaml_io.dump(tmp_path/'strategy.yml',{'markets':['PUBLIC:ASSET/QUOTE'],'entrypoint':'main.py:run'})
    (tmp_path/'main.py').write_text('def run(ctx): return 42'+chr(10))
    calls=[]
    monkeypatch.setattr(candles,'fetch_candles',lambda market,**kwargs:calls.append((market,kwargs)) or [{'ts':1}])
    assert research.entrypoint()(None)==42
    assert research.fetch_candles('PUBLIC:ASSET/QUOTE',interval='1h',start=1,end=3601,count=2)==[{'ts':1}]
    assert calls[0][1]['allow_mock'] is False
    assert calls[0][1]['config_like'].paths.root==tmp_path
    with pytest.raises(ValueError,match='saved candidate'):
        research.fetch_candles('OTHER:ASSET/QUOTE',interval='1h',start=1,end=3601,count=2)
    with pytest.raises(ValueError,match='bounds'):
        research.fetch_candles('PUBLIC:ASSET/QUOTE',interval='1h',start=1,end=7201,count=1)
    assert len(calls)==1
