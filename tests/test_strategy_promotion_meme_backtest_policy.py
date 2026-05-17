from __future__ import annotations

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.trading.promotion import EvidenceStore, evaluate_promotion
from nerya.trading.strategy_crud import CreateRequest, create

pytestmark = pytest.mark.smoke


def _create_strategy(tmp_path, *, status: str) -> tuple[WorkspacePaths, EvidenceStore]:
    paths = WorkspacePaths(root=tmp_path)
    create(
        paths,
        CreateRequest(
            strategy_id="meme_promotion_policy",
            title="Meme promotion policy",
            status=status,
            markets=("OKX_ONCHAIN:solana:Token444444444444444444444444444444444",),
            account_id="paper_main",
        ),
    )
    return paths, EvidenceStore(paths)


def _record_required(store: EvidenceStore, *, include_canary: bool = False) -> None:
    for kind in (
        "static_review",
        "paper_window",
        "shadow_window",
        "protection_check",
        "backtest_waiver",
    ):
        store.record(
            strategy_id="meme_promotion_policy",
            kind=kind,  # type: ignore[arg-type]
            passed=True,
            payload={"test": True},
            operator="alice",
        )
    if include_canary:
        store.record(
            strategy_id="meme_promotion_policy",
            kind="canary_window",
            passed=True,
            payload={"test": True},
            operator="alice",
        )


def test_backtest_waiver_needs_operator_signoff_before_canary(tmp_path):
    paths, store = _create_strategy(tmp_path, status="shadow")
    _record_required(store)

    decision = evaluate_promotion(
        paths,
        strategy_id="meme_promotion_policy",
        target="canary",
    )

    assert decision.verdict == "needs_evidence"
    assert "backtest_waiver" in decision.evidence_seen
    assert "operator_signoff" in decision.missing_evidence

    store.record(
        strategy_id="meme_promotion_policy",
        kind="operator_signoff",
        passed=True,
        payload={"reason": "approve meme strategy without standard backtest"},
        operator="alice",
    )

    decision = evaluate_promotion(
        paths,
        strategy_id="meme_promotion_policy",
        target="canary",
    )

    assert decision.verdict == "allow"
    assert "backtest_waiver" in decision.evidence_seen
    assert "operator_signoff" in decision.evidence_seen


def test_backtest_waiver_live_still_escalates_for_final_operator_click(tmp_path):
    paths, store = _create_strategy(tmp_path, status="canary")
    _record_required(store, include_canary=True)
    store.record(
        strategy_id="meme_promotion_policy",
        kind="operator_signoff",
        passed=True,
        payload={"reason": "approve live meme strategy without standard backtest"},
        operator="alice",
    )

    decision = evaluate_promotion(
        paths,
        strategy_id="meme_promotion_policy",
        target="live",
    )

    assert decision.verdict == "escalate"
    assert "backtest_waiver" in decision.evidence_seen
    assert "operator_signoff" in decision.evidence_seen
    assert "live_promotion_requires_operator_click" in decision.reasons
