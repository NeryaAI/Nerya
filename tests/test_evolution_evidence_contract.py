"""Evidence readiness is not investment quality or a fixed workflow priority."""
from __future__ import annotations

from dataclasses import replace

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import Proposal
from nerya.evolution.ranking import rank_proposals

pytestmark = pytest.mark.smoke


def test_rank_uses_only_each_proposals_explicit_references(tmp_path, monkeypatch):
    from nerya.evolution import ranking
    (tmp_path / "evidence.json").write_text('{"observation": 7}', encoding="utf-8")
    first = Proposal(id="p1", kind="learning_update", state="draft", path=tmp_path / "p1",
                     summary="first", ts="2026-09-06T01:00:00Z", evidence_refs=["file:evidence.json"])
    second = replace(first, id="p2", kind="strategy_config_patch", path=tmp_path / "p2",
                     evidence_refs=["file:missing.json"])
    monkeypatch.setattr(ranking, "list_proposals", lambda paths: [second, first])
    rows = rank_proposals(WorkspacePaths(tmp_path))
    assert [row.proposal.id for row in rows] == ["p1", "p2"]
    assert rows[0].asdict()["evidence_coverage"] == 1.0
    assert rows[1].asdict()["unresolved_refs"] == ["file:missing.json"]
    assert "score" not in rows[0].asdict()
    assert "causal" in rows[0].rationale


def test_proposal_summary_cannot_claim_another_strategy(tmp_path, monkeypatch):
    from nerya.evolution import ranking
    proposal = Proposal(id="p1", kind="learning_update", state="draft", path=tmp_path / "p1",
                        summary="mentions strategies/alpha/main.py but belongs to no strategy",
                        ts="2026-09-06T01:00:00Z")
    monkeypatch.setattr(ranking, "list_proposals", lambda paths: [proposal])
    assert rank_proposals(WorkspacePaths(tmp_path), strategy_id="alpha") == []
    proposal.metadata = {"strategy_id": "alpha"}
    assert len(rank_proposals(WorkspacePaths(tmp_path), strategy_id="alpha")) == 1


def test_empty_evolution_tick_creates_no_proposal(tmp_path):
    from nerya.evolution.runner import evolve
    from nerya.evolution.patch_proposal import list_proposals
    config = Config(paths=WorkspacePaths(tmp_path), data={})
    result = evolve(config)
    assert result["status"] == "no_evidence"
    assert result["proposal"] is None
    assert list_proposals(config.paths) == []
