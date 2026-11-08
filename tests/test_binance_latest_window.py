"""Offline Binance pagination contract: startTime selects the earliest rows."""

from urllib.parse import parse_qs, urlparse

import pytest

from nerya.data import candles


HOUR = 3600
NOW = 1_793_182_345
CURRENT_OPEN = NOW // HOUR * HOUR
VENUES = ["BINANCE", "BINANCE_PERPETUAL"]
COUNTS = [1, 120, 1000, 1500]


@pytest.fixture
def binance(monkeypatch):
    monkeypatch.setattr(candles.time, "time", lambda: NOW)
    available = [
        [ts * 1000, "81000", "82000", "80000", "81492", "10"]
        for ts in range(CURRENT_OPEN - 2000 * HOUR, CURRENT_OPEN + 1, HOUR)
    ]
    calls = []

    def fake_http_json(url):
        parsed = urlparse(url)
        assert (parsed.netloc, parsed.path) in {
            ("data-api.binance.vision", "/api/v3/klines"),
            ("fapi.binance.com", "/fapi/v1/klines"),
        }
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        calls.append(query)
        assert query["symbol"] == "BTCUSDT"
        assert query["interval"] == "1h"
        limit = int(query["limit"])
        assert 1 <= limit <= 1000
        start_ms = int(query.get("startTime", 0))
        end_ms = int(query.get("endTime", NOW * 1000))
        eligible = [row for row in available if start_ms <= row[0] <= end_ms]
        # Binance returns chronological rows: earliest with startTime,
        # otherwise the most recent limit rows up to endTime.
        return eligible[:limit] if "startTime" in query else eligible[-limit:]

    def reject_network(*args, **kwargs):
        pytest.fail("real network is forbidden")

    monkeypatch.setattr(candles, "_http_json", fake_http_json)
    monkeypatch.setattr(candles, "urlopen", reject_network)
    return available, calls


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("count", COUNTS)
@pytest.mark.parametrize("closed_only", [False, True])
@pytest.mark.parametrize("now", [NOW, CURRENT_OPEN])
def test_latest_window_returns_tail_including_latest_available(
    binance, monkeypatch, venue, count, closed_only, now,
):
    monkeypatch.setattr(candles.time, "time", lambda: now)
    available, calls = binance
    if closed_only:
        available.pop()
    expected = available[-count:]

    rows = candles._fetch_public_rest_klines(
        venue, f"{venue}:BTCUSDT", interval="1h", count=count,
    )

    assert [row["ts"] for row in rows] == [row[0] // 1000 for row in expected]
    assert rows[-1]["close"] == float(expected[-1][4])
    assert rows[-1]["ts"] <= now
    assert all(int(call["endTime"]) == now * 1000 for call in calls)
    assert len(calls) <= (count + 999) // 1000 + 1


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("count", COUNTS)
def test_end_only_window_returns_tail_at_historical_end(binance, venue, count):
    available, calls = binance
    end = CURRENT_OPEN - 20 * HOUR + 17
    expected = [row for row in available if row[0] <= end * 1000][-count:]

    rows = candles._fetch_public_rest_klines(
        venue, f"{venue}:BTCUSDT", interval="1h", count=count, end=end,
    )

    assert [row["ts"] for row in rows] == [row[0] // 1000 for row in expected]
    assert all(int(call["endTime"]) <= end * 1000 for call in calls)


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("count", COUNTS)
@pytest.mark.parametrize("bounded_end", [False, True])
def test_explicit_start_preserves_earliest_count_and_exact_bounds(
    binance, venue, count, bounded_end,
):
    available, calls = binance
    start = CURRENT_OPEN - 1900 * HOUR + 17
    end = CURRENT_OPEN - 10 * HOUR if bounded_end else None
    upper = NOW if end is None else end
    expected = [row for row in available if start * 1000 <= row[0] <= upper * 1000][:count]

    rows = candles._fetch_public_rest_klines(
        venue, f"{venue}:BTCUSDT", interval="1h", count=count, start=start, end=end,
    )

    assert [row["ts"] for row in rows] == [row[0] // 1000 for row in expected]
    assert int(calls[0]["startTime"]) == start * 1000
    assert all(int(call["endTime"]) == upper * 1000 for call in calls)


@pytest.mark.parametrize("venue", VENUES)
def test_short_explicit_window_does_not_fill_from_outside_bounds(binance, venue):
    available, _ = binance
    start = CURRENT_OPEN - 20 * HOUR + 1
    end = CURRENT_OPEN - 17 * HOUR - 1
    expected = [row for row in available if start * 1000 <= row[0] <= end * 1000]

    rows = candles._fetch_public_rest_klines(
        venue, f"{venue}:BTCUSDT", interval="1h", count=120, start=start, end=end,
    )

    assert len(rows) == 2
    assert [row["ts"] for row in rows] == [row[0] // 1000 for row in expected]


@pytest.mark.parametrize("venue", VENUES)
def test_latest_short_history_is_not_padded_or_expanded(binance, venue):
    available, _ = binance
    available[:] = available[-3:]

    rows = candles._fetch_public_rest_klines(
        venue, f"{venue}:BTCUSDT", interval="1h", count=120,
    )

    assert [row["ts"] for row in rows] == [row[0] // 1000 for row in available]


@pytest.mark.parametrize("venue", VENUES)
def test_latest_gaps_do_not_expand_the_existing_window(binance, venue):
    available, _ = binance
    available[:] = available[::2]
    floor = NOW - (120 + 5) * HOUR
    expected = [row for row in available if floor * 1000 <= row[0] <= NOW * 1000]

    rows = candles._fetch_public_rest_klines(
        venue, f"{venue}:BTCUSDT", interval="1h", count=120,
    )

    assert 0 < len(rows) < 120
    assert [row["ts"] for row in rows] == [row[0] // 1000 for row in expected]


@pytest.mark.parametrize("venue", VENUES)
def test_empty_window_stays_empty(binance, venue):
    available, calls = binance
    available.clear()

    assert candles._fetch_public_rest_klines(
        venue, f"{venue}:BTCUSDT", interval="1h", count=120,
    ) == []
    assert len(calls) == 1
