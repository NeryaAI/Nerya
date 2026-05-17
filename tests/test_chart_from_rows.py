"""Tests for ``nerya.charting.from_rows`` builders + skill integrations.

The builders are the connective tissue between flat skill data and the
composer; if they drop rows silently or pick the wrong column, every
skill in PR5 quietly falls over. We test the column-aliasing,
type-coercion, and equity-curve-with-drawdown derivation in isolation,
then exercise the equity_research and backtest integrations end-to-end
with synthetic rows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nerya.charting import (
    candle_chart_from_rows,
    equity_curve_from_rows,
    line_chart_from_rows,
)


pytestmark = pytest.mark.smoke


# ---------- column-aliasing & coercion ----------------------------------


def test_line_chart_picks_first_matching_keys() -> None:
    rows = [
        {"date": "2024-01-01", "close": 100.0},
        {"date": "2024-01-02", "close": 101.5},
        {"date": "2024-01-03", "close": 99.8},
    ]
    block = line_chart_from_rows(
        rows,
        title="AAPL daily",
        skill="equity_research",
        action="prices",
    )
    assert block is not None
    assert block["chart_kind"] == "line"
    data = block["series"][0]["data"]
    assert len(data) == 3
    # Sorted by time ascending; close picked from "close" column.
    assert data[0]["value"] == 100.0
    assert data[-1]["value"] == 99.8


def test_line_chart_returns_none_on_empty() -> None:
    assert line_chart_from_rows([], title="x", skill="s", action="a") is None


def test_line_chart_skips_unparseable_rows() -> None:
    rows = [
        {"time": 1700000000, "value": 1.0},
        {"time": "not a time", "value": 2.0},
        {"time": 1700003600, "value": "junk"},
        {"time": 1700007200, "value": 3.0},
    ]
    block = line_chart_from_rows(rows, title="x", skill="s", action="a")
    assert block is not None
    assert len(block["series"][0]["data"]) == 2


def test_candle_chart_handles_short_keys() -> None:
    rows = [
        {"t": 1700000000, "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000},
        {"t": 1700003600, "o": 100.5, "h": 102, "l": 100, "c": 101.5, "v": 1100},
    ]
    block = candle_chart_from_rows(rows, title="BTC 1h", skill="markets", action="get_candles")
    assert block is not None
    assert block["chart_kind"] == "candlestick"
    candles = block["series"][0]["data"]
    assert len(candles) == 2
    assert candles[0]["volume"] == 1000


def test_candle_chart_returns_none_when_missing_ohlc() -> None:
    rows = [{"date": "2024-01-01", "close": 100}]
    assert candle_chart_from_rows(rows, title="x", skill="s", action="a") is None


def test_equity_curve_appends_drawdown_overlay() -> None:
    rows = [
        {"time": 1, "equity": 1.00},
        {"time": 2, "equity": 1.10},  # peak
        {"time": 3, "equity": 1.05},  # drawdown -4.55%
        {"time": 4, "equity": 1.20},  # new peak
    ]
    block = equity_curve_from_rows(rows, title="t", initial_capital=1.0)
    assert block is not None
    series = block["series"]
    names = [s["name"] for s in series]
    assert names == ["equity", "drawdown_pct"]
    dd = series[1]["data"]
    assert dd[0]["value"] == 0.0  # at peak
    assert dd[1]["value"] == 0.0  # still at peak
    assert dd[2]["value"] < 0.0  # drawdown
    assert dd[3]["value"] == 0.0  # new peak again
    # Total return insight prepended.
    assert any("Total return: +20.00%" in s for s in block["insights"])


def test_equity_curve_returns_none_when_no_value_column() -> None:
    rows = [{"time": 1, "wrong": 1.0}]
    assert equity_curve_from_rows(rows) is None


# ---------- equity_research integration --------------------------------


def test_equity_research_attaches_chart_when_prices_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Stub EquitiesClient so we don't hit the network.
    fake_payload = {
        "ticker": "AAPL",
        "interval": "day",
        "as_of": "2024-01-15",
        "prices": [
            {"date": "2024-01-10", "open": 180, "high": 182, "low": 179, "close": 181},
            {"date": "2024-01-11", "open": 181, "high": 183, "low": 180.5, "close": 182.4},
            {"date": "2024-01-12", "open": 182, "high": 184, "low": 181, "close": 183.0},
        ],
    }

    class _FakeClient:
        def prices(self, ticker: str, **_kwargs: Any) -> dict[str, Any]:
            return dict(fake_payload)

        def news(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("not called in this test")

        def insider_trades(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("not called in this test")

        def metrics_snapshot(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("not called in this test")

    import nerya.skills.builtin.equity_research_skill.scripts.fetch_market_data as fmd

    monkeypatch.setattr(fmd, "EquitiesClient", lambda: _FakeClient())

    out = fmd.run(ticker="AAPL", command="prices", limit=3, interval="day")
    assert out["ok"] is True
    blocks = out.get("chart_blocks") or []
    assert len(blocks) == 1
    assert blocks[0]["chart_kind"] == "candlestick"
    # Inline path (no workspace passed) → series carries data directly.
    assert blocks[0]["series"][0]["data"][0]["close"] == 181


def test_equity_research_no_chart_for_news_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """News doesn't have a chartable shape — no chart_blocks should appear."""

    class _FakeClient:
        def prices(self, *a, **k):
            raise AssertionError

        def news(self, ticker: str, **_k: Any) -> dict[str, Any]:
            return {"news": [{"headline": "x"}]}

        def insider_trades(self, *a, **k):
            raise AssertionError

        def metrics_snapshot(self, *a, **k):
            raise AssertionError

    import nerya.skills.builtin.equity_research_skill.scripts.fetch_market_data as fmd

    monkeypatch.setattr(fmd, "EquitiesClient", lambda: _FakeClient())
    out = fmd.run(ticker="AAPL", command="news")
    assert "chart_blocks" not in out


# ---------- backtest integration ---------------------------------------


def test_backtest_render_chart_attaches_blocks(tmp_path: Path) -> None:
    # Build a fake backtest dir under a fake workspace so the helper
    # can find the workspace root and route to the bulk path.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "nerya.yml").write_text("# stub\n", encoding="utf-8")
    bt_dir = workspace / "backtest" / "demo_strat" / "20260101T000000Z"
    bt_dir.mkdir(parents=True)

    # Minimal CSV rows for the renderer.
    ohlcv_csv = bt_dir / "ohlcv_indicators_portfolio.csv"
    ohlcv_csv.write_text(
        "ts,open,high,low,close,equity,rsi_14\n"
        "1700000000,100,102,99,101,1.00,55\n"
        "1700003600,101,103,100,102.5,1.025,60\n"
        "1700007200,102.5,104,101.5,103.2,1.032,65\n"
        "1700010800,103.2,103.5,101.8,102,1.020,52\n",
        encoding="utf-8",
    )
    trades_csv = bt_dir / "trades.csv"
    trades_csv.write_text(
        "ts,side,reason\n1700003600,buy,signal\n1700010800,sell,stop\n",
        encoding="utf-8",
    )
    metrics = {
        "verdict": "PASS",
        "total_return_pct": 2.0,
        "max_drawdown_pct": -1.16,
        "sharpe_ratio": 1.4,
        "initial_capital_usd": 1.0,
    }
    (bt_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")

    from nerya.skills.builtin.backtest.scripts.render_chart import render_chart

    chart = render_chart(bt_dir)
    blocks = chart.get("chart_blocks") or []
    # Equity + price candle.
    assert len(blocks) == 2
    kinds = {b["chart_kind"] for b in blocks}
    assert kinds == {"line", "candlestick"}
    # Equity block carries the auto-derived drawdown overlay + total
    # return insight from the helper.
    eq = next(b for b in blocks if b["chart_kind"] == "line")
    series_names = [s["name"] for s in eq["series"]]
    assert "drawdown_pct" in series_names
    assert any("Total return:" in i for i in eq["insights"])
    # Bulk path (workspace had nerya.yml) → series.data should have
    # been replaced by data_uri references.
    assert eq["series"][0].get("data") in (None, [])
    assert eq["series"][0].get("data_uri", "").startswith("nerya://chart/")


# ---------- module-level imports for the test above --------------------

import json  # noqa: E402  -- imported late to keep test_logic above tidy
