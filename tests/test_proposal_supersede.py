from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import (
    create_proposal,
    list_proposals,
    supersede_pending_siblings,
)


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def _tuning_proposal(cfg: Config, *, target: str, state: str = "pending_review"):
    return create_proposal(
        cfg.paths,
        kind="strategy_tuning_proposal",
        summary=f"tuning recommendation for {target}",
        target=target,
        initial_state=state,
    )


def test_supersede_marks_older_open_siblings(tmp_path) -> None:
    cfg = _config(tmp_path)
    old_a = _tuning_proposal(cfg, target="strategies/s1")
    old_b = _tuning_proposal(cfg, target="strategies/s1")
    newest = _tuning_proposal(cfg, target="strategies/s1")

    superseded = supersede_pending_siblings(
        cfg.paths,
        kind="strategy_tuning_proposal",
        target="strategies/s1",
        keep_id=newest.id,
    )

    assert set(superseded) == {old_a.id, old_b.id}
    states = {p.id: p.state for p in list_proposals(cfg.paths)}
    assert states[newest.id] == "pending_review"
    assert states[old_a.id] == "superseded"
    assert states[old_b.id] == "superseded"


def test_supersede_leaves_other_targets_kinds_and_terminal_states(tmp_path) -> None:
    cfg = _config(tmp_path)
    other_target = _tuning_proposal(cfg, target="strategies/s2")
    other_kind = create_proposal(
        cfg.paths,
        kind="learning_update",
        summary="unrelated",
        target="strategies/s1",
        initial_state="pending_review",
    )
    applied = _tuning_proposal(cfg, target="strategies/s1", state="applied")
    newest = _tuning_proposal(cfg, target="strategies/s1")

    superseded = supersede_pending_siblings(
        cfg.paths,
        kind="strategy_tuning_proposal",
        target="strategies/s1",
        keep_id=newest.id,
    )

    assert superseded == []
    states = {p.id: p.state for p in list_proposals(cfg.paths)}
    assert states[other_target.id] == "pending_review"
    assert states[other_kind.id] == "pending_review"
    assert states[applied.id] == "applied"
    assert states[newest.id] == "pending_review"


def test_supersede_without_target_is_noop(tmp_path) -> None:
    cfg = _config(tmp_path)
    _tuning_proposal(cfg, target="strategies/s1")

    assert (
        supersede_pending_siblings(
            cfg.paths,
            kind="strategy_tuning_proposal",
            target=None,
            keep_id="prp_none",
        )
        == []
    )


def test_inbox_hides_superseded_proposals(tmp_path) -> None:
    from types import SimpleNamespace

    from nerya.api.routes_inbox import _read_proposals

    cfg = _config(tmp_path)
    old = _tuning_proposal(cfg, target="strategies/s1")
    newest = _tuning_proposal(cfg, target="strategies/s1")
    supersede_pending_siblings(
        cfg.paths,
        kind="strategy_tuning_proposal",
        target="strategies/s1",
        keep_id=newest.id,
    )

    client = SimpleNamespace(config=cfg)
    ids = {p["id"] for p in _read_proposals(client)}
    assert newest.id in ids
    assert old.id not in ids
