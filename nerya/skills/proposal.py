"""Skill proposal scaffolding used by evolution.

2026-04-26: ``skill.yml`` was retired. The full typed manifest lives in
``SKILL.md`` frontmatter (skill-runtime compatibility). This scaffolder
emits exactly one ``SKILL.md`` with the manifest baked into the
frontmatter, plus a placeholder body, ``actions.py`` shim, ``references/``
dir, and ``tests/`` dir.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core import yaml_io


def _dump_skill_md(manifest: dict[str, Any], skill_id: str) -> str:
    title = manifest.get("title") or skill_id
    description = (
        manifest.get("description")
        or "Auto-generated proposal — replace this on review."
    )
    raw = yaml_io.dumps(manifest).rstrip()
    body = (
        f"# {title}\n\n"
        f"{description}\n\n"
        "Auto-generated proposal. See `references/` for design notes "
        "and `actions.py` for the runnable surface.\n"
    )
    return f"---\n{raw}\n---\n\n{body}"


def scaffold(pending_dir: Path, skill_id: str, manifest: dict[str, Any], actions_py: str) -> Path:
    target = pending_dir / skill_id
    target.mkdir(parents=True, exist_ok=True)
    # SKILL.md is the *only* manifest. yaml_io.dumps lets us reuse the
    # exact serialiser used at runtime so round-tripping is identical.
    (target / "SKILL.md").write_text(
        _dump_skill_md(manifest, skill_id), encoding="utf-8"
    )
    (target / "actions.py").write_text(actions_py, encoding="utf-8")
    (target / "references").mkdir(exist_ok=True)
    (target / "tests").mkdir(exist_ok=True)
    return target
