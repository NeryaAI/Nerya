"""Phase 13 helper — annotate every builtin SKILL.md with the new
``metadata.nerya.style`` field and append a short Playbook section to
the body when it is missing.

The new ``SkillIndex`` (``nerya.tools.native.skill.SkillIndex``) reads
``metadata.nerya.style`` to decide how to render the skill in
``skill_index`` / ``skill_view``. ``style: playbook`` flags coding-only
skills (operator) whose surface is now covered by native tools; every
other builtin gets ``style: domain``.

Usage
-----
::

    python scripts/migrate_skill_md_playbook.py [--dry-run]

The script is idempotent — running it twice produces no second-pass
edits.

Plan refs
---------
* docs/agent-harness-comparison-and-refactor-todo.md Phase 13
* docs/agent-intelligence-gap-and-cursor-refactor-plan.md §3.6
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Optional

import yaml


PLAYBOOK_ONLY_SKILLS = {
    # operator now layers on native tools — its skill is a playbook,
    # not a manifest of actions.
    "operator",
}


PLAYBOOK_FOOTER_HEADING = "## When to reach for this skill"


def _split_frontmatter(text: str) -> tuple[Optional[dict], str, str]:
    """Return ``(frontmatter, body, raw_frontmatter_text)``.

    When the file has no leading ``--- ... ---`` block the first slot is
    ``None`` and the body is the original text.
    """

    m = re.match(r"^---\s*\n+(?P<fm>.*?)\n---\s*\n(?P<body>.*)$", text, re.DOTALL)
    if not m:
        return None, text, ""
    raw = m.group("fm")
    try:
        doc = yaml.safe_load(raw) or {}
    except Exception:
        return None, text, raw
    if not isinstance(doc, dict):
        return None, text, raw
    return doc, m.group("body"), raw


def _ensure_metadata_style(doc: dict, *, skill_id: str) -> bool:
    """Set ``metadata.nerya.style`` when missing. Returns True on change."""

    metadata = doc.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {"raw": metadata}
        doc["metadata"] = metadata
    nerya_meta = metadata.setdefault("nerya", {})
    if not isinstance(nerya_meta, dict):
        nerya_meta = {"raw": nerya_meta}
        metadata["nerya"] = nerya_meta
    if nerya_meta.get("style"):
        return False
    nerya_meta["style"] = (
        "playbook" if skill_id in PLAYBOOK_ONLY_SKILLS else "domain"
    )
    nerya_meta.setdefault("skill_id", skill_id)
    return True


def _ensure_playbook_section(body: str, *, skill_id: str, manifest: dict) -> tuple[str, bool]:
    """Append a Playbook section if the body lacks one. Returns (body, changed)."""

    if PLAYBOOK_FOOTER_HEADING in body:
        return body, False
    if skill_id in PLAYBOOK_ONLY_SKILLS:
        # Operator already authored its own playbook in Phase 12.
        return body, False

    description = (manifest.get("description") or "").strip()
    actions = manifest.get("actions") or []
    if isinstance(actions, list):
        action_names = [a.get("name") for a in actions if isinstance(a, dict) and a.get("name")]
    else:
        action_names = []

    bullets = []
    for name in action_names[:10]:
        bullets.append(f"- `{name}`")
    if len(action_names) > 10:
        bullets.append(f"- … and {len(action_names) - 10} more")

    block = [
        "",
        PLAYBOOK_FOOTER_HEADING,
        "",
        f"This skill exposes **{skill_id}** domain operations to the agent.",
        "",
    ]
    if description:
        block.append(description)
        block.append("")
    block.extend([
        "Actions registered with the native ToolRegistry:",
        "",
    ])
    block.extend(bullets if bullets else ["- _no actions exposed_"])
    block.extend([
        "",
        "Call `skill_view` for the full action schemas, or invoke each action via",
        f"its bridged tool name `skill_{skill_id}__<action>`.",
        "",
    ])
    if not body.endswith("\n"):
        body = body + "\n"
    return body + "\n".join(block).rstrip() + "\n", True


def migrate_one(path: Path, *, dry_run: bool = False) -> dict:
    text = path.read_text(encoding="utf-8")
    doc, body, _raw = _split_frontmatter(text)
    if doc is None:
        return {"path": str(path), "status": "no_frontmatter"}

    skill_id = doc.get("id") or doc.get("name") or path.parent.name
    if not isinstance(skill_id, str):
        return {"path": str(path), "status": "invalid_id"}

    fm_changed = _ensure_metadata_style(doc, skill_id=skill_id)
    new_body, body_changed = _ensure_playbook_section(body, skill_id=skill_id, manifest=doc)

    if not (fm_changed or body_changed):
        return {"path": str(path), "status": "unchanged"}

    new_fm = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True).strip()
    new_text = f"---\n{new_fm}\n---\n{new_body if body_changed else body}"
    if not dry_run:
        path.write_text(new_text, encoding="utf-8")
    return {
        "path": str(path),
        "status": "updated" if not dry_run else "would_update",
        "fm_changed": fm_changed,
        "body_changed": body_changed,
        "skill_id": skill_id,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent / "nerya" / "skills" / "builtin"),
    )
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"missing: {root}")
        return 1
    summary = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        md = entry / "SKILL.md"
        if not md.exists():
            continue
        result = migrate_one(md, dry_run=args.dry_run)
        summary.append(result)
    for row in summary:
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
