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

    assert candidate["safe_to_promote"] is True
    assert candidate["promotion_gates"]["can_promote"] is True
    assert candidate["promotion_gates"]["review_only_until_promoted"] is True
    assert candidate["promotion_gates"]["selector_eligible"] is False

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


def test_asset_candidate_without_evidence_is_blocked_from_promotion(tmp_path):
    paths = WorkspacePaths(tmp_path)
    candidate = create_candidate(
        paths,
        kind="capsule",
        summary="no evidence capsule",
        payload={"summary": "capsule"},
        evidence_refs=[],
    )

    assert candidate["safe_to_promote"] is False
    assert "missing_evidence_refs" in candidate["blocked_reasons"]
    assert candidate["promotion_gates"]["selector_eligible"] is False

    out = promote_candidate(paths, candidate["id"])

    assert out["ok"] is False
    assert out["reason"] == "blocked"
    assert "missing_evidence_refs" in out["blocked_reasons"]
