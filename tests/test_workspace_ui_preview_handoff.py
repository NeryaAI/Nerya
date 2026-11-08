"""Producer-owned preview handoff, using only isolated candidate files."""

import json
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from nerya.agent.loop import WorkspaceNativeAgentLoop
from nerya.core import yaml_io
from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core.paths import WorkspacePaths
from nerya.tools.native import workspace_ui as native_ui
from nerya.tools.types import ToolCall, ToolErrorKind
from nerya.workspace import ui

pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def offline_and_review_only(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("preview handoff must not access network/models or apply a proposal")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("nerya.llm.gateway.LLMGateway.call_messages", forbidden)
    monkeypatch.setattr(ui, "apply", forbidden)
    monkeypatch.setattr("nerya.core.paths.resolve_workspace", lambda: WorkspacePaths(root=tmp_path))
    monkeypatch.setattr("nerya.runtime.feature_flags.is_enabled", lambda *args: True)


def _call(operations):
    return ToolCall(id="panel-handoff", name="workspace_ui_propose", arguments={
        "summary": "Create a read-only observation panel", "operations": operations,
    })


@pytest.mark.parametrize("container", ["home", "page", "both", "navigation"])
def test_real_producer_hands_off_navigable_unapplied_preview(tmp_path, container):
    config = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    before = ui.read(config.paths)
    operations = []
    if container in {"page", "both", "navigation"}:
        operations.append({"op": "upsert_page", "page": {
            "id": "observations", "title": "Observations",
        }})
    if container != "navigation":
        pages = ["home", "observations"] if container == "both" else [
            "home" if container == "home" else "observations"
        ]
        for page in pages:
            operations.append({"op": "upsert_widget", "page": page, "widget": {
                "id": "price-history", "kind": "chart", "source": "market.candles",
                "config": {"venue": "binance", "market": "SOL/USDT", "timeframe": "1h"},
            }})
    result = native_ui.workspace_ui_propose_handler(_call(operations), config=config)
    assert not result.is_error, result.text()
    data = result.content[0].data
    assert data["preview_href"] == (
        f"/workspace/pages/preview?proposal_id={data['proposal_id']}"
    )
    href = urlsplit(data["preview_href"])
    assert not href.scheme and not href.netloc and not href.fragment
    assert parse_qs(href.query) == {"proposal_id": [data["proposal_id"]]}
    assert data["next_required_action"] == "preview_and_review_proposal"
    assert data["state"] == data["proposal"]["state"] == "pending_review"
    assert data["applied"] is False
    assert data["data_validation"]["status"] == "not_checked"

    # Exercise the actual model-facing renderer without starting a loop or model.
    loop = object.__new__(WorkspaceNativeAgentLoop)
    loop.config = config
    rendered = loop._render_tool_result(result)
    text = "\n".join(part["text"] for part in rendered["content"])
    if "compaction" in rendered:
        text = text.split("\n[compacted_kept]\n", 1)[1]
    visible = json.loads(text)
    assert visible["preview_href"] == data["preview_href"]
    assert visible["next_required_action"] == "preview_and_review_proposal"
    assert visible["data_validation"]["status"] == "not_checked"

    files = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    candidate = ui.preview(config.paths, parse_qs(href.query)["proposal_id"][0])
    assert candidate["ok"] and candidate["state"] == "pending_review"
    assert candidate["applied"] is False and candidate["data_validation"] == "not_checked"
    metadata = yaml_io.load(config.paths.proposals / data["proposal_id"] / "proposal.yml")
    assert metadata["state"] == "pending_review"
    after = ui.read(config.paths)
    assert (after["manifest"], after["revision"], after["digest"]) == (
        before["manifest"], before["revision"], before["digest"],
    )
    assert not (tmp_path / "ui/workspace.yml").exists()
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == files


def test_real_producer_returns_exact_preview_url_and_action(tmp_path):
    config = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    result = native_ui.workspace_ui_propose_handler(
        _call([{"op": "upsert_page", "page": {"id": "observations", "title": "Observations"}}]),
        config=config,
    )

    assert not result.is_error, result.text()
    data = result.content[0].data
    expected = f"/workspace/pages/preview?proposal_id={data['proposal_id']}"
    assert data["preview_url"] == expected
    assert data["preview_href"] == expected
    assert data["preview_action"] == {
        "id": "preview",
        "label": "Preview panel",
        "href": expected,
    }


def test_inspect_recovers_existing_preview_without_apply_or_new_proposal(tmp_path, monkeypatch):
    config = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    proposed = native_ui.workspace_ui_propose_handler(
        _call([{"op": "upsert_page", "page": {"id": "observations", "title": "Observations"}}]),
        config=config,
    )
    assert not proposed.is_error, proposed.text()
    proposal_data = proposed.content[0].data
    proposal_id = proposal_data["proposal_id"]
    assert not (tmp_path / "ui/workspace.yml").exists()


    proposals_before = len(list(config.paths.proposals.iterdir()))
    monkeypatch.setattr(ui, "apply", lambda *args, **kwargs: pytest.fail("recovery must not apply"))

    recovered = native_ui.workspace_ui_inspect_handler(
        ToolCall(
            id="recover-preview",
            name="workspace_ui_inspect",
            arguments={"proposal_id": proposal_id},
        ),
        config=config,
    )

    assert not recovered.is_error, recovered.text()
    data = recovered.content[0].data
    expected = f"/workspace/pages/preview?proposal_id={proposal_id}"
    assert data["source"] == "proposal"
    assert data["proposal_id"] == proposal_id
    assert data["preview_url"] == expected
    assert data["preview_action"]["href"] == expected
    assert data["state"] == "pending_review"
    assert data["applied"] is False
    assert data["data_validation"]["status"] == "not_checked"
    assert data["manifest"]["pages"][0]["id"] == "observations"
    assert len(list(config.paths.proposals.iterdir())) == proposals_before
    assert not (tmp_path / "ui/workspace.yml").exists()


@pytest.mark.parametrize('proposal_id', ['', '../escape', 'prp_missing', 'prp_missing?apply=true', 17])
def test_inspect_recovery_rejects_invalid_or_unknown_candidate_without_writes(tmp_path, proposal_id):
    config = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    result = native_ui.workspace_ui_inspect_handler(
        ToolCall(name='workspace_ui_inspect', arguments={'proposal_id': proposal_id}), config=config,
    )
    assert result.is_error
    assert 'preview_url' not in result.text()
    assert {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()} == before


@pytest.mark.parametrize("bad_id", [
    "", None, 123, "prp_unknown", "../escape", "prp_bad?apply=true",
    "padded", "suffixed",
])
def test_producer_rejects_unknown_or_inexact_preview_id(tmp_path, monkeypatch, bad_id):
    config = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    propose = ui.propose

    def tampered(paths, payload):
        result = propose(paths, payload)
        assert result["ok"]
        pid = result["proposal_id"]
        result["proposal_id"] = (
            f" {pid} " if bad_id == "padded" else
            f"{pid}_unknown" if bad_id == "suffixed" else bad_id
        )
        return result

    monkeypatch.setattr(ui, "propose", tampered)
    result = native_ui.workspace_ui_propose_handler(
        _call([{"op": "upsert_page", "page": {"id": "observations", "title": "Observations"}}]),
        config=config,
    )
    assert result.is_error, result.text()
    assert result.error.kind is ToolErrorKind.EXECUTION_ERROR
    assert "preview_href" not in result.text()
    assert not (tmp_path / "ui/workspace.yml").exists()
