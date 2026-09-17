from copy import deepcopy
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import pytest
from nerya.core import yaml_io
from nerya.strategies.input_context import StrategyInputContext, collect_task_context, context_prompt
from nerya.strategies.agent_task import StrategyAgentTask
from nerya.strategies.agent_execution import validate_agent_configuration
from nerya.strategies.source_config import validate_source
from nerya.skills.builtin.backtest.scripts.backtest_run import _discover_strategy_timeframes

pytestmark = pytest.mark.smoke


def build(fail=None, **overrides):
    calls = []
    def candles(market, **kwargs):
        calls.append((market, kwargs))
        if fail and fail(market, kwargs["timeframe"]):
            raise RuntimeError("provider unavailable")
        return [{"market": market, "timeframe": kwargs["timeframe"], "close": 0, "confirmed": False}]
    source = {"id": "bars", "provider": "runtime.market", "capability": "candles", "markets": ["mock:BTC", "mock:ETH"], "timeframes": ["15m", "1h", "4h"], "limit": 120, **overrides}
    market = SimpleNamespace(candles=candles, ticker=lambda m, **kw: calls.append((m, kw)) or {"last": 0})
    return StrategyInputContext([source], market, None, ("mock:BTC",), "test-run"), calls


def test_matrix_captures_every_pair_and_preserves_zero_false():
    inputs, calls = build()
    result = inputs.source("bars")
    assert result["format"] == "market_series/v1"
    assert len(calls) == len(result["series"]) == 6
    assert {(m, k["timeframe"]) for m, k in calls} == {(m, tf) for m in ["mock:BTC", "mock:ETH"] for tf in ["15m", "1h", "4h"]}
    assert all(k["limit"] == 120 for _, k in calls)
    assert all(r["value"][0]["close"] == 0 and r["value"][0]["confirmed"] is False for r in result["series"])
    inputs.source("bars", market="mock:ETH", timeframe="4h")[0]["close"] = 999
    assert inputs.source("bars", market="mock:ETH", timeframe="4h")[0]["close"] == 0
    assert len(calls) == 6


def test_concurrent_readers_share_one_snapshot():
    inputs, calls = build()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: inputs.source("bars"), range(3)))
    assert len(calls) == 6
    assert results[0] == results[1] == results[2]
    assert inputs.snapshot()["source:bars"]["run_id"] == "test-run"


@pytest.mark.parametrize("selectors", [{"market": "mock:BTC"}, {"timeframe": "1h"}, {"market": "unknown", "timeframe": "1h"}])
def test_selection_is_never_implicit(selectors):
    inputs, _ = build()
    with pytest.raises(ValueError, match="exactly one"):
        inputs.source("bars", **selectors)


def test_plural_one_keeps_envelope_and_legacy_scalar_keeps_list():
    inputs, calls = build(markets=["mock:BTC"], timeframes=["15m"])
    assert len(inputs.source("bars")["series"]) == 1
    legacy = StrategyInputContext([{"id": "old", "timeframe": "15m", "limit": 17}], inputs.market, None, ("mock:BTC",), "legacy")
    assert isinstance(legacy.source("old"), list)
    assert calls[-1][1]["limit"] == 17


def test_partial_failure_retains_successes_and_does_not_retry_hiddenly():
    inputs, calls = build(fail=lambda m, tf: m == "mock:ETH" and tf == "1h")
    task = StrategyAgentTask.dispatch(prompt="analyze")
    ctx = SimpleNamespace(inputs=inputs, trigger=SimpleNamespace(payload={}))
    collect_task_context(task, ctx, {"sources": ["bars"], "on_error": "continue"})
    rows = task.context["published"]["source:bars"]["value"]["series"]
    assert sum(r["status"] == "returned" for r in rows) == 5
    failed = next(r for r in rows if r["status"] == "error")
    assert "value" not in failed and "provider unavailable" in failed["error"]
    assert "bars" in task.context["input_errors"]
    with pytest.raises(ValueError, match="unavailable"):
        collect_task_context(StrategyAgentTask.dispatch(prompt="stop"), ctx, {"sources": ["bars"]})
    assert len(calls) == 6


def test_skip_does_not_prefetch_matrix():
    inputs, calls = build()
    collect_task_context(StrategyAgentTask.skip("no signal"), SimpleNamespace(inputs=inputs), {"sources": ["bars"]})
    assert calls == []


@pytest.mark.parametrize("patch", [{"markets": []}, {"markets": "BTC"}, {"markets": ["BTC", "BTC"]}, {"timeframes": [""]}, {"timeframes": ["15m", "15m"]}, {"timeframes": ["1h"], "timeframe": "15m"}, {"limit": 0}, {"limit": True}, {"limit": 1.2}])
def test_invalid_dimensions_block_manifest(patch):
    with pytest.raises(ValueError):
        validate_agent_configuration({"data_sources": [{"id": "bars", **patch}]})


def test_news_and_ticker_do_not_fake_candle_periods():
    with pytest.raises(ValueError): validate_source({"provider": "runtime.news", "markets": ["BTC"]})
    with pytest.raises(ValueError): validate_source({"provider": "runtime.market", "capability": "ticker", "timeframes": ["1h"]})
    inputs, calls = build(capability="ticker")
    inputs.sources[0].pop("timeframes")
    out = inputs.source("bars")
    assert len(out["series"]) == len(calls) == 2
    assert all(row["timeframe"] is None for row in out["series"])


def test_configured_parameters_and_account_are_forwarded():
    inputs, calls = build(limit=17, account="paper-reader")
    inputs.source("bars")
    assert all(kw["limit"] == 17 and kw["account"] == "paper-reader" for _, kw in calls)


def test_all_source_periods_are_discovered_by_replay(tmp_path):
    yaml_io.dump(tmp_path / "strategy.yml", {"data_sources": [{"id": "bars", "timeframes": ["15m", "1h", "4h"]}, {"id": "daily", "timeframe": "1d"}]})
    assert _discover_strategy_timeframes(tmp_path) == ["15m", "1h", "4h", "1d"]


def test_agent_context_contains_all_actual_series_and_published_output(tmp_path):
    inputs, _ = build()
    inputs.publish("calculation", {"score": 0, "ready": False})
    task = StrategyAgentTask.dispatch(prompt="ORIGINAL TASK", context={"from_script": 0})
    collect_task_context(task, SimpleNamespace(inputs=inputs, trigger=SimpleNamespace(payload={})), {"sources": ["bars"]})
    package = SimpleNamespace(root=tmp_path, strategy_id="matrix", manifest=SimpleNamespace(extras={}))
    prompt = context_prompt(task, package, "run")
    assert all(term in prompt for term in ["mock:BTC", "mock:ETH", "15m", "1h", "4h", '"score": 0', '"ready": false'])
    assert len(task.context["published"]["source:bars"]["value"]["series"]) == 6
