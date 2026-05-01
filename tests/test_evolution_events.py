from __future__ import annotations

from nerya.core.paths import WorkspacePaths
from nerya.evolution.event_store import append_signal, list_events, list_signals, record_event
from nerya.evolution.events import EvolutionSignal
import pytest

pytestmark = pytest.mark.smoke


def test_signal_store_dedupes_by_key(tmp_path):
    paths = WorkspacePaths(tmp_path)
    signal = EvolutionSignal.create(
        source="turn",
        kind="repeated_noop",
        severity="warn",
        summary="noop cluster",
        evidence_refs=["turn:t1"],
        dedupe_key="noop:*",
    )

    _, created_first = append_signal(paths, signal)
    _, created_second = append_signal(paths, signal)

    rows = list_signals(paths)
    assert created_first is True
    assert created_second is False
    assert len(rows) == 1
    assert rows[0]["kind"] == "repeated_noop"


def test_event_store_filters_by_proposal(tmp_path):
    paths = WorkspacePaths(tmp_path)
    record_event(
        paths,
        proposal_id="prp_1",
        outcome="proposed",
        summary="proposal created",
        evidence_refs=["proposal:prp_1"],
    )
    record_event(paths, proposal_id="prp_2", outcome="rejected")

    rows = list_events(paths, proposal_id="prp_1")

    assert len(rows) == 1
    assert rows[0]["proposal_id"] == "prp_1"
    assert rows[0]["outcome"] == "proposed"
