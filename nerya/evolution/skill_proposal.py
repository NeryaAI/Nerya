"""Turn repeatable workflows into reviewable skill proposals."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..core import yaml_io
from ..core.time import now_iso
from ..skills.manifest import SkillManifest
from .patch_proposal import Proposal, create_proposal


_RESERVED_SKILL_IDS = {"installed", "pending", "rejected", "enabled", "trust"}


def _slugify(name: str) -> str:
    text = name.strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "_", text)
    text = text.strip("_-.")
    return text or name.strip()


def _coerce_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                out.append(text)
        return out
    text = str(value).strip()
    return [text] if text else []


def _render_bullets(lines: list[str], *, fallback: str) -> str:
    if not lines:
        return f"- {fallback}\n"
    return "".join(f"- {line}\n" for line in lines)


def _render_steps(lines: list[str]) -> str:
    if not lines:
        return (
            "1. Inspect the current request and load relevant existing skills.\n"
            "2. Follow the proven workflow captured by this skill.\n"
            "3. Return the result with concrete evidence and any remaining risk.\n"
        )
    return "".join(f"{idx}. {line}\n" for idx, line in enumerate(lines, start=1))


def _render_skill_md(
    *,
    name: str,
    description: str,
    workflow: list[str],
    triggers: list[str],
    gotchas: list[str],
    script_notes: list[str],
    reference_notes: list[str],
) -> str:
    frontmatter = yaml_io.dumps({
        "name": name,
        "description": description,
        "version": "0.1.0",
    })
    title = name.strip()
    return (
        "<!-- nerya-skill-frontmatter-start -->\n"
        "---\n"
        f"{frontmatter}"
        "---\n"
        "<!-- nerya-skill-frontmatter-end -->\n\n"
        f"# {title}\n\n"
        "## When to Use\n\n"
        f"{_render_bullets(triggers, fallback=description)}\n"
        "## Workflow\n\n"
        f"{_render_steps(workflow)}\n"
        "## Evidence to Collect\n\n"
        "- Files, commands, API responses, logs, screenshots, or test outputs that prove the result.\n"
        "- Any user constraint that materially changes the workflow.\n\n"
        "## Gotchas\n\n"
        f"{_render_bullets(gotchas, fallback='No known gotchas yet. Add them after real use.')}\n"
        "## Scripts and Helpers\n\n"
        f"{_render_bullets(script_notes, fallback='No helper script is required yet. Add scripts/ only when execution becomes repeatable.')}\n"
        "## References\n\n"
        f"{_render_bullets(reference_notes, fallback='No extra references yet.')}\n"
        "## Maintenance\n\n"
        "- If this workflow changes after a real run, patch the skill instead of leaving stale instructions in memory.\n"
        "- Keep durable procedure here; keep temporary task status in the session transcript or journal.\n"
    )


def _render_rationale(
    *,
    skill_id: str,
    description: str,
    workflow: list[str],
    triggers: list[str],
    evidence_refs: list[str],
) -> str:
    return (
        f"# Skill proposal: {skill_id}\n\n"
        f"Created at: {now_iso()}\n\n"
        "## Why\n\n"
        f"{description}\n\n"
        "This proposal captures a workflow that is expected to recur. It keeps the "
        "procedure in a lazy-loaded `SKILL.md` instead of long-term memory or an "
        "always-on prompt.\n\n"
        "## Trigger Signals\n\n"
        f"{_render_bullets(triggers, fallback='The operator or agent identifies this as a repeated workflow.')}\n"
        "## Captured Workflow\n\n"
        f"{_render_steps(workflow)}\n"
        "## Evidence References\n\n"
        f"{_render_bullets(evidence_refs, fallback='No external evidence refs were supplied.')}"
    )


def propose_skill_from_workflow(
    paths,
    *,
    name: str,
    description: str,
    workflow: Any,
    triggers: Any = None,
    evidence_refs: Any = None,
    gotchas: Any = None,
    script_notes: Any = None,
    reference_notes: Any = None,
    update_existing: bool = False,
) -> dict[str, Any]:
    """Create a PatchProposal that would add/update a workspace skill.

    The live workspace is not mutated. The proposed files are staged under
    ``evolution/proposals/<id>/after/skills/<skill_id>/`` so the existing
    proposal promotion path can apply them after operator review.
    """

    raw_name = str(name or "").strip()
    if not raw_name:
        raise ValueError("name is required")
    if len(raw_name) > 120:
        raise ValueError("name is too long")

    desc = str(description or "").strip()
    if not desc:
        raise ValueError("description is required")

    skill_id = _slugify(raw_name)
    if not skill_id:
        raise ValueError("skill id is empty")
    if skill_id in _RESERVED_SKILL_IDS:
        raise ValueError(f"reserved skill id: {skill_id}")

    active_target = Path(paths.skills) / skill_id / "SKILL.md"
    target_exists = active_target.exists()
    if target_exists and not update_existing:
        raise FileExistsError(
            f"active skill already exists at {active_target}; set update_existing=true"
        )

    workflow_lines = _coerce_lines(workflow)
    trigger_lines = _coerce_lines(triggers)
    evidence_lines = _coerce_lines(evidence_refs)
    gotcha_lines = _coerce_lines(gotchas)
    script_lines = _coerce_lines(script_notes)
    reference_lines = _coerce_lines(reference_notes)

    skill_md = _render_skill_md(
        name=raw_name,
        description=desc,
        workflow=workflow_lines,
        triggers=trigger_lines,
        gotchas=gotcha_lines,
        script_notes=script_lines,
        reference_notes=reference_lines,
    )

    validate_dir = paths.proposals / ".validate_skill_proposal" / skill_id
    validate_dir.mkdir(parents=True, exist_ok=True)
    validate_path = validate_dir / "SKILL.md"
    validate_path.write_text(skill_md, encoding="utf-8")
    try:
        parsed = SkillManifest.from_skill_md(validate_path)
    finally:
        try:
            validate_path.unlink()
            validate_dir.rmdir()
        except OSError:
            pass
    if parsed.id != skill_id:
        raise ValueError(
            f"frontmatter name resolves to {parsed.id!r}, expected {skill_id!r}"
        )

    target = f"skills/{skill_id}/SKILL.md"
    extra_files: dict[str, str] = {
        f"after/skills/{skill_id}/SKILL.md": skill_md,
        "notes/workflow_capture.md": _render_rationale(
            skill_id=skill_id,
            description=desc,
            workflow=workflow_lines,
            triggers=trigger_lines,
            evidence_refs=evidence_lines,
        ),
    }
    if script_lines:
        extra_files[f"after/skills/{skill_id}/scripts/README.md"] = (
            "# Helper script notes\n\n" + _render_bullets(script_lines, fallback="")
        )
    if reference_lines:
        extra_files[f"after/skills/{skill_id}/references/README.md"] = (
            "# Reference notes\n\n" + _render_bullets(reference_lines, fallback="")
        )

    proposal: Proposal = create_proposal(
        paths,
        kind="skill_proposal",
        summary=f"Capture recurring workflow as skill `{skill_id}`",
        rationale=_render_rationale(
            skill_id=skill_id,
            description=desc,
            workflow=workflow_lines,
            triggers=trigger_lines,
            evidence_refs=evidence_lines,
        ),
        test_plan=(
            "# Test plan\n\n"
            "1. Validate `after/skills/{skill_id}/SKILL.md` with "
            "`SkillManifest.from_skill_md`.\n"
            "2. After approval, apply the proposal and run `SkillKernel.reload()`.\n"
            "3. Confirm `skill_index refresh=true` lists the new skill.\n"
        ).format(skill_id=skill_id),
        rollback=(
            "# Rollback\n\n"
            f"Remove `skills/{skill_id}/` from the workspace and reload the skill kernel.\n"
        ),
        extra_files=extra_files,
        initial_state="pending_review",
        target=target,
        evidence_refs=evidence_lines,
        metadata={
            "skill_id": skill_id,
            "source": "workflow_to_skill",
            "update_existing": update_existing,
            "target_exists": target_exists,
        },
    )
    return {
        "ok": True,
        "skill_id": skill_id,
        "target": target,
        "proposal": proposal.asdict(),
        "skill_md_path": str(proposal.path / "after" / "skills" / skill_id / "SKILL.md"),
        "next_steps": [
            "Review the generated SKILL.md under the proposal directory.",
            "Approve/apply the proposal when the workflow is ready to become active.",
            "Reload skills and verify the new skill appears in skill_index.",
        ],
    }


__all__ = ["propose_skill_from_workflow"]
