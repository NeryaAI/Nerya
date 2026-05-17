"""Schema-level tests for ``nerya.agent.chart_block``.

These tests pin the wire shape that the dashboard's TypeScript mirror
(`dashboard/lib/chartBlock.ts`) consumes. If the JSON layout drifts on
either side, the renderer breaks silently — so we lock the keys here.
"""

from __future__ import annotations

import json

import pytest

from nerya.agent.chart_block import (
    CHART_BLOCK_VERSION,
    MAX_INLINE_BYTES,
    ChartBlock,
    ChartOverlay,
    ChartSeries,
    ChartSource,
    estimate_inline_bytes,
    make_chart_id,
    stable_chart_id,
    validate_chart_block,
)


pytestmark = pytest.mark.smoke


def _inline_block() -> ChartBlock:
    return ChartBlock(
        chart_kind="line",
        title="demo",
        series=[
            ChartSeries(
                type="line",
                name="x",
                data=[
                    {"time": 1700000000, "value": 1.0},
                    {"time": 1700003600, "value": 1.5},
                ],
            )
        ],
        source=ChartSource(
            skill="demo", action="inline", as_of="2026-05-06T00:00:00Z"
        ),
        insights=["hello"],
        path="inline",
    )


def _bulk_block() -> ChartBlock:
    return ChartBlock(
        chart_kind="candlestick",
        title="bulk demo",
        series=[
            ChartSeries(
                type="candlestick",
                name="ohlc",
                data_uri="nerya://chart/abc#series/ohlc",
            )
        ],
        source=ChartSource(
            skill="markets", action="get_quote", as_of="2026-05-06T00:00:00Z"
        ),
        path="bulk",
        bulk_data_uri="nerya://chart/abc",
    )


def test_inline_block_has_canonical_shape() -> None:
    block = _inline_block()
    payload = block.as_dict()

    assert payload["kind"] == "chart"
    assert payload["version"] == CHART_BLOCK_VERSION
    assert payload["chart_kind"] == "line"
    assert payload["path"] == "inline"
    assert payload["title"] == "demo"
    assert payload["insights"] == ["hello"]
    assert payload["series"][0]["data"][0]["value"] == 1.0
    assert "bulk_data_uri" not in payload  # absent on inline path
    assert "ts" in payload


def test_bulk_block_carries_uri_and_no_inline_data() -> None:
    block = _bulk_block()
    payload = block.as_dict()

    assert payload["path"] == "bulk"
    assert payload["bulk_data_uri"] == "nerya://chart/abc"
    assert "data" not in payload["series"][0]
    assert payload["series"][0]["data_uri"] == "nerya://chart/abc#series/ohlc"


def test_validate_accepts_well_formed_blocks() -> None:
    assert validate_chart_block(_inline_block()) == []
    assert validate_chart_block(_bulk_block()) == []


def test_validate_rejects_series_with_neither_data_nor_uri() -> None:
    block = ChartBlock(
        chart_kind="line",
        title="bad",
        series=[ChartSeries(type="line", name="x")],  # data=None, data_uri=None
        source=ChartSource(skill="demo", action="bad", as_of="now"),
        path="inline",
    )
    errors = validate_chart_block(block)
    assert any("must set either data or data_uri" in e for e in errors)


def test_validate_rejects_both_data_and_uri_on_same_series() -> None:
    block = ChartBlock(
        chart_kind="line",
        title="bad",
        series=[
            ChartSeries(
                type="line",
                name="x",
                data=[{"time": 1, "value": 1.0}],
                data_uri="nerya://chart/abc",
            )
        ],
        source=ChartSource(skill="demo", action="bad", as_of="now"),
        path="inline",
    )
    errors = validate_chart_block(block)
    assert any("cannot set both data and data_uri" in e for e in errors)


def test_validate_rejects_invalid_path() -> None:
    block = _inline_block()
    block.path = "magic"  # type: ignore[assignment]
    errors = validate_chart_block(block)
    assert any("path must be" in e for e in errors)


def test_validate_rejects_bulk_without_uri() -> None:
    block = ChartBlock(
        chart_kind="line",
        title="bulk no uri",
        series=[
            ChartSeries(
                type="line",
                name="x",
                data=[{"time": 1, "value": 1.0}],
            )
        ],
        source=ChartSource(skill="demo", action="x", as_of="now"),
        path="bulk",
    )
    errors = validate_chart_block(block)
    assert any("bulk path requires" in e for e in errors)


def test_estimate_inline_bytes_grows_with_data() -> None:
    small = _inline_block()
    big = ChartBlock(
        chart_kind="line",
        title="big",
        series=[
            ChartSeries(
                type="line",
                name="x",
                data=[{"time": i, "value": float(i)} for i in range(500)],
            )
        ],
        source=ChartSource(skill="demo", action="big", as_of="now"),
        path="inline",
    )
    assert estimate_inline_bytes(small) < estimate_inline_bytes(big)
    assert estimate_inline_bytes(big) < MAX_INLINE_BYTES  # 500 floats well under 256KB


def test_make_chart_id_unique() -> None:
    a = make_chart_id()
    b = make_chart_id()
    assert a != b
    assert a.startswith("chart_")


def test_stable_chart_id_is_deterministic() -> None:
    payload = {"symbol": "BTC", "tf": "1d"}
    a = stable_chart_id("markets", "get_quote", payload)
    b = stable_chart_id("markets", "get_quote", payload)
    c = stable_chart_id("markets", "get_quote", {"symbol": "ETH", "tf": "1d"})
    assert a == b
    assert a != c
    assert a.startswith("markets.get_quote.")


def test_overlays_emit_camel_friendly_range_keys() -> None:
    overlay = ChartOverlay(
        type="region",
        from_time=1700000000,
        to_time=1700090000,
        color="#fbbf24",
        label="vix spike",
    )
    payload = overlay.as_dict()
    assert payload["from"] == 1700000000
    assert payload["to"] == 1700090000
    assert "from_time" not in payload
    assert "to_time" not in payload


def test_block_round_trips_through_json() -> None:
    block = _inline_block()
    payload = block.as_dict()
    encoded = json.dumps(payload, default=str)
    decoded = json.loads(encoded)
    assert decoded["chart_id"] == payload["chart_id"]
    assert decoded["series"][0]["data"][0]["value"] == 1.0
