from __future__ import annotations

from types import SimpleNamespace

from nerya.api import routes_evolution
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
import pytest

pytestmark = pytest.mark.smoke


def test_evolution_assets_routes_candidate_promote(tmp_path):
    client = SimpleNamespace(config=Config(paths=WorkspacePaths(tmp_path), data={}))
    route_map = {(method, path): handler for method, path, handler in routes_evolution.routes()}

    candidate = route_map[("POST", "/evolution/assets/candidate")](
        client,
        {
            "kind": "gene",
            "summary": "route gene",
            "payload": {
                "id": "gene_route",
                "category": "repair",
                "signals_match": ["tool_failure_cluster"],
                "preconditions": [],
                "strategy": [],
                "validation": [],
            },
        },
    )
    promoted = route_map[("POST", "/evolution/assets/promote")](
        client,
        {"candidate_id": candidate["id"]},
    )
    listed = route_map[("POST", "/evolution/assets")](client, {"kind": "gene"})

    assert promoted["ok"] is True
    assert any(row["id"] == "gene_route" for row in listed["assets"])


def test_evolution_apply_route_returns_dict(tmp_path):
    client = SimpleNamespace(config=Config(paths=WorkspacePaths(tmp_path), data={}))
    route_map = {(method, path): handler for method, path, handler in routes_evolution.routes()}

    out = route_map[("POST", "/evolution/apply")](client, {"proposal_id": "missing"})

    assert out == {"ok": False, "reason": "not_found"}
