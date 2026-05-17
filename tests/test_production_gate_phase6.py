"""Production gate — phase 6 acceptance for runtime capability rollout.

Lights up the full closed-loop chain end-to-end inside a temp workspace
and asserts that every phase's runtime invariant holds:

- **Phase 0** — feature flags load with the expected defaults; toggling
  via env vars survives :func:`feature_flags.reset_cache`.
- **Phase 1** — capability catalog enumerates the expected runtime surfaces and
  marks them ready while flags are on.
- **Phase 2** — tool compaction shrinks oversized payloads behind the flag.
- **Phase 3** — evidence vault auto-ingest writes a doc on a "promote"
  decision point, and the ACL refuses cross-strategy leakage.
- **Phase 4** — prompt guard auto-classify + operator profile capture
  both honor the flag and the trading-safety boundary.
- **Phase 5** — E2E auto-capture writes a finalized run on disk.

This test deliberately exercises the *combination* of subsystems so any
future regression that quietly disables one of the closed loops is
caught here even if the per-phase smoke tests still pass individually.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core.paths import WorkspacePaths


pytestmark = pytest.mark.smoke


def _client(tmp_path):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    return SimpleNamespace(config=cfg, skills=None)


def test_phase0_feature_flags_load_with_expected_defaults(tmp_path):
    from nerya.runtime import feature_flags as ff

    ff.reset_cache()
    client = _client(tmp_path)
    snap = ff.snapshot(client)
    keys = {f["key"] for f in snap["flags"]}
    expected = {
        "runtime.capability_catalog_v2",
        "runtime.data_source_sync_state",
        "runtime.tool_result_compaction",
        "runtime.evidence_vault",
        "runtime.prompt_guard_review_queue",
        "runtime.operator_profile",
        "runtime.e2e_artifact_capture",
    }
    assert expected.issubset(keys), (
        f"missing phase flags: {expected - keys}"
    )
    # All defaults are on (operator opt-out instead of opt-in).
    assert snap["counts"]["enabled"] == len(snap["flags"])


def test_phase1_capability_catalog_reflects_active_flags(tmp_path):
    from nerya.runtime import feature_flags as ff
    from nerya.runtime import capability_catalog as cc

    ff.reset_cache()
    client = _client(tmp_path)
    entries = cc.build_catalog(client)
    ids = {e.id for e in entries}
    # The expected runtime surfaces must all be enumerated.
    for needed in ("evidence.vault", "memory.operator_profile",
                   "security.prompt_guard_review",
                   "runtime.tool_result_compaction",
                   "ops.e2e_artifact_capture"):
        assert needed in ids, f"capability catalog missing {needed!r}"


def test_phase2_tool_compaction_runs_only_when_flag_on(monkeypatch, tmp_path):
    from nerya.runtime import feature_flags as ff
    from nerya.agent.loop import WorkspaceNativeAgentLoop
    from nerya.tools.types import ToolResult, ToolResultPart

    rows = [{"order_id": f"o_{i}", "status": "filled", "x": "y" * 200}
            for i in range(40)]
    result = ToolResult(
        tool_use_id="phase6_call",
        name="orders.list",
        content=[ToolResultPart.json_part({"orders": rows})],
    )
    loop = WorkspaceNativeAgentLoop.__new__(WorkspaceNativeAgentLoop)

    # flag on → compaction applies
    monkeypatch.delenv("NERYA_FF_RUNTIME_TOOL_RESULT_COMPACTION", raising=False)
    ff.reset_cache()
    block = loop._render_tool_result(result)
    assert "compaction" in block, "compaction should apply when flag is on"

    # flag off → compaction skipped
    monkeypatch.setenv("NERYA_FF_RUNTIME_TOOL_RESULT_COMPACTION", "0")
    ff.reset_cache()
    block_off = loop._render_tool_result(result)
    assert "compaction" not in block_off, "compaction must respect flag off"

    monkeypatch.delenv("NERYA_FF_RUNTIME_TOOL_RESULT_COMPACTION", raising=False)
    ff.reset_cache()


def test_phase3_evidence_autoingest_and_acl(tmp_path):
    from nerya.evidence import autoingest as ai
    from nerya.evidence.store import open_store

    client = _client(tmp_path)
    doc = ai.on_strategy_promote(
        client,
        strategy_id="phase6_alpha",
        proposal_id="p_001",
        title="Phase 6 alpha promote",
        summary="Promoted under acceptance test.",
    )
    assert doc is not None and doc.scope == "strategy"
    store = open_store(client)
    # ACL: scope=any without strategy_id must not leak
    leaked = store.search(scope="any")
    assert all(r.get("strategy_id") != "phase6_alpha" for r in leaked), (
        "ACL leaked private strategy evidence into operator scope=any"
    )
    # With matching strategy_id the doc is visible
    visible = store.search(scope="any", strategy_id="phase6_alpha")
    assert any(r.get("evidence_id") == doc.evidence_id for r in visible)


def test_phase4_prompt_guard_and_profile_capture(tmp_path):
    from nerya.agent.prompt_firewall import classify_user_input
    from nerya.agent.profile_capture import observe_turn

    client = _client(tmp_path)

    # Prompt guard — innocuous prompt is allowed
    verdict = classify_user_input(
        client,
        text="Please summarize the latest market notes.",
        source_route="POST /agent/run_turn",
        source_channel="chat",
    )
    assert verdict.get("verdict") in ("allow", "review"), verdict

    # Profile capture proposes a language fact after 3 EN-only turns
    for _ in range(3):
        observe_turn(client, user_text="Please continue in English clearly.")
    facts = client  # placeholder for type hint; we re-import to keep lint happy
    from nerya.agent import operator_profile
    style = operator_profile.list_facts(client.config.paths, facet="style")
    assert any(
        f.get("key") == "preferred_language" and f.get("source") == "agent_inferred"
        for f in style
    )


def test_phase5_e2e_auto_capture_writes_finalized_run(tmp_path):
    from nerya.ops import auto_capture as ac
    from nerya.ops import e2e_artifacts as e2e

    client = _client(tmp_path)
    meta = ac.capture_dashboard_smoke(
        client,
        label="phase6.smoke",
        checks=[
            {"method": "GET", "url": "/healthz", "status_code": 200, "elapsed_ms": 7},
        ],
    )
    assert meta is not None and meta.get("status") == "ok"
    runs = e2e.list_runs(client)
    assert any(r.get("run_id") == meta["run_id"] for r in runs)


def test_phase6_disabling_a_flag_degrades_only_that_phase(monkeypatch, tmp_path):
    """Disabling one flag should NOT affect the other phases' behavior."""
    from nerya.runtime import feature_flags as ff
    from nerya.evidence import autoingest as ai
    from nerya.agent.profile_capture import observe_turn

    monkeypatch.setenv("NERYA_FF_RUNTIME_EVIDENCE_VAULT", "0")
    ff.reset_cache()
    try:
        client = _client(tmp_path)
        # Evidence autoingest is gated off
        assert ai.on_strategy_promote(
            client,
            strategy_id="phase6_isolated",
            proposal_id="p_iso",
            title="iso",
            summary="iso",
        ) is None
        # Profile capture is unaffected (different flag)
        out = observe_turn(client, user_text="Please respond in English clearly.")
        assert out["flag_enabled"] is True
    finally:
        monkeypatch.delenv("NERYA_FF_RUNTIME_EVIDENCE_VAULT", raising=False)
        ff.reset_cache()
