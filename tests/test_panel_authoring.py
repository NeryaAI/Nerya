"""Regression for the BTC/ETH panel acceptance routing gap; no runtime/network."""

from copy import deepcopy
import json
from pathlib import Path
import re
import socket

import pytest

from nerya.core import yaml_io
from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import list_proposals
from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.native.workspace_ui import (
    workspace_ui_inspect_handler,
    workspace_ui_propose_handler,
)
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke
BUILTIN_ROOT = Path(__file__).resolve().parents[1] / "nerya/skills/builtin"


@pytest.mark.parametrize("query", [
    "帮我做一个展示 BTC、ETH 行情和账户概览的面板。",
    "BTC ETH panel",
    "BTC/ETH 行情面板",
    "panel",
    "面板",
])
def test_panel_request_discovers_native_authoring_workflow(query):
    index = SkillIndex([BUILTIN_ROOT])
    result = skill_tool_handler(
        ToolCall(name="Skill", arguments={"action": "search", "query": query}),
        skill_index=index,
    )
    assert not result.is_error
    rows = result.content[0].data["skills"]
    assert rows and rows[0]["workflow"] == "coding", rows
    # Follow the real search -> load -> reference path, without widening grants.
    loaded = skill_tool_handler(
        ToolCall(name="Skill", arguments={"skill": rows[0]["workflow"]}),
        skill_index=index,
    )
    assert "workspace_ui_inspect" in loaded.text()
    assert "references/native-panels.md" in loaded.text()


def test_panel_search_still_respects_role_skill_restrictions():
    result = skill_tool_handler(
        ToolCall(name="Skill", arguments={"action": "search", "query": "BTC ETH panel"},
                 metadata={"allowed_skills": ["markets"]}),
        skill_index=SkillIndex([BUILTIN_ROOT]),
    )
    assert all(row["skill_id"] == "markets" for row in result.content[0].data["skills"])


def test_primary_skill_list_advertises_panel_construction():
    result = skill_tool_handler(
        ToolCall(name="Skill", arguments={"action": "list"}),
        skill_index=SkillIndex([BUILTIN_ROOT]),
    )
    data = next(part.data for part in result.content if part.type == "json")
    coding = next(row for row in data["skills"] if row["skill_id"] == "coding")
    assert "面板" in coding["description"]
    assert "workspace_ui_inspect" in coding["description"]
    assert "workspace_ui_propose" in coding["description"]


def test_coding_delivery_start_reports_unsupported_contract_without_retry(tmp_path):
    from test_task_delivery import _command, _deps, _runtime
    from nerya.tools.native.delivery import delivery_start_handler

    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    result = delivery_start_handler(ToolCall(name="delivery_start", arguments={
        "skill_id": "coding", "workflow_id": "native_panels_btc_eth",
        "requested_scope": {"summary": "Create a read-only BTC and ETH panel"},
        "stages": [{"id": "inspect_ui"}, {"id": "author_proposal"}, {"id": "deliver"}],
    }), deps=_deps(config, command))
    assert result.is_error
    hint = result.error.recovery_hint
    assert hint["action"] == "read_skill_workflow"
    assert hint["retryable"] is False
    assert "Do not retry delivery_start" in hint["hint"]
    assert "artifact_examples" not in hint
    assert runtime.store.snapshot(command["session_id"])["commands"][0]["delivery"] is None
    loaded = skill_tool_handler(ToolCall(name="Skill", arguments={"skill": "coding"}),
                                skill_index=SkillIndex([BUILTIN_ROOT]))
    assert "Do not call `delivery_start`" in loaded.text()


@pytest.mark.parametrize("existing_page", [False, True])
def test_first_pass_panel_reference_builds_only_a_pending_native_proposal(tmp_path, monkeypatch, existing_page):
    def no_network(*args, **kwargs):
        pytest.fail("Panel construction must not query live data or any runtime service")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    config = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    preserved = {"id": "operator-note", "kind": "markdown", "text": "Keep this note"}
    if existing_page:
        yaml_io.dump(tmp_path / "ui/workspace.yml", {
            "version": 1, "home": {"widgets": []},
            "pages": [{"id": "market-account-overview", "title": "BTC / ETH overview",
                       "widgets": [preserved, {
                           "id": "btc-quote", "kind": "market_ticker",
                           "source": "market.ticker",
                           "config": {"venue": "binance", "market": "BTC/USDT"},
                       }], "nav": {"order": 7}}],
        })
    inspected = workspace_ui_inspect_handler(
        ToolCall(name="workspace_ui_inspect", arguments={}), config=config,
    ).content[0].data
    reference = skill_tool_handler(
        ToolCall(name="Skill", arguments={"skill": "coding", "file": "references/native-panels.md"}),
        skill_index=SkillIndex([BUILTIN_ROOT]),
    )
    assert not reference.is_error
    examples = re.findall(r"```json\n(.*?)\n```", reference.text(), re.DOTALL)
    assert len(examples) == 1
    args = json.loads(examples[0])
    assert len(json.dumps(args)) < 4000
    assert args["operations"][0]["op"] == "add_page"
    # Follow the reference's CREATE collision rule, using the inspected ids.
    page_id = args["operations"][0]["page"]["id"]
    existing_ids = {page["id"] for page in inspected["manifest"]["pages"]}
    if page_id in existing_ids:
        page_id += "-2"
        args["operations"][0]["page"]["id"] = page_id
        for operation in args["operations"][1:]:
            operation["page"] = page_id
    assert page_id not in existing_ids
    args.update(base_revision=inspected["revision"], base_digest=inspected["digest"])
    proposed = workspace_ui_propose_handler(
        ToolCall(name="workspace_ui_propose", arguments=args), config=config,
    )
    assert not proposed.is_error, proposed.text()
    data = proposed.content[0].data
    assert data["resource_kind"] == "workspace_ui"
    assert data["state"] == "pending_review"
    assert data["applied"] is False
    assert data["data_validation"]["status"] == "not_checked"
    assert data["next_required_action"] == "preview_and_review_proposal"
    assert data["preview_href"] == f"/workspace/pages/preview?proposal_id={data['proposal_id']}"
    proposals = list_proposals(config.paths)
    assert len(proposals) == 1
    assert proposals[0].state == "pending_review"
    staged = config.paths.proposals / data["proposal_id"] / "after/ui/workspace.yml"
    manifest = yaml_io.load(staged)
    assert len(manifest["pages"]) == len(inspected["manifest"]["pages"]) + 1
    widgets = next(page for page in manifest["pages"] if page["id"] == page_id)["widgets"]
    tickers = [widget for widget in widgets if widget["kind"] == "market_ticker"]
    assert {widget["config"]["market"] for widget in tickers} == {"BTC/USDT", "ETH/USDT"}
    assert all(widget["source"] == {"id": "market.ticker", "read_only": True} for widget in tickers)
    portfolio = next(widget for widget in widgets if widget["kind"] == "portfolio")
    assert portfolio["source"] == {"id": "portfolio.summary", "read_only": True}
    assert "mode" not in portfolio.get("config", {})
    if existing_page:
        # Inspect adds a navigation href that is not part of the stored manifest.
        original = {key: value for key, value in inspected["manifest"]["pages"][0].items()
                    if key != "href"}
        assert next(page for page in manifest["pages"] if page["id"] == original["id"]) == original
    else:
        assert not (tmp_path / "ui/workspace.yml").exists()
    assert not (tmp_path / "accounts.yml").exists()
    assert not (tmp_path / "nerya.yml").exists()
    assert not list(tmp_path.rglob("*.html"))
    unchanged = workspace_ui_inspect_handler(
        ToolCall(name="workspace_ui_inspect", arguments={}), config=config,
    ).content[0].data
    assert unchanged["digest"] == inspected["digest"]
    assert unchanged["revision"] == inspected["revision"]


def test_add_page_then_widget_can_omit_initial_widget_list(tmp_path):
    config = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    result = workspace_ui_propose_handler(ToolCall(
        name="workspace_ui_propose",
        arguments={"summary": "New page", "operations": [
            {"op": "add_page", "page": {"id": "watch", "title": "Watch"}},
            {"op": "add_widget", "page": "watch",
             "widget": {"id": "btc", "kind": "market_ticker",
                        "config": {"market": "BTC/USDT", "venue": "binance"}}},
        ]},
    ), config=config)
    assert not result.is_error, result.text()
    assert result.content[0].data["state"] == "pending_review"
    assert not (tmp_path / "ui/workspace.yml").exists()


def test_inspect_exposes_server_data_contract_without_claiming_data_readiness(tmp_path):
    data = workspace_ui_inspect_handler(
        ToolCall(name="workspace_ui_inspect", arguments={}),
        config=Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG)),
    ).content[0].data
    catalog = {row["kind"]: row for row in data["catalog"]["widget_kinds"]}
    ticker = catalog["market_ticker"]
    assert ticker["config_example"] == {"venue": "binance", "market": "BTC/USDT"}
    assert ticker["data_contract"]["mode"] == "public_quote_read_only"
    assert ticker["data_contract"]["requires_trading_enabled"] is False
    portfolio = catalog["portfolio"]
    assert portfolio["data_contract"]["mode"] == "server_account_modes"
    assert portfolio["data_contract"]["supports_account_filter"] is False
    assert portfolio["data_contract"]["supports_mode_filter"] is False
    assert data["data_validation"]["status"] == "not_checked"
