from __future__ import annotations

from nerya.core.paths import WorkspacePaths
from nerya.evolution.assets import (
    create_candidate,
    list_candidates,
    promote_candidate,
    reject_candidate,
    search_assets,
)
import pytest

pytestmark = pytest.mark.smoke


def test_asset_candidate_promote_writes_gene(tmp_path):
    paths = WorkspacePaths(tmp_path)
    candidate = create_candidate(
        paths,
        kind="gene",
        summary="test gene",
        payload={
            "id": "gene_test",
            "category": "repair",
            "signals_match": ["tool_failure_cluster"],
            "preconditions": [],
            "strategy": [],
            "validation": ["python -m pytest tests/test_evolution_assets.py -q"],
            "max_files": 1,
        },
        evidence_refs=["signal:sig_1"],
    )

    out = promote_candidate(paths, candidate["id"])

    assert out["ok"] is True
    assert any(asset["id"] == "gene_test" for asset in search_assets(paths, kind="gene"))
    assert list_candidates(paths) == []


def test_asset_candidate_reject_removes_from_pending(tmp_path):
    paths = WorkspacePaths(tmp_path)
    candidate = create_candidate(
        paths,
        kind="capsule",
        summary="test capsule",
        payload={"summary": "capsule"},
        evidence_refs=["signal:sig_1"],
    )

    out = reject_candidate(paths, candidate["id"], reason="duplicate")

    assert out["ok"] is True
    assert list_candidates(paths) == []
