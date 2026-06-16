from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nerya.evolution.observation_summary import (
    latest_observation_time,
    observation_weight,
    parse_observed_at,
    summarize_observation_weights,
)


pytestmark = pytest.mark.smoke


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_observation_summary_applies_decay_and_source_caps():
    anchor = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    rows = [
        {
            "status": "regressed",
            "source": "strategy_run_paper",
            "observed_at": _iso(anchor),
        },
        {
            "status": "failed",
            "source": "strategy_run_paper",
            "observed_at": _iso(anchor),
        },
        {
            "status": "regressed",
            "source": "strategy_run_paper",
            "observed_at": _iso(anchor),
        },
        {
            "status": "healthy",
            "source": "validation_backtest",
            "observed_at": _iso(anchor),
        },
        {
            "status": "observing",
            "source": "strategy_run_live",
            "observed_at": _iso(anchor - timedelta(days=7)),
        },
    ]

    summary = summarize_observation_weights(
        rows,
        half_life_days=7.0,
        source_weight_cap=2.0,
    )

    assert summary["by_status"] == {
        "failed": 1,
        "healthy": 1,
        "observing": 1,
        "regressed": 2,
    }
    assert summary["by_source"] == {
        "strategy_run_live": 1,
        "strategy_run_paper": 3,
        "validation_backtest": 1,
    }
    assert summary["weighted_by_source"] == {
        "strategy_run_live": 0.5,
        "strategy_run_paper": 2.0,
        "validation_backtest": 1.0,
    }
    assert summary["weighted_by_status"] == {
        "failed": pytest.approx(0.6667, abs=0.0001),
        "healthy": 1.0,
        "observing": 0.5,
        "regressed": pytest.approx(1.3333, abs=0.0001),
    }
    assert summary["weighted_negative_count"] == 2.0
    assert summary["weighted_healthy_count"] == 1.0
    assert summary["weighted_observing_count"] == 0.5
    assert summary["decay"] == {
        "half_life_days": 7.0,
        "source_weight_cap": 2.0,
        "anchor_observed_at": _iso(anchor),
    }
    assert summary["dominant_sources"][0] == {
        "source": "strategy_run_paper",
        "raw_count": 3,
        "weight": 2.0,
    }


def test_observation_summary_handles_outcomes_defaults_and_bad_timestamps():
    anchor = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    rows = [
        {
            "status": "ok",
            "source": "validation_backtest",
            "observed_at": _iso(anchor),
        },
        {
            "outcome": "degraded",
            "source": "operator",
            "observed_at": "not-a-timestamp",
        },
        {
            "source": "",
            "ts": _iso(anchor - timedelta(days=14)),
        },
    ]

    summary = summarize_observation_weights(rows, half_life_days=7.0)

    assert summary["by_status"] == {
        "degraded": 1,
        "observing": 1,
        "ok": 1,
    }
    assert summary["by_source"] == {
        "operator": 1,
        "unknown": 1,
        "validation_backtest": 1,
    }
    assert summary["weighted_by_status"] == {
        "degraded": 0.5,
        "observing": 0.25,
        "ok": 1.0,
    }
    assert summary["weighted_negative_count"] == 0.5
    assert summary["weighted_healthy_count"] == 1.0
    assert summary["weighted_observing_count"] == 0.25
    assert latest_observation_time(rows) == anchor
    assert parse_observed_at({"observed_at": "2026-01-15T12:00:00Z"}) == anchor
    assert observation_weight(
        {"observed_at": "not-a-timestamp"},
        anchor=anchor,
    ) == 0.5
