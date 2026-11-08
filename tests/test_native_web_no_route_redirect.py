from __future__ import annotations

import pytest

from nerya.tools.native import web as native_web
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke


def test_web_search_does_not_route_by_query_keywords(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "results": []}

    monkeypatch.setattr(native_web.web_search, "run", fake_run)

    result = native_web.web_search_handler(
        ToolCall(
            name="web_search",
            arguments={
                "query": "Solana meme coin smart money tracking tools Birdeye DexScreener"
            },
        )
    )

    assert result.is_error is False
    assert captured["query"] == (
        "Solana meme coin smart money tracking tools Birdeye DexScreener"
    )


def test_web_search_fetch_does_not_route_by_query_keywords(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "documents": []}

    monkeypatch.setattr(native_web.search_fetch, "run", fake_run)

    result = native_web.web_search_fetch_handler(
        ToolCall(
            name="web_search_fetch",
            arguments={"query": "Solana on-chain wallet API data provider tracking"},
        )
    )

    assert result.is_error is False
    assert captured["query"] == "Solana on-chain wallet API data provider tracking"
    assert captured["use_scrapling_fallback"] is True


def test_web_fetch_does_not_route_by_url_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "url": kwargs.get("url"), "text": ""}

    monkeypatch.setattr(native_web.fetch_url, "run", fake_run)

    result = native_web.web_fetch_handler(
        ToolCall(
            name="web_fetch",
            arguments={"url": "https://api.dexscreener.com/latest/dex/tokens/abc"},
        )
    )

    assert result.is_error is False
    assert captured["url"] == "https://api.dexscreener.com/latest/dex/tokens/abc"


def test_onchain_source_discovery_loads_shared_search_workflow() -> None:
    from test_dex_exact_scope_routing import read_skill

    entry = read_skill(skill="research", file="SKILL.md")
    assert "references/onchain-data-discovery.md" in entry
    guide = read_skill(skill="research", file="references/onchain-data-discovery.md")
    for step in ("web_search", "web_fetch", 'data_api(op="schema")',
                 "replay_input:true", "not coverage receipts"):
        assert step in guide
    assert "search before asking the user to find a source" in guide
    for skill, file in (("strategy_author", "SKILL.md"), ("financial_ops", "SKILL.md"),
                        ("backtest", "references/replay-capabilities.md")):
        assert "references/onchain-data-discovery.md" in read_skill(skill=skill, file=file)


def test_replay_tool_description_routes_missing_history_to_discovery(tmp_path) -> None:
    from copy import deepcopy

    from nerya.agent.file_state import FileStateCache
    from nerya.core.config import Config, DEFAULT_CONFIG
    from nerya.core.paths import WorkspacePaths
    from nerya.tools.native.bootstrap import NativeToolDeps, register_native_tools
    from nerya.tools.native.skill import SkillIndex
    from nerya.tools.native.task import TaskState
    from nerya.tools.registry import ToolRegistry

    registry = ToolRegistry()
    deps = NativeToolDeps(
        workspace_root=tmp_path, file_state=FileStateCache(),
        task_state=TaskState(), skill_index=SkillIndex([]),
        config=Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG)),
    )
    register_native_tools(registry, deps)
    description = registry.get("strategy_backtest").description
    assert "references/onchain-data-discovery.md" in description
    assert "authorized read-only producers" in description
    assert "stop and report the data gap" not in description
