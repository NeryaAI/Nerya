from types import SimpleNamespace

import pytest

from nerya.api.routes_evolution import _proposal_detail_dict, routes
from nerya.core import yaml_io
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import create_proposal

pytestmark = pytest.mark.smoke


def test_proposal_detail_includes_strategy_package_files(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="BTC MACD agent",
        extra_files={
            "after/strategies/btc_macd_agent/strategy.yml": (
                "strategy_id: btc_macd_agent\nexecution_mode: agent\n"
            ),
            "after/strategies/btc_macd_agent/main.py": "def run(ctx):\n    return 'macd'\n",
        },
    )

    detail = _proposal_detail_dict(proposal)

    assert detail["files"]["strategy.yml"].startswith("strategy_id: btc_macd_agent")
    assert "execution_mode: agent" in detail["files"]["strategy.yml"]
    assert "macd" in detail["files"]["main.py"]


def test_list_proposals_route_limits_after_newest_first_sort(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    paths.proposals.mkdir(parents=True)
    old_dir = paths.proposals / "prp_a_old"
    new_dir = paths.proposals / "prp_z_new"
    old_dir.mkdir()
    new_dir.mkdir()
    (old_dir / "proposal.yml").write_text(
        yaml_io.dumps(
            {
                "id": "prp_a_old",
                "kind": "strategy_package_proposal",
                "state": "pending_review",
                "summary": "Old BTC proposal",
                "ts": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (new_dir / "proposal.yml").write_text(
        yaml_io.dumps(
            {
                "id": "prp_z_new",
                "kind": "strategy_package_proposal",
                "state": "pending_review",
                "summary": "New whale proposal",
                "ts": "2026-02-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    list_route = next(
        handler
        for method, path, handler in routes()
        if method == "GET" and path == "/evolution/proposals"
    )
    client = SimpleNamespace(config=SimpleNamespace(paths=paths))

    result = list_route(
        client,
        {"kind": "strategy_package_proposal", "limit": "1"},
    )

    assert [p["id"] for p in result["proposals"]] == ["prp_z_new"]
