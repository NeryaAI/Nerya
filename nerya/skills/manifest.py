"""Skill manifest loader (Anthropic Skill spec).

The on-disk shape mirrors the agent skill runtime ``SKILL.md`` exactly:

* The frontmatter is a tiny YAML block with **only** the fields the
  spec defines: ``name``, ``description``, ``version``, ``license``,
  ``author``. Anything else is ignored.
* The markdown body is the operator-facing playbook the agent reads
  when it loads the skill.
* Any executable scripts that ship with the skill live under
  ``scripts/`` and are invoked by the agent through ``run_shell``;
  the loader does **not** import them. ``actions == {}`` for every
  manifest loaded by :meth:`SkillManifest.from_skill_md`.

Procedural single-file ``SKILL.md`` skills (one synthesised ``run``
action) live in :mod:`nerya.skills.procedural`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import re as _re

from ..core import yaml_io
from ..core.errors import SkillManifestError


# Action-name prefixes that imply a pure-read operation. Still used by
# the dynamic MCP bridge and by some operator-side preset utilities.
_READ_PREFIXES: tuple[str, ...] = (
    "list_",
    "get_",
    "read_",
    "view_",
    "show_",
    "find_",
    "search_",
    "fetch_",
    "describe_",
    "inspect_",
    "explain_",
    "preview_",
    "lookup_",
    "report_",
    "history_",
    "status_",
    "summary_",
)

#: Standalone read-only action names.
_READ_NAMES: frozenset[str] = frozenset(
    {
        "list",
        "get",
        "read",
        "view",
        "show",
        "find",
        "search",
        "fetch",
        "describe",
        "inspect",
        "explain",
        "preview",
        "lookup",
        "report",
        "history",
        "status",
        "summary",
    }
)


def action_is_read_only(action_name: str) -> bool:
    """Heuristic: does the action's *name* imply a pure-read operation?"""

    if action_name in _READ_NAMES:
        return True
    return any(action_name.startswith(p) for p in _READ_PREFIXES)


@dataclass
class ActionSpec:
    """Operational metadata for a single skill action.

    Retained as a stable type so the runtime, flow runner, and MCP
    dynamic-tool bridge keep working for *procedural* and user-
    installed skills that synthesise actions at load time. Manifests
    loaded from ``builtin/`` carry an empty ``actions`` map.
    """

    name: str
    title: str = ""
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    permissions: list[str] = field(default_factory=list)
    risk_gate: str = "n/a"       # "required" | "optional" | "n/a"
    approval_gate: str = "n/a"   # "always" | "threshold" | "n/a"
    context_policy: str = "scoped_strategy"
    journal: bool = True
    flow: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ready"
    tags: list[str] = field(default_factory=list)
    requires_env: list[str] = field(default_factory=list)
    requires_secret: list[str] = field(default_factory=list)
    check_fn: str = ""
    result_kind: str = ""
    path_scope: str = ""


@dataclass
class SkillManifest:
    """A loaded skill — frontmatter + (optional) procedural actions.

    Aligned with the Anthropic Agent Skill spec (agent skill runtime).
    The YAML frontmatter only carries:

    * ``name`` (required) — agent-visible skill name.
    * ``description`` (required) — one-paragraph trigger blurb.
    * ``version`` — semver (defaults to ``0.1.0``).
    * ``license`` / ``author`` — provenance.
    * ``requires_integration`` — optional. Name of an entry under
      ``integrations.<name>`` whose ``enabled`` flag must be true for
      the skill to be registered. Nerya-specific extension; strict
      Anthropic-spec loaders can ignore it without harm.

    Anything else in the frontmatter is ignored. Actions, when
    present, come from procedural single-file SKILL.md loading and
    not from auto-importing scripts.
    """

    id: str
    version: str
    title: str
    description: str
    permissions: list[str]
    actions: dict[str, ActionSpec]
    source: str = "builtin"
    path: Path | None = None
    status: str = "ready"
    tags: list[str] = field(default_factory=list)
    instructions: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    requires_integration: str = ""

    def is_proposal_only(self) -> bool:
        """True when the manifest is a proposal-only scaffold."""
        if self.status == "proposal_only_unimplemented":
            return True
        return any(a.status == "proposal_only_unimplemented"
                   for a in self.actions.values())

    @classmethod
    def from_skill_md(cls, md_path: Path) -> "SkillManifest":
        """Load a SKILL.md by parsing frontmatter + body only.

        The loader is deliberately minimal: it parses the YAML
        frontmatter, captures the markdown body, and stops. It does
        **not** scan ``scripts/`` and does **not** import any Python.
        The agent reads the markdown body and decides how to invoke
        any bundled scripts (typically via ``run_shell``).

        The resulting manifest has ``actions == {}`` and is therefore
        invisible to ``runtime.call``; that is intentional.

        The frontmatter only carries:

        * ``name`` (required)
        * ``description`` (required)
        * ``version`` / ``license`` / ``author`` (optional)

        Anything else is ignored.
        """

        if not md_path.exists():
            raise SkillManifestError(f"missing SKILL.md: {md_path}")
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillManifestError(f"cannot read {md_path}: {exc}") from exc

        doc, body = _split_frontmatter(text, source=md_path)
        return cls._build(
            doc,
            body=body,
            source_path=md_path,
            actions={},
        )

    @classmethod
    def _build(
        cls,
        doc: dict[str, Any],
        *,
        body: str,
        source_path: Path,
        actions: dict[str, ActionSpec],
    ) -> "SkillManifest":
        """Project Anthropic-spec frontmatter onto a manifest.

        Only ``name`` / ``description`` / ``version`` / ``license`` /
        ``author`` are read off ``doc``; anything else is ignored.
        """

        name = doc.get("name") or source_path.parent.name
        if not name:
            raise SkillManifestError(
                f"{source_path}: frontmatter missing required `name`"
            )
        skill_id = _slugify(str(name))

        description = str(doc.get("description") or "").strip()
        version = str(doc.get("version") or "0.1.0").strip()
        requires_integration = str(doc.get("requires_integration") or "").strip()

        skill_permissions: list[str] = []
        seen: set[str] = set()
        for a in actions.values():
            for perm in a.permissions:
                if perm not in seen:
                    seen.add(perm)
                    skill_permissions.append(perm)

        return cls(
            id=skill_id,
            version=version,
            title=str(name),
            description=description,
            permissions=skill_permissions,
            actions=actions,
            source="builtin",
            path=source_path.parent,
            status="ready",
            tags=[],
            instructions=body,
            metadata={},
            requires_integration=requires_integration,
        )


# --------------------------------------------------------------------------
# Frontmatter parsing helpers
# --------------------------------------------------------------------------


_FRONTMATTER_RE = _re.compile(
    # Tolerate a leading byte-order mark, blank lines, and HTML comments
    # before the frontmatter. Markdown autoformatters in some IDEs treat
    # ``name: foo\n---`` as a setext H2 if ``---`` immediately precedes
    # body content, so a sentinel HTML comment (``<!-- ... -->``) before
    # the opening ``---`` is the cheapest reliable way to keep YAML
    # frontmatter intact across formatter runs.
    r"\A(?:\ufeff)?(?:[ \t]*\n|<!--.*?-->[ \t]*\n)*"
    r"---\s*\n(?P<fm>.*?)\n---\s*\n",
    _re.DOTALL,
)


def _split_frontmatter(
    text: str,
    *,
    source: Path,
) -> tuple[dict[str, Any], str]:
    """Parse a ``SKILL.md`` into ``(frontmatter_doc, body)``."""

    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise SkillManifestError(
            f"{source}: missing YAML frontmatter (`---` block)"
        )
    fm_block = m.group("fm")
    try:
        doc = yaml_io.loads(fm_block) or {}
    except Exception as exc:
        raise SkillManifestError(
            f"{source}: invalid YAML frontmatter: {exc}"
        ) from exc
    if not isinstance(doc, dict):
        raise SkillManifestError(
            f"{source}: frontmatter must be a YAML mapping"
        )
    body = text[m.end():].strip()
    return doc, body


def _slugify(name: str) -> str:
    """Normalise an Anthropic-spec ``name`` into an ASCII id."""

    s = name.strip().lower()
    s = _re.sub(r"[^a-z0-9_.-]+", "_", s)
    s = s.strip("_-.")
    return s or name.strip()


# --------------------------------------------------------------------------
# CLI helper used by every standalone skill action script
# --------------------------------------------------------------------------


def cli_main(run_callable, *, default_payload: dict[str, Any] | None = None) -> None:
    """Standard ``__main__`` driver for ``scripts/<action>.py`` modules.

    Reads a JSON payload from ``--json '{...}'``, ``--payload-file path``,
    or stdin (when no flags are passed and stdin is not a tty), invokes
    ``run_callable(ctx=None, **payload)``, and prints the JSON result to
    stdout. Errors are written to stderr with a non-zero exit code.

    The script is therefore runnable both from in-process callers (via
    ``run(ctx, **payload)`` import) and from a shell (via
    ``python -m nerya.skills.builtin.<skill>.scripts.<action>``).
    """

    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--json",
        dest="payload_json",
        default=None,
        help="JSON-encoded payload",
    )
    parser.add_argument(
        "--payload-file",
        dest="payload_file",
        default=None,
        help="path to a JSON file containing the payload",
    )
    args = parser.parse_args()

    payload: dict[str, Any]
    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
    elif args.payload_json:
        payload = json.loads(args.payload_json)
    elif not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        payload = json.loads(raw) if raw else {}
    else:
        payload = dict(default_payload or {})

    try:
        result = run_callable(None, **payload)
    except Exception as exc:  # pragma: no cover — surfaced to stderr
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str))
    sys.stdout.write("\n")


__all__ = [
    "ActionSpec",
    "SkillManifest",
    "action_is_read_only",
    "cli_main",
]
