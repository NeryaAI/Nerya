from __future__ import annotations

import copy

import pytest

from nerya.agent.task_control import create_mandate, subscribe_task, trigger_health
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.triggers.market_source import MarketWatch, poll


pytestmark = pytest.mark.smoke


def live(source, **extra):
    return {"_envelope": {"mode": "live", "degraded": False, "source": source}, **extra}


def test_market_source_uses_baseline_then_emits_price_event(tmp_path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    cfg = Config(paths=paths, data=copy.deepcopy(DEFAULT_CONFIG))
    create_mandate(paths, task_id="task_market", command_id="cmd", session_id="session", actor_id="owner",
                   task_kind="research", objective="Watch BTC", scope={"paper_only": True})
    subscribe_task(paths, task_id="task_market", source="market", kind="price.move", match={"payload.market": "BINANCE:BTCUSDT"})
    calls = []

    def dispatch(_paths, event, config):
        calls.append(event)
        return {"state": "delivered"}

    monkeypatch.setattr("nerya.triggers.market_source.dispatch_trigger", dispatch)
    ticker = {"last": 100.0, **live("binance")}
    poll(cfg, watches=[MarketWatch(id="btc", market="BINANCE:BTCUSDT", price_change_pct=5)], now=1000,
         ticker_fetch=lambda *a, **k: ticker, funding_fetch=lambda *a, **k: {"funding_rate": 0.0, **live("binance")})
    ticker["last"] = 110.0
    result = poll(cfg, watches=[MarketWatch(id="btc", market="BINANCE:BTCUSDT", price_change_pct=5)], now=1100,
                  ticker_fetch=lambda *a, **k: ticker, funding_fetch=lambda *a, **k: {"funding_rate": 0.0, **live("binance")})
    assert len(calls) == 1 and calls[0]["kind"] == "price.move"
    assert result["emitted"][0]["delivery"]["state"] == "delivered"


def test_market_source_rejects_mock_or_unavailable_data_and_marks_health(tmp_path):
    cfg = Config(paths=WorkspacePaths(tmp_path), data=copy.deepcopy(DEFAULT_CONFIG))
    result = poll(cfg, watches=[MarketWatch(id="btc", market="BINANCE:BTCUSDT", price_change_pct=1)], now=1000,
                  ticker_fetch=lambda *a, **k: {"last": 100, "_envelope": {"mode": "mock", "degraded": False}},
                  funding_fetch=lambda *a, **k: {"funding_rate": 0, "_envelope": {"mode": "live", "degraded": False}})
    assert not result["ok"] and result["emitted"] == []
    assert trigger_health(cfg.paths, "market")[0]["state"] == "degraded"
