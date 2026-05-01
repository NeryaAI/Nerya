"""Audit skill manifests against the official Agent Skills frontmatter spec.

Reference: https://docs.anthropic.com/en/docs/agent-skills (mirrored at
https://www.runoob.com/skills/skills-structure.html). The official spec
allows exactly these top-level keys in ``SKILL.md`` frontmatter:

    name           required, lowercase + digits + '-', <=64 chars
    description    required, <=1024 chars
    license        optional
    compatibility  optional, <=500 chars
    metadata       optional dict (free-form extension point)
    allowed-tools  optional list

Anything else is a Nerya-local extension. Historically Nerya has been
cramming `id`, `version`, `permissions`, `actions[]`, `tags`, `dashboard`,
`status`, plus dozens of per-action `agent_*` / `risk_gate` /
`approval_gate` / `path_scope` / `result_kind` keys directly into the
top level. This script walks every built-in skill (and optionally any
workspace-installed skill) and prints a table of non-standard fields so
we know exactly what needs to migrate under `metadata.nerya.*` (or be
deleted outright) before the loop refactor finishes.

Usage::

    python scripts/audit_skill_fields.py
    python scripts/audit_skill_fields.py --root path/to/skills
    python scripts/audit_skill_fields.py --json

The script does not modify any skill — it is read-only audit output.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml  # type: ignore
except Exception:  # pragma: no cover - resolved at runtime
    _yaml = None  # type: ignore


OFFICIAL_TOP_LEVEL = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}


OFFICIAL_PER_ACTION: set[str] = set()
"""The official spec has no concept of an action object at all.

Every key inside an entry of a Nerya-style ``actions:`` list is
non-standard with respect to the Agent Skills spec, but some of them
are obviously load-bearing for the runtime (``name``, ``description``,
``input_schema``). The audit reports the full list and lets the
operator decide which to keep under ``metadata.nerya.actions[*].*`` and
which to delete.
"""


KNOWN_RUNTIME_PER_ACTION = {
    "name",
    "description",
    "input_schema",
    "output_schema",
}
"""Per-action keys we expect to keep (under ``metadata.nerya.actions``)."""


def _read_frontmatter(path: Path) -> tuple[dict[str, Any] | None, str]:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(?P<fm>.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return None, "no-frontmatter"
    block = m.group("fm")
    if _yaml is None:
        return None, "pyyaml-missing"
    try:
        doc = _yaml.safe_load(block)
    except Exception as exc:
        return None, f"yaml-error: {exc.__class__.__name__}: {exc}"
    if not isinstance(doc, dict):
        return None, "not-a-mapping"
    return doc, ""


def _audit_skill(md_path: Path) -> dict[str, Any]:
    doc, err = _read_frontmatter(md_path)
    rel = md_path
    out: dict[str, Any] = {
        "path": str(rel),
        "ok": doc is not None,
        "error": err or None,
        "name": None,
        "description_present": False,
        "extra_top_level": [],
        "actions_count": 0,
        "extra_per_action": {},
        "missing_required": [],
    }
    if doc is None:
        return out

    name = doc.get("name") or doc.get("id")
    out["name"] = name
    if not doc.get("name"):
        out["missing_required"].append("name")
    if not doc.get("description"):
        out["missing_required"].append("description")
    out["description_present"] = bool(doc.get("description"))

    extras = sorted(set(doc.keys()) - OFFICIAL_TOP_LEVEL)
    out["extra_top_level"] = extras

    actions = doc.get("actions")
    per_action_extras: dict[str, int] = {}
    if isinstance(actions, list):
        out["actions_count"] = len(actions)
        for entry in actions:
            if not isinstance(entry, dict):
                continue
            for key in entry.keys():
                if key in KNOWN_RUNTIME_PER_ACTION:
                    continue
                per_action_extras[key] = per_action_extras.get(key, 0) + 1
    out["extra_per_action"] = per_action_extras
    return out


def _walk_skills(root: Path) -> list[Path]:
    return sorted(root.rglob("SKILL.md"))


def _print_table(rows: list[dict[str, Any]], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return

    extra_keys: dict[str, int] = {}
    per_action_universe: dict[str, int] = {}
    for r in rows:
        for k in r.get("extra_top_level") or []:
            extra_keys[k] = extra_keys.get(k, 0) + 1
        for k, n in (r.get("extra_per_action") or {}).items():
            per_action_universe[k] = per_action_universe.get(k, 0) + int(n)

    print("=" * 78)
    print("Skill audit: non-standard fields vs. Agent Skills spec")
    print("=" * 78)
    print()
    print(f"Skills scanned: {len(rows)}")
    print(
        "Skills with parser errors: "
        f"{sum(1 for r in rows if not r.get('ok'))}"
    )
    print()
    print("Top-level non-standard keys (count = skills using it):")
    print("-" * 78)
    print(f"{'KEY':<32} {'COUNT':>8}")
    for k, n in sorted(extra_keys.items(), key=lambda x: (-x[1], x[0])):
        print(f"{k:<32} {n:>8}")
    print()
    print("Per-action non-standard keys (count = total occurrences):")
    print("-" * 78)
    print(f"{'KEY':<40} {'COUNT':>8}")
    for k, n in sorted(per_action_universe.items(), key=lambda x: (-x[1], x[0])):
        print(f"{k:<40} {n:>8}")
    print()
    print("Per-skill detail:")
    print("-" * 78)
    print(
        f"{'NAME':<28} {'ACTIONS':>8} {'TOP-EXTRA':>10} {'ACT-EXTRA':>10}"
        "  PATH"
    )
    for r in sorted(rows, key=lambda x: x.get("name") or x.get("path") or ""):
        if not r.get("ok"):
            print(
                f"{(r.get('name') or '?'):<28} "
                f"{'?':>8} {'?':>10} {'?':>10}"
                f"  {r['path']}  [error: {r.get('error')}]"
            )
            continue
        top = len(r.get("extra_top_level") or [])
        act = sum((r.get("extra_per_action") or {}).values())
        print(
            f"{(r.get('name') or '?'):<28} "
            f"{r.get('actions_count', 0):>8} "
            f"{top:>10} {act:>10}"
            f"  {r['path']}"
        )
    print()
    missing = [r for r in rows if r.get("missing_required")]
    if missing:
        print("Skills missing required fields:")
        print("-" * 78)
        for r in missing:
            keys = ", ".join(r.get("missing_required") or [])
            print(f"  {r.get('name') or '?'}  [missing: {keys}]  {r['path']}")
        print()
    print("Done.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parents[1]
    ap.add_argument(
        "--root",
        default=str(here / "nerya" / "skills" / "builtin"),
        help="Root directory to walk for SKILL.md files",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a text table",
    )
    args = ap.parse_args(argv)

    if _yaml is None:
        print(
            "PyYAML is required for this audit (pip install pyyaml).",
            file=sys.stderr,
        )
        return 2

    root = Path(args.root)
    if not root.exists():
        print(f"root does not exist: {root}", file=sys.stderr)
        return 2

    paths = _walk_skills(root)
    rows = [_audit_skill(p) for p in paths]
    _print_table(rows, json_mode=bool(args.json))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
