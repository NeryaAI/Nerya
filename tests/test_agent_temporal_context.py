from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from nerya.agent.kernel import AgentKernel
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core.time import reset_clock, set_clock
from nerya.llm.task_classes import COMPLEX_REASONING, normalise_task_class


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(
        paths=WorkspacePaths(root=tmp_path),
        data=deepcopy(DEFAULT_CONFIG),
    )


def test_system_prompt_includes_current_date_and_freshness_rules(tmp_path) -> None:
    set_clock(lambda: datetime(2026, 5, 2, 15, 58, 10, tzinfo=timezone.utc))
    try:
        cfg = _config(tmp_path)
        kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
        deps = kernel._ensure_registry()

        prompt = kernel._build_system_prompt(deps, session_id="s1")
    finally:
        reset_clock()

    assert "Today's date is 2026-05-02" in prompt
    assert "Current UTC time is 2026-05-02T15:58:10Z" in prompt
    assert "web_search_fetch" in prompt
    assert "current/latest/recent/today/this year" in prompt
    assert "Do not describe 2024-2025 as the current environment" in prompt


def test_freeform_investment_analysis_tasks_route_to_complex_reasoning() -> None:
    assert normalise_task_class("analysis") == COMPLEX_REASONING
    assert normalise_task_class("a_share_investment_guide") == COMPLEX_REASONING
