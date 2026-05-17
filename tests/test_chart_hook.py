"""Tests for the kernel-side chart_blocks extractor.

Covers:
- happy path: ``markets.get_candles`` style stdout (pretty-printed JSON
  with ``chart_blocks: [...]``).
- single-block lenient shape (``chart_block: <obj>``).
- non-string / non-dict result types.
- guard rails against malformed JSON & non-chart dicts.
- ``@@nerya:chart@@ <id>`` marker extraction (dynamic-code path).
"""

from __future__ import annotations

import json

import pytest

from nerya.agent.chart_hook import (
    extract_chart_blocks,
    extract_chart_marker_ids,
    normalise_chart_blocks,
)


pytestmark = pytest.mark.smoke


_CHART = {
    "kind": "chart",
    "version": "v1",
    "chart_id": "demo.smoke.deadbeef",
    "chart_kind": "candlestick",
    "title": "demo",
    "series": [{"type": "candlestick", "name": "ohlc", "data": []}],
    "source": {"skill": "markets", "action": "get_candles", "as_of": ""},
    "path": "inline",
}


def _envelope_with_chart(extra: dict | None = None) -> dict:
    payload = {
        "market": "mock:BTC/USDT",
        "interval": "1h",
        "limit": 8,
        "ohlcv": [],
        "chart_blocks": [_CHART],
    }
    if extra:
        payload.update(extra)
    return payload


def test_extract_from_dict_happy_path() -> None:
    out = extract_chart_blocks(_envelope_with_chart())
    assert len(out) == 1
    assert out[0]["chart_id"] == "demo.smoke.deadbeef"


def test_extract_from_pretty_printed_stdout() -> None:
    payload = _envelope_with_chart()
    stdout = json.dumps(payload, indent=2)
    out = extract_chart_blocks(stdout)
    assert len(out) == 1
    assert out[0]["chart_id"] == "demo.smoke.deadbeef"


def test_extract_from_stdout_with_prose_prefix() -> None:
    payload = _envelope_with_chart()
    body = json.dumps(payload)
    decorated = f"$ python -m markets.get_candles\n[exit=0]\n## stdout\n{body}\n"
    out = extract_chart_blocks(decorated)
    assert len(out) == 1


def test_extract_singular_chart_block_shape() -> None:
    out = extract_chart_blocks({"chart_block": _CHART})
    assert len(out) == 1
    assert out[0]["chart_id"] == "demo.smoke.deadbeef"


def test_extract_returns_empty_for_unrelated_text() -> None:
    assert extract_chart_blocks("hello world") == []
    assert extract_chart_blocks("{not json") == []
    assert extract_chart_blocks(None) == []
    assert extract_chart_blocks(42) == []


def test_extract_drops_malformed_chart_dicts() -> None:
    malformed = {
        "chart_blocks": [
            {"kind": "chart"},  # missing chart_id
            {"chart_id": "nope"},  # missing kind
            "not-a-dict",
            _CHART,  # one good entry survives
        ]
    }
    out = extract_chart_blocks(malformed)
    assert len(out) == 1
    assert out[0]["chart_id"] == "demo.smoke.deadbeef"


def test_extract_walks_past_first_unrelated_json_blob() -> None:
    payload = _envelope_with_chart()
    text = json.dumps({"unrelated": True}) + "\n" + json.dumps(payload)
    out = extract_chart_blocks(text)
    assert len(out) == 1


def test_marker_extraction() -> None:
    text = (
        "running script...\n"
        "@@nerya:chart@@ markets.get_candles.55b6cb355ef0c3e9\n"
        "more output\n"
        "@@nerya:chart@@ another.chart.id\n"
    )
    ids = extract_chart_marker_ids(text)
    assert ids == [
        "markets.get_candles.55b6cb355ef0c3e9",
        "another.chart.id",
    ]


def test_marker_extraction_no_match() -> None:
    assert extract_chart_marker_ids("nothing to see") == []
    assert extract_chart_marker_ids(None) == []


def test_marker_extraction_ignores_non_id_chars() -> None:
    """The id regex is allow-list (alnum + ``._-``); spaces / quotes
    end the match so a marker mid-sentence still works."""

    text = "see @@nerya:chart@@ chart.with-dashes_OK and then prose"
    assert extract_chart_marker_ids(text) == ["chart.with-dashes_OK"]


def test_normalise_filters_non_chart() -> None:
    raw = [_CHART, {"kind": "chart"}, "garbage", 42]
    out = normalise_chart_blocks(raw)
    assert len(out) == 1
    assert out[0]["chart_id"] == _CHART["chart_id"]
