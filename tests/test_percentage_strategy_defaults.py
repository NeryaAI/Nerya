"""Execute percentage-sizing defaults; fixture prices are NOT alpha evidence."""
from __future__ import annotations

from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from nerya.core import yaml_io
from nerya.core.paths import WorkspacePaths
from nerya.evolution.strategy_code_generator import StrategyCodeGenerator, StrategyGenerationRequest
from nerya.skills.builtin.backtest.scripts.config import load_config
from nerya.skills.builtin.backtest.scripts.engine import run_backtest, settle
from nerya.skills.builtin.backtest.scripts.portfolio import PortfolioState
from nerya.skills.manifest import SkillManifest
from nerya.strategies.configuration_contract import configuration_issues
from nerya.strategies.signal_runner import run_signal_strategy
from nerya.trading.capital import _resolve_notional
from nerya.trading.order_intents import SizingPolicy

pytestmark = pytest.mark.smoke
BUILTIN = Path(__file__).resolve().parents[1] / "nerya/skills/builtin"
MARKET = "MOCK:A"
START = 1700006400


def generate(tmp_path, **kwargs):
    request = StrategyGenerationRequest(
        strategy_id="percent_fixture", markets=(MARKET,), accounts=("paper_main",),
        create_tuning=False, **kwargs,
    )
    result = StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(
        request, validate=True, create_proposal_record=False,
    )
    assert result.validation.ok, result.validation.asdict()
    return result.files, yaml_io.loads(result.files["strategy.yml"])


@pytest.mark.parametrize("kind", ["scalping", "trend", "news", "agent", "agent_team"])
def test_all_generated_archetypes_use_saved_percentage_not_dollar_examples(tmp_path, kind):
    files, manifest = generate(tmp_path, strategy_class=kind)
    assert manifest["params"]["sizing"] == {"method": "pct_nav", "pct_nav": 0.9}
    assert manifest["policy"]["max_single_order_usd"] == 0
    assert manifest["policy"]["max_daily_notional_usd"] == 0
    assert manifest["backtest"]["max_open_trades"] == manifest["policy"]["max_open_positions"] == 1
    assert manifest["backtest"]["stake_amount"] == {"mode": "unlimited"}
    source = files["main.py"]
    assert re.search(r"sizing=ctx.config.params\[['\"]sizing['\"]\]", source)
    assert "ctx.policy.default_order_usd" not in source
    assert '"size": 100' not in source
    if kind in {"agent", "agent_team"}:
        assert "size_pct_nav" in source and "risk_check" in source
        assert "json.dumps(ctx.config.params['sizing']" in source or "'sizing': ctx.config.params['sizing']" in source


@pytest.mark.parametrize("count,slots", [(1, 1), (2, 2), (3, 3), (10, 3)])
def test_multi_market_budget_is_split_by_concurrent_slots(tmp_path, count, slots):
    request = StrategyGenerationRequest(
        strategy_id="basket_fixture", markets=tuple(f"MOCK:A{i}" for i in range(count)),
        accounts=("paper_main",), create_tuning=False,
    )
    files = StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(
        request, validate=True, create_proposal_record=False,
    ).files
    manifest = yaml_io.loads(files["strategy.yml"])
    assert manifest["policy"]["max_open_positions"] == manifest["backtest"]["max_open_trades"] == slots
    assert manifest["params"]["sizing"]["pct_nav"] * slots == pytest.approx(0.9)


def test_explicit_fixed_order_and_risk_limits_win(tmp_path):
    limits = {"default_order_usd": 100, "max_single_order_usd": 125,
              "max_daily_notional_usd": 400, "max_open_positions": 2}
    _, manifest = generate(tmp_path, policy_overrides=limits)
    assert manifest["params"]["sizing"] == {"method": "fixed_usd", "fixed_usd": 100}
    assert all(manifest["policy"][key] == value for key, value in limits.items())
    assert manifest["backtest"]["max_open_trades"] == 2


def test_inline_percentage_and_replay_override_are_not_rewritten(tmp_path):
    inline = {"params": {"lookback": 300, "sizing": {"method": "pct_nav", "pct_nav": 0.12}},
              "policy": {"max_single_order_usd": 100, "max_open_positions": 2},
              "backtest": {"max_open_trades": 1, "stake_amount": {"mode": "fixed", "fixed_usd": 50}}}
    _, manifest = generate(tmp_path, files={"strategy.yml": yaml_io.dumps(inline)})
    assert manifest["params"] == inline["params"]
    assert manifest["backtest"] == inline["backtest"]
    assert manifest["policy"]["max_single_order_usd"] == 100


def test_inline_policy_slots_control_missing_sizing_not_earlier_scaffold(tmp_path):
    _, manifest = generate(tmp_path, files={"strategy.yml": yaml_io.dumps(
        {"policy": {"max_open_positions": 3}, "params": {"lookback": 250}})})
    assert manifest["params"]["sizing"]["pct_nav"] == pytest.approx(0.3)
    assert manifest["params"]["lookback"] == 250
    assert manifest["backtest"]["max_open_trades"] == 3


def test_ranked_winner_only_team_does_not_leave_two_slots_unused(tmp_path):
    request = StrategyGenerationRequest(strategy_id="winner_fixture", strategy_class="agent_team",
        markets=("MOCK:A", "MOCK:B", "MOCK:C"), accounts=("paper_main",), create_tuning=False)
    files = StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(
        request, create_proposal_record=False).files
    manifest = yaml_io.loads(files["strategy.yml"])
    assert manifest["policy"]["max_open_positions"] == manifest["backtest"]["max_open_trades"] == 1
    assert manifest["params"]["sizing"]["pct_nav"] == 0.9


@pytest.mark.parametrize("kind,subagent", [("scalping", False), ("trend", False), ("trend", True), ("news", True)])
def test_generated_entry_branches_forward_configured_percentage(tmp_path, kind, subagent):
    files, manifest = generate(tmp_path, strategy_class=kind,
        subagents=("analyst",) if subagent else ())
    scope = {}
    exec(compile(files["main.py"], "generated_fixture.py", "exec"), scope)
    # Stub indicator/model verdicts, not money/SDK semantics. No model/network.
    scope["_rsi"] = lambda *_: 55
    scope["_ma_cross_signal"] = lambda *_: {"cross": "golden_cross", "fast_now": 101, "slow_now": 100}
    records = []
    def open_position(**kwargs):
        records.append(kwargs)
        return {"status": "submitted"}
    ctx = SimpleNamespace(
        runmode="paper", config=SimpleNamespace(markets=[MARKET], params=manifest["params"]),
        trigger={"market": MARKET}, policy=SimpleNamespace(min_confidence=0.55),
        market=SimpleNamespace(candles=lambda *a, **k: bars([100] * 60 + [100.1]),
            features=lambda *a, **k: {}),
        portfolio=SimpleNamespace(position=lambda *_: None, positions=lambda *_: []),
        trading=SimpleNamespace(open_position=open_position),
        result=SimpleNamespace(hold=lambda **k: {"status": "hold"}),
        state=SimpleNamespace(get=lambda *_: None, set=lambda *_: None),
        clock=SimpleNamespace(now_iso=lambda: "2026-01-01T00:00:00Z"),
        news=SimpleNamespace(fetch=lambda **k: [{"title": "fixture event"}]),
        dedupe=SimpleNamespace(news=lambda items: items),
        llm=SimpleNamespace(classify=lambda **k: {"label": "alpha"}),
        subagents=SimpleNamespace(run=lambda *a, **k: {"output": {
            "recommendation": "buy", "confidence": 0.8, "thesis": "fixture"}}),
    )
    # Scalper's independent volume check requires a real fixture volume spike.
    rows = ctx.market.candles()
    rows[-1]["volume"] = 2000
    ctx.market.candles = lambda *a, **k: rows
    assert scope["run"](ctx)["status"] == "submitted"
    assert len(records) == 1
    assert records[0]["sizing"] is manifest["params"]["sizing"]
    assert records[0]["sizing"]["pct_nav"] == 0.9


@pytest.mark.parametrize("fraction", [True, 0, -0.1, 90, float("nan"), float("inf"), "0.9"])
def test_bad_percentage_is_rejected_at_save_not_mid_replay(fraction):
    manifest = SimpleNamespace(policy=SimpleNamespace(max_single_order_usd=0),
        extras={"params": {"sizing": {"method": "pct_nav", "pct_nav": fraction}}})
    assert any(code == "invalid_strategy_sizing" for code, _ in configuration_issues(manifest))


def test_skill_allocation_fragment_matches_actual_sdk_and_replay_schema():
    text = (BUILTIN / "strategy_author/references/position-sizing.md").read_text()
    fragment = yaml_io.loads(re.search(r"```yaml\n(.*?)\n```", text, re.S).group(1))
    policy = SizingPolicy(**fragment["params"]["sizing"])
    assert policy.pct_nav == 0.9
    config = load_config(markets=[MARKET], overrides=fragment["backtest"])
    assert config.stake_amount.mode == "unlimited"
    assert config.max_open_trades == fragment["policy"]["max_open_positions"]
    assert _resolve_notional(policy, nav_usd=10000, mark_price=100) == 9000
    assert _resolve_notional(policy, nav_usd=12000, mark_price=100) == 10800
    assert _resolve_notional(policy, nav_usd=8000, mark_price=100) == 7200
    for name in ("strategy_author", "backtest", "trading"):
        entry = BUILTIN / name / "SKILL.md"
        assert SkillManifest.from_skill_md(entry).id == name
        assert "references/position-sizing.md" in entry.read_text()
    assert "exposure_pct" in text and "NOT the fraction of capital" in text
    assert "Agent Loop" in text and "losing results remain" in text


def bars(prices):
    return [{"ts": START + i * 3600, "open": p, "high": p, "low": p,
             "close": p, "volume": 1000} for i, p in enumerate(prices)]


@pytest.mark.parametrize("capital", [1000, 10000, 20000])
def test_real_sdk_replay_reinvests_current_equity_not_initial_or_fixed_100(capital):
    def signals(rows):
        return {"buy": len(rows) in {2, 6}, "sell": len(rows) in {4, 8}}

    config = load_config(markets=[MARKET], overrides={
        "tf": "1h", "warmup_bars": 0, "initial_capital_usd": capital,
        "fee_bps_by_venue": {"MOCK": 0}, "slip_bps_by_venue": {"MOCK": 0},
    })
    result = run_backtest(None, config, run_fn=lambda ctx: run_signal_strategy(ctx, signals),
        strategy_config={"strategy_id": "percent_fixture", "timeframe": "1h", "markets": [MARKET],
            "policy": {"max_single_order_usd": 0, "max_daily_notional_usd": 0},
            "params": {"sizing": {"method": "pct_nav", "pct_nav": 0.9}}},
        candles_by_market={MARKET: bars([100, 100, 100, 110, 110, 110, 110, 121, 121, 121])})
    entries = [trade for trade in result.trades if trade["side"] == "buy"]
    assert len(entries) == 2
    assert entries[0]["notional"] == pytest.approx(capital * 0.9)
    assert entries[1]["notional"] == pytest.approx(capital * 1.09 * 0.9)
    assert result.equity_series[-1][1] == pytest.approx(capital * 1.09 ** 2)
    assert not result.rejected_signals


def test_three_slots_retain_cash_headroom_and_explicit_caps_still_reject():
    markets = ["MOCK:A", "MOCK:B", "MOCK:C"]
    config = load_config(markets=markets, overrides={"max_open_trades": 3,
        "fee_bps_by_venue": {"MOCK": 0}, "slip_bps_by_venue": {"MOCK": 0}})
    current = {market: bars([100])[0] for market in markets}
    orders = [{"market": market, "side": "buy", "size": 0.3, "size_unit": "pct_nav",
               "plan_action": "open_position"} for market in markets]
    portfolio = PortfolioState(10000)
    fills, rejects = settle([dict(order) for order in orders], current, current, portfolio, config)
    assert not rejects and len(fills) == 3
    assert [fill["notional"] for fill in fills] == pytest.approx([3000] * 3)
    assert portfolio.cash == pytest.approx(1000)
    limited = SimpleNamespace(max_single_order_usd=100, max_daily_notional_usd=0, max_open_positions=3)
    fills, rejects = settle(orders[:1], current, current, PortfolioState(10000), config, policy=limited)
    assert not fills and rejects[0]["reject_reason"] == "max_single_order_usd"
