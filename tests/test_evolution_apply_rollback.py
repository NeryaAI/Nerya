from __future__ import annotations

import json

import pytest

from nerya.core import jsonl
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import (
    create_proposal,
    list_proposals,
    reseal_candidate_bundle,
    set_state,
)
from nerya.evolution.promotion import apply_proposal
from nerya.evolution.rollback import rollback_proposal


pytestmark = pytest.mark.smoke


def test_apply_records_observation_pending_without_positive_reward(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="Apply must wait for outcome evidence",
        initial_state="approved",
        evidence_refs=["turn:apply"],
        extra_files={"after/notes/applied.txt": "applied\n"},
    )

    result = apply_proposal(paths, proposal.id)

    assert result["ok"] is True
    events = [
        row
        for row in jsonl.read_all(paths.evolution_events)
        if row.get("proposal_id") == proposal.id and row.get("outcome") == "applied"
    ]
    assert events
    assert all(float(row.get("outcome_score") or 0.0) == 0.0 for row in events)
    assert all(
        (row.get("metadata") or {}).get("observation_status") == "pending"
        for row in events
    )

    capsules = [
        row
        for row in jsonl.read_all(paths.evolution_capsules)
        if row.get("promotion_ref") == f"proposal:{proposal.id}"
    ]
    assert len(capsules) == 1
    assert capsules[0]["outcome_score"] == 0.0
    assert capsules[0]["metadata"]["outcome_status"] == "observing"
    assert capsules[0]["metadata"]["reward_status"] == "pending"


def test_rollback_manifest_removes_created_restores_modified_and_deleted(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    (tmp_path / "modified.txt").write_text("before\n", encoding="utf-8")
    (tmp_path / "deleted.txt").write_text("restore me\n", encoding="utf-8")
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="Round-trip mutation manifest",
        initial_state="approved",
        metadata={"deleted_files": ["deleted.txt"]},
        extra_files={
            "after/modified.txt": "after\n",
            "after/created.txt": "new\n",
        },
    )

    applied = apply_proposal(paths, proposal.id)

    assert applied["ok"] is True
    manifest_path = paths.evolution / "artifacts" / proposal.id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["created"] == ["created.txt"]
    assert manifest["modified"] == ["modified.txt"]
    assert manifest["deleted"] == ["deleted.txt"]
    assert not (tmp_path / "deleted.txt").exists()
    assert (tmp_path / "created.txt").exists()

    rolled_back = rollback_proposal(paths, proposal.id)

    assert rolled_back["ok"] is True
    assert not (tmp_path / "created.txt").exists()
    assert (tmp_path / "modified.txt").read_text(encoding="utf-8") == "before\n"
    assert (tmp_path / "deleted.txt").read_text(encoding="utf-8") == "restore me\n"
    assert rolled_back["removed_created_files"] == ["created.txt"]
    assert set(rolled_back["restored_files"]) == {"modified.txt", "deleted.txt"}


def test_set_state_applied_event_is_not_a_reward(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="State transition",
        initial_state="pending_review",
    )

    updated = set_state(paths, proposal.id, "applied")

    assert updated is not None
    event = jsonl.read_all(paths.evolution_events)[-1]
    assert event["outcome"] == "applied"
    assert event["outcome_score"] == 0.0
    assert event["metadata"]["observation_status"] == "pending"


def test_set_state_approved_event_is_gate_only_not_a_reward(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="Approval is not outcome evidence",
        initial_state="pending_review",
    )

    updated = set_state(paths, proposal.id, "approved", note="operator gate")

    assert updated is not None
    event = jsonl.read_all(paths.evolution_events)[-1]
    assert event["outcome"] == "approved"
    assert event["outcome_score"] == 0.0
    assert event["metadata"]["approval_status"] == "approved"
    assert event["metadata"]["reward_status"] == "unevaluated"


def test_legacy_artifacts_derive_created_files_from_after_snapshot(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    (tmp_path / "old.txt").write_text("old\n", encoding="utf-8")
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="Legacy artifact compatibility",
        initial_state="approved",
        extra_files={
            "after/old.txt": "new\n",
            "after/new.txt": "new file\n",
        },
    )
    assert apply_proposal(paths, proposal.id)["ok"] is True
    (paths.evolution / "artifacts" / proposal.id / "manifest.json").unlink()

    result = rollback_proposal(paths, proposal.id)

    assert result["ok"] is True
    assert not (tmp_path / "new.txt").exists()
    assert (tmp_path / "old.txt").read_text(encoding="utf-8") == "old\n"


def test_apply_fails_closed_when_workspace_changed_after_review(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    target = tmp_path / "notes.txt"
    target.write_text("reviewed base\n", encoding="utf-8")
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="CAS workspace conflict",
        initial_state="approved",
        evidence_refs=["turn:cas"],
        extra_files={"after/notes.txt": "candidate\n"},
    )
    target.write_text("operator changed this\n", encoding="utf-8")

    result = apply_proposal(paths, proposal.id)

    assert result["ok"] is False
    assert result["reason"] == "candidate_bundle_conflict"
    assert "base_revision" in result["candidate_bundle"]["mismatches"]
    assert target.read_text(encoding="utf-8") == "operator changed this\n"


def test_apply_fails_closed_when_candidate_bundle_is_tampered(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="CAS bundle conflict",
        initial_state="approved",
        evidence_refs=["turn:cas-bundle"],
        extra_files={"after/notes.txt": "candidate\n"},
    )
    bundle_path = proposal.path / "candidate_bundle.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["after_digest"] = "tampered"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = apply_proposal(paths, proposal.id)

    assert result["ok"] is False
    assert result["reason"] == "candidate_bundle_conflict"
    assert result["candidate_bundle"]["reason"] == "candidate_bundle_digest_invalid"
    assert not (tmp_path / "notes.txt").exists()


def test_apply_fails_closed_when_signed_deletion_changes(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    target = tmp_path / "old.txt"
    target.write_text("keep me\n", encoding="utf-8")
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="Deletion declarations are signed",
        initial_state="approved",
        metadata={"deleted_files": ["old.txt"]},
    )
    proposal_yml = proposal.path / "proposal.yml"
    proposal_yml.write_text(
        proposal_yml.read_text(encoding="utf-8").replace("old.txt", "other.txt"),
        encoding="utf-8",
    )

    result = apply_proposal(paths, proposal.id)

    assert result["ok"] is False
    assert result["reason"] == "candidate_bundle_conflict"
    assert target.read_text(encoding="utf-8") == "keep me\n"


def test_apply_fails_closed_when_staged_file_mode_changes(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="Staged file modes are signed",
        initial_state="approved",
        extra_files={"after/notes.txt": "candidate\n"},
    )
    (proposal.path / "after" / "notes.txt").chmod(0o755)

    result = apply_proposal(paths, proposal.id)

    assert result["ok"] is False
    assert result["reason"] == "candidate_bundle_conflict"
    assert not (tmp_path / "notes.txt").exists()


def test_apply_fails_closed_when_validation_report_changes_after_review(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="Bind legacy validation evidence",
        initial_state="approved",
        extra_files={
            "after/notes.txt": "candidate\n",
            "validation_report.json": json.dumps({"ok": False, "issues": ["fail"]}),
        },
    )
    (proposal.path / "validation_report.json").write_text(
        json.dumps({"ok": True, "issues": []}), encoding="utf-8"
    )

    result = apply_proposal(paths, proposal.id)

    assert result["ok"] is False
    assert result["reason"] == "candidate_bundle_conflict"
    assert "validation_report_digest" in result["candidate_bundle"]["mismatches"]


def test_apply_fails_closed_when_candidate_bundle_version_is_malformed(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="Reject malformed CAS version",
        initial_state="approved",
        evidence_refs=["turn:cas-version"],
        extra_files={"after/notes.txt": "candidate\n"},
    )
    bundle_path = proposal.path / "candidate_bundle.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["version"] = "not-a-number"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = apply_proposal(paths, proposal.id)

    assert result["ok"] is False
    assert result["reason"] == "candidate_bundle_conflict"
    assert result["candidate_bundle"]["reason"] == "candidate_bundle_missing_or_invalid"
    assert not (tmp_path / "notes.txt").exists()


def test_apply_fails_closed_when_candidate_after_contains_symlink(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="Reject staged links",
        initial_state="approved",
        evidence_refs=["turn:cas-symlink"],
        extra_files={"after/notes.txt": "candidate\n"},
    )
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("must not be copied\n", encoding="utf-8")
    (proposal.path / "after" / "leak.txt").symlink_to(secret)

    result = apply_proposal(paths, proposal.id)

    assert result["ok"] is False
    assert result["reason"] == "candidate_bundle_conflict"
    assert result["candidate_bundle"]["reason"] == "candidate_bundle_symlink"
    assert not (tmp_path / "leak.txt").exists()


def test_apply_fails_closed_when_after_root_is_symlink(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="Reject an escaped after root",
        initial_state="approved",
        extra_files={"after/notes.txt": "candidate\n"},
    )
    outside = tmp_path / "outside-after"
    outside.mkdir()
    (outside / "leak.txt").write_text("must not copy\n", encoding="utf-8")
    after = proposal.path / "after"
    (after / "notes.txt").unlink()
    after.rmdir()
    after.symlink_to(outside, target_is_directory=True)

    result = apply_proposal(paths, proposal.id)

    assert result["ok"] is False
    assert result["reason"] == "candidate_bundle_conflict"
    assert result["candidate_bundle"]["reason"] == "candidate_bundle_symlink"
    assert not (tmp_path / "leak.txt").exists()


def test_list_proposals_ignores_proposal_directory_symlink(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="Do not follow proposal directory links",
    )
    outside = tmp_path / "external-proposal"
    proposal.path.rename(outside)
    proposal.path.symlink_to(outside, target_is_directory=True)

    assert not any(item.id == proposal.id for item in list_proposals(paths))


def test_rollback_fails_closed_when_workspace_changed_after_apply(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    target = tmp_path / "notes.txt"
    target.write_text("before\n", encoding="utf-8")
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="Detect rollback conflict",
        initial_state="approved",
        extra_files={"after/notes.txt": "applied\n"},
    )
    assert apply_proposal(paths, proposal.id)["ok"] is True
    target.write_text("operator edit\n", encoding="utf-8")

    result = rollback_proposal(paths, proposal.id)

    assert result["ok"] is False
    assert result["reason"] == "rollback_conflict"
    assert target.read_text(encoding="utf-8") == "operator edit\n"


def test_create_proposal_rejects_extra_file_path_escape(tmp_path):
    paths = WorkspacePaths(root=tmp_path)

    with pytest.raises(ValueError, match="escapes proposal staging"):
        create_proposal(
            paths,
            kind="learning_update",
            summary="Reject staging path escape",
            extra_files={"../outside.txt": "must not be written\n"},
        )

    assert not (tmp_path / "outside.txt").exists()


def test_create_proposal_rejects_validation_plan_path_escape(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    with pytest.raises(ValueError, match="invalid validation plan id"):
        create_proposal(
            paths,
            kind="learning_update",
            summary="Reject plan path escape",
            validation_plan_id="../outside",
        )


def test_review_stage_can_reseal_attached_evidence_but_apply_stays_immutable(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="Attach replay before approval",
        initial_state="pending_review",
        evidence_refs=["turn:replay"],
        extra_files={"after/notes.txt": "candidate\n"},
    )
    (proposal.path / "after" / "replay.json").write_text(
        '{"ok": true}\n', encoding="utf-8"
    )

    assert reseal_candidate_bundle(paths, proposal.id, note="replay")
    assert set_state(paths, proposal.id, "approved") is not None
    applied = apply_proposal(paths, proposal.id)

    assert applied["ok"] is True
    assert (tmp_path / "replay.json").read_text(encoding="utf-8") == '{"ok": true}\n'
