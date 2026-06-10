from __future__ import annotations

import pytest

from nerya.llm.task_classes import (
    AGENT_LOOP,
    STRUCTURED_EXTRACTION,
    normalise_task_class,
)


pytestmark = pytest.mark.smoke


def test_extract_candle_data_routes_as_structured_extraction() -> None:
    assert normalise_task_class("extract_candle_data") == STRUCTURED_EXTRACTION


def test_agent_loop_alias_routes_as_agent_loop_class() -> None:
    assert normalise_task_class("agent.loop") == AGENT_LOOP
