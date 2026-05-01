"""Merge every ``skill.yml`` into its sibling ``SKILL.md`` frontmatter.

Hermes / Claude Code parity: a typed skill is one ``SKILL.md`` with
YAML frontmatter that contains the full action manifest plus a markdown
body for the playbook. This migration walks ``nerya/skills/builtin/``,
merges each ``skill.yml`` content into the matching ``SKILL.md``
frontmatter (yml wins on conflict because it is the existing canonical
manifest), then deletes the now-redundant ``skill.yml``.

Run once. Idempotent: re-running on an already-migrated tree is a
no-op.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILTIN = ROOT / "nerya" / "skills" / "builtin"

sys.path.insert(0, str(ROOT))

from nerya.core import yaml_io  # noqa: E402

FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<fm>.*?)\n---\s*\n", re.DOTALL)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text.strip()
    fm = m.group("fm")
    try:
        meta = yaml_io.loads(fm) or {}
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    body = text[m.end():]
    return meta, body


def _merge(yml_doc: dict, md_meta: dict) -> dict:
    """skill.yml takes precedence; SKILL.md frontmatter contributes
    fields it has that the yml does not (e.g. ``description`` overrides
    when md has a richer one).
    """
    out = dict(md_meta)
    out.update(yml_doc)  # yml wins
    # If md had a description but yml didn't, keep md's.
    if not yml_doc.get("description") and md_meta.get("description"):
        out["description"] = md_meta["description"]
    if not yml_doc.get("title") and md_meta.get("title"):
        out["title"] = md_meta["title"]
    return out


def _dump_frontmatter(meta: dict) -> str:
    raw = yaml_io.dumps(meta).rstrip()
    return f"---\n{raw}\n---\n"


def migrate_one(skill_dir: Path) -> str:
    yml = skill_dir / "skill.yml"
    md = skill_dir / "SKILL.md"
    if not yml.exists() and md.exists():
        # Already migrated (no skill.yml). Confirm SKILL.md has actions.
        text = md.read_text(encoding="utf-8")
        meta, _ = _split_frontmatter(text)
        if not meta.get("actions"):
            return f"  ! {skill_dir.name}: no skill.yml AND SKILL.md lacks actions block"
        return f"  = {skill_dir.name}: already migrated"
    if not yml.exists():
        return f"  ? {skill_dir.name}: no skill.yml and no SKILL.md (skipped)"
    yml_doc = yaml_io.load(yml) or {}
    if not isinstance(yml_doc, dict):
        return f"  ! {skill_dir.name}: skill.yml is not a YAML mapping"
    if md.exists():
        text = md.read_text(encoding="utf-8")
        md_meta, body = _split_frontmatter(text)
    else:
        md_meta, body = {}, ""
    merged = _merge(yml_doc, md_meta)
    new_text = _dump_frontmatter(merged)
    if body.strip():
        new_text += "\n" + body.strip().rstrip() + "\n"
    md.write_text(new_text, encoding="utf-8")
    yml.unlink()
    return f"  + {skill_dir.name}: merged ({len(merged.get('actions') or [])} actions)"


def main() -> int:
    if not BUILTIN.exists():
        print(f"missing builtin root: {BUILTIN}", file=sys.stderr)
        return 1
    print(f"Migrating skills under {BUILTIN}")
    for d in sorted(BUILTIN.iterdir()):
        if not d.is_dir():
            continue
        print(migrate_one(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
