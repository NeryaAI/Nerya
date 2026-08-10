"""Core transform — upstream SKILL.md ⇒ Nerya-flavoured SKILL.md.

The upstream files are markdown-only (no YAML frontmatter), often with a
``description:`` line embedded in the body, and use ``reference/``
(singular) for lazy-loaded notes. Nerya requires:

* a marker-wrapped YAML frontmatter block (see ``frontmatter.py``);
* ``references/`` (plural) for lazy notes;
* ``references/`` (plural) for lazy notes.

This module performs the literal text rewrite. It is intentionally
side-effect-free at the API level: callers pass strings in and get
strings out. ``apply_to_directory`` is the only function that walks the
filesystem, and even then it only writes to the **target** workspace —
the upstream tree is treated as read-only.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .frontmatter import (
    FrontmatterMeta,
    compose_nerya_description,
    extract_upstream_description,
    render_frontmatter_block,
)
from .name_map import SkillTarget

#: Lines we strip from the upstream body because the new Nerya
#: frontmatter already carries the same information in structured form.
#: Kept conservative: we only touch the literal ``description:`` line so
#: the rest of the upstream markdown remains byte-identical (useful for
#: future three-way merge against an upstream upgrade).
_DESCRIPTION_LINE_RE = re.compile(
    r"^[ \t]*description:\s*.+?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

#: Regex used to rewrite ``reference/`` references inside the body of a
#: skill that we're moving to ``references/``. We only rewrite paths that
#: look like a relative markdown link or a fenced filesystem path; bare
#: prose mentions of the word "reference" are left alone.
_REFERENCE_PATH_RE = re.compile(r"(?P<prefix>[\(\[\s`])reference/(?P<rest>[^\s`\)\]]+)")


@dataclass(frozen=True)
class TransformResult:
    """Outcome of a single transform operation."""

    target: SkillTarget
    upstream_skill_md: Path
    nerya_skill_md_text: str
    upstream_description: str
    nerya_description: str
    references_singular_to_plural: bool
    extra_files_copied: tuple[Path, ...]


def transform_skill_md(
    upstream_text: str,
    target: SkillTarget,
    *,
    license_: str = "Apache-2.0",
    author: str = "Anthropic",
    requires_integration: str = "",
    version: str = "0.0.1",
) -> tuple[str, str, str]:
    """Render the Nerya-flavoured SKILL.md text for one upstream skill.

    Returns ``(nerya_text, upstream_description, nerya_description)``.

    The body rewrite is conservative on purpose: only the upstream
    ``description:`` line is removed (because we hoisted it into YAML
    frontmatter), and any in-body ``reference/`` paths are rewritten to
    ``references/`` so they line up with the directory rename done by
    :func:`apply_to_directory`. Everything else — H1 title, workflow,
    examples — is preserved verbatim so future ``upgrade`` runs can do a
    clean text diff against the upstream original.
    """
    upstream_description = extract_upstream_description(upstream_text)
    nerya_description = compose_nerya_description(
        upstream_description,
        upstream_vertical=target.upstream_vertical,
        upstream_skill=target.upstream_skill,
    )

    body = _DESCRIPTION_LINE_RE.sub("", upstream_text, count=1)
    body = _strip_excess_blank_lines(body)
    body = _rewrite_reference_paths(body)

    meta = FrontmatterMeta(
        name=target.skill_id,
        description=nerya_description,
        license=license_,
        author=author,
        version=version,
        requires_integration=requires_integration,
    )
    fm = render_frontmatter_block(meta)

    nerya_text = "\n".join([fm, "", body.strip()]).rstrip() + "\n"
    return nerya_text, upstream_description, nerya_description


def apply_to_directory(
    *,
    upstream_skill_dir: Path,
    target: SkillTarget,
    workspace_root: Path,
    license_: str = "Apache-2.0",
    author: str = "Anthropic",
    requires_integration: str = "",
    version: str = "0.0.1",
    dry_run: bool = True,
) -> TransformResult:
    """Materialise one upstream skill directory into the workspace.

    Layout produced
    ---------------
    ``<workspace_root>/<target.rel_skill_dir>/SKILL.md``
    ``<workspace_root>/<target.rel_skill_dir>/references/<...>``     (if upstream had ``reference/``)
    ``<workspace_root>/<target.rel_skill_dir>/<aux>``                (any other top-level files copied verbatim)

    This function never touches the upstream tree.
    """
    upstream_md = upstream_skill_dir / "SKILL.md"
    if not upstream_md.is_file():
        raise FileNotFoundError(f"upstream SKILL.md missing: {upstream_md}")

    upstream_text = upstream_md.read_text(encoding="utf-8")
    nerya_text, upstream_desc, nerya_desc = transform_skill_md(
        upstream_text,
        target,
        license_=license_,
        author=author,
        requires_integration=requires_integration,
        version=version,
    )

    target_dir = target.absolute_skill_dir(workspace_root)
    target_md = target.absolute_skill_md(workspace_root)

    extra_files: list[Path] = []
    references_renamed = False

    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)
        target_md.write_text(nerya_text, encoding="utf-8")

        for path in _iter_extra_assets(upstream_skill_dir):
            rel = path.relative_to(upstream_skill_dir)
            new_rel = _rename_reference_dir(rel)
            if new_rel != rel:
                references_renamed = True
            dest = target_dir / new_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            extra_files.append(dest)
    else:
        for path in _iter_extra_assets(upstream_skill_dir):
            rel = path.relative_to(upstream_skill_dir)
            new_rel = _rename_reference_dir(rel)
            if new_rel != rel:
                references_renamed = True
            extra_files.append(target_dir / new_rel)

    return TransformResult(
        target=target,
        upstream_skill_md=upstream_md,
        nerya_skill_md_text=nerya_text,
        upstream_description=upstream_desc,
        nerya_description=nerya_desc,
        references_singular_to_plural=references_renamed,
        extra_files_copied=tuple(extra_files),
    )


# ---- internals -------------------------------------------------------------


def _iter_extra_assets(upstream_skill_dir: Path) -> Iterator[Path]:
    """Yield every file under *upstream_skill_dir* except ``SKILL.md``."""
    for path in sorted(upstream_skill_dir.rglob("*")):
        if path.is_dir():
            continue
        if path.name == "SKILL.md" and path.parent == upstream_skill_dir:
            continue
        yield path


def _rename_reference_dir(rel: Path) -> Path:
    """Rewrite ``reference/<x>`` to ``references/<x>`` to match Nerya layout."""
    parts = list(rel.parts)
    if parts and parts[0] == "reference":
        parts[0] = "references"
        return Path(*parts)
    return rel


def _strip_excess_blank_lines(text: str) -> str:
    """Collapse runs of 3+ blank lines to a single blank line."""
    return re.sub(r"\n{3,}", "\n\n", text)


def _rewrite_reference_paths(body: str) -> str:
    """Rewrite in-body ``reference/<x>`` paths to ``references/<x>``."""
    return _REFERENCE_PATH_RE.sub(
        lambda m: f"{m.group('prefix')}references/{m.group('rest')}",
        body,
    )


def _relative_repo_path(upstream_md: Path, upstream_repo_root: Path | None) -> str:
    """Compute a stable relative path string for the ``adapted_from`` block."""
    if upstream_repo_root is None:
        return upstream_md.as_posix()
    try:
        return upstream_md.relative_to(upstream_repo_root).as_posix()
    except ValueError:
        return upstream_md.as_posix()


__all__ = [
    "TransformResult",
    "apply_to_directory",
    "transform_skill_md",
]
