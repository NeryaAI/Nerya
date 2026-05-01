"""Bulk-migrate built-in skill ``actions.py`` modules into ``scripts/handlers.py``.

This is a **one-time development helper** that implements Phase 3 of the
``2026-04-25-skill-md-only-migration-plan.md`` migration:

For every skill under ``nerya/skills/builtin/<name>_skill/`` whose
``actions.py`` is still the home of executable logic, move the entire
file into ``scripts/handlers.py`` (with relative imports bumped by one
package level so they continue to resolve), then rewrite ``actions.py``
as a thin compatibility shim that re-exports the public callables from
``.scripts.handlers``.

This satisfies the migration rule:

    "executable logic belongs in scripts/, not actions.py or YAML"

Skills that already follow the per-function ``scripts/<name>.py``
pattern (``team_skill``, ``strategy_validation_skill``) are left
untouched. Skills whose ``actions.py`` is *already* a re-export shim
(detected by the marker comment ``# migrated_to_scripts``) are also
skipped, so the script is idempotent.

Usage::

    python scripts/migrate_actions_to_handlers.py             # migrate all
    python scripts/migrate_actions_to_handlers.py --skill foo  # one skill
    python scripts/migrate_actions_to_handlers.py --check      # dry-run only
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path


_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_BUILTIN = _ROOT / "nerya" / "skills" / "builtin"

_ALREADY_MIGRATED_PER_FUNCTION = {
    "team_skill",
    "strategy_validation_skill",
}

_MARKER = "# migrated_to_scripts"


# Match ``from XYZ import ...`` where XYZ starts with one or more dots.
_RE_FROM_RELATIVE = re.compile(r"^(\s*from\s+)(\.+)(\S*)(\s+import\b)")


def _bump_relative_imports(src: str) -> str:
    """Bump every ``from .X import Y`` / ``from .. import Y`` by +1 dot.

    Moving a module from ``<pkg>/actions.py`` into
    ``<pkg>/scripts/handlers.py`` makes it one level deeper in the
    package tree, so every dotted relative import must gain one dot.
    """

    out: list[str] = []
    for line in src.splitlines(keepends=True):
        m = _RE_FROM_RELATIVE.match(line)
        if m:
            head, dots, tail_name, import_kw = m.groups()
            new_dots = "." * (len(dots) + 1)
            replaced = (
                f"{head}{new_dots}{tail_name}{import_kw}"
                + line[m.end():]
            )
            out.append(replaced)
        else:
            out.append(line)
    return "".join(out)


def _public_names(src: str) -> list[str]:
    """Return public top-level def/async-def/class names (no leading ``_``)."""

    tree = ast.parse(src)
    names: list[str] = []
    for node in tree.body:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            if not node.name.startswith("_"):
                names.append(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Name)
                    and not tgt.id.startswith("_")
                    and tgt.id not in {"__all__"}
                ):
                    names.append(tgt.id)
    # Preserve declaration order, dedupe.
    seen: set[str] = set()
    ordered: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def _make_shim(skill_id: str, public_names: list[str]) -> str:
    """Generate a compatibility shim ``actions.py``.

    The shim:

    1. Eagerly re-exports every *public* top-level callable / class so
       static imports (``from <pkg>.actions import foo``) keep working.
    2. Falls back to module-level ``__getattr__`` for *private* names
       (``_helper``, ``_VALID_DRIVERS``, etc.) and any other attribute
       defined on ``handlers`` but not present in the public list.
       This is needed because some tests reach into private module
       members for monkeypatching.
    """

    if public_names:
        all_block = (
            "__all__ = [\n"
            + "".join(f"    {n!r},\n" for n in public_names)
            + "]\n"
        )
    else:
        all_block = "__all__: list[str] = []\n"
    body = (
        f'"""Compatibility shim for {skill_id}.\n\n'
        "Phase 3 of the SKILL.md-only migration moves executable logic\n"
        "into ``scripts/handlers.py``. This module re-exports the public\n"
        "callables so existing imports (``nerya.skills.builtin."
        f"{skill_id}.actions``) continue to resolve while the canonical\n"
        "implementation lives under ``scripts/``.\n\n"
        "Do not add new logic here — extend ``scripts/handlers.py`` (or\n"
        "split into per-action ``scripts/<action>.py`` modules) instead.\n"
        '"""\n\n'
        f"{_MARKER}\n"
        "from __future__ import annotations\n\n"
        "import sys as _sys\n"
        "import types as _types\n\n"
        "from .scripts import handlers as _handlers\n\n"
    )
    if public_names:
        body += "from .scripts.handlers import (  # noqa: F401\n"
        for n in public_names:
            body += f"    {n},\n"
        body += ")\n\n"
    body += (
        "def __getattr__(name: str):\n"
        "    \"\"\"Forward private/non-action attributes to the handlers "
        "module.\n\n"
        "    Some tests and runtime helpers reach into private members\n"
        "    (``_VALID_DRIVERS``, ``_deterministic_parse``, ...) for\n"
        "    monkey-patching or introspection. Forwarding via\n"
        "    ``__getattr__`` keeps them reachable without re-exporting\n"
        "    them in the public surface.\n"
        "    \"\"\"\n\n"
        "    try:\n"
        "        return getattr(_handlers, name)\n"
        "    except AttributeError as exc:\n"
        "        raise AttributeError(\n"
        f"            f\"module 'nerya.skills.builtin.{skill_id}.actions' \"\n"
        "            f\"has no attribute {name!r}\"\n"
        "        ) from exc\n\n"
        "def __dir__() -> list[str]:\n"
        "    return sorted(set(__all__) | set(dir(_handlers)))\n\n"
        "class _ForwardingModule(_types.ModuleType):\n"
        "    \"\"\"Module subclass that mirrors writes to the handlers module.\n\n"
        "    Existing tests use ``monkeypatch.setattr(actions, \"_foo\", x)``\n"
        "    to swap in fake implementations. With the new layout the real\n"
        "    callable lives in ``scripts.handlers``, so the patch must\n"
        "    propagate there to actually take effect at call time.\n"
        "    \"\"\"\n\n"
        "    def __setattr__(self, name: str, value):\n"
        "        super().__setattr__(name, value)\n"
        "        if name.startswith(\"__\") and name.endswith(\"__\"):\n"
        "            return\n"
        "        try:\n"
        "            setattr(_handlers, name, value)\n"
        "        except Exception:\n"
        "            pass\n\n"
        "    def __delattr__(self, name: str):\n"
        "        super().__delattr__(name)\n"
        "        if name.startswith(\"__\") and name.endswith(\"__\"):\n"
        "            return\n"
        "        try:\n"
        "            delattr(_handlers, name)\n"
        "        except Exception:\n"
        "            pass\n\n"
        "_sys.modules[__name__].__class__ = _ForwardingModule\n\n"
    )
    body += all_block
    return body


def _make_scripts_init(skill_id: str, public_names: list[str]) -> str:
    body = (
        f'"""``{skill_id}`` script package.\n\n'
        "Phase 3 of the SKILL.md-only migration: executable logic\n"
        "lives in ``handlers.py`` (and may be split into per-action\n"
        "modules over time).\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
    )
    if public_names:
        body += "from .handlers import (  # noqa: F401\n"
        for n in public_names:
            body += f"    {n},\n"
        body += ")\n"
    return body


def _migrate_one(
    skill_dir: Path, *, dry: bool, refresh_shim: bool = False
) -> dict[str, object]:
    skill_id = skill_dir.name
    actions_path = skill_dir / "actions.py"
    info: dict[str, object] = {"skill": skill_id, "status": "skipped"}
    if not actions_path.exists():
        info["reason"] = "no actions.py"
        return info
    if skill_id in _ALREADY_MIGRATED_PER_FUNCTION:
        info["reason"] = "already per-function migrated"
        info["status"] = "skipped"
        return info
    src = actions_path.read_text(encoding="utf-8")
    handlers_path = skill_dir / "scripts" / "handlers.py"
    if _MARKER in src:
        if not refresh_shim or not handlers_path.exists():
            info["reason"] = "already migrated"
            info["status"] = "skipped"
            return info
        # Refresh shim from the existing handlers.py — public surface may
        # have changed (e.g. private helpers we want to expose).
        handlers_src = handlers_path.read_text(encoding="utf-8")
        public_names = _public_names(handlers_src)
        shim = _make_shim(skill_id, public_names)
        init = _make_scripts_init(skill_id, public_names)
        info.update({
            "status": "would-refresh-shim" if dry else "refreshed-shim",
            "public_names": public_names,
        })
        if dry:
            return info
        actions_path.write_text(shim, encoding="utf-8")
        init_path = skill_dir / "scripts" / "__init__.py"
        # Always rewrite scripts/__init__.py for already-migrated skills
        # so it stays consistent with the shim's public surface.
        init_path.write_text(init, encoding="utf-8")
        return info

    public_names = _public_names(src)
    bumped = _bump_relative_imports(src)
    shim = _make_shim(skill_id, public_names)
    init = _make_scripts_init(skill_id, public_names)

    info.update({
        "status": "would-migrate" if dry else "migrated",
        "public_names": public_names,
        "imports_bumped": bumped != src,
    })

    if dry:
        return info

    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "handlers.py").write_text(bumped, encoding="utf-8")
    init_path = scripts_dir / "__init__.py"
    # Only overwrite ``scripts/__init__.py`` if it does not already export
    # something custom (some skills may have an existing scripts dir).
    if not init_path.exists() or init_path.read_text(
        encoding="utf-8"
    ).strip() in ("", '""""""'):
        init_path.write_text(init, encoding="utf-8")
    actions_path.write_text(shim, encoding="utf-8")
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skill",
        action="append",
        default=None,
        help="only migrate the given skill id (folder name); repeatable",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="dry-run; report what would change without writing files",
    )
    parser.add_argument(
        "--refresh-shim",
        action="store_true",
        help=(
            "for skills already migrated, regenerate ``actions.py`` and "
            "``scripts/__init__.py`` from the current ``scripts/handlers.py``."
        ),
    )
    args = parser.parse_args(argv)

    targets: list[Path] = []
    for d in sorted(_BUILTIN.iterdir()):
        if not d.is_dir():
            continue
        if args.skill is not None and d.name not in set(args.skill):
            continue
        targets.append(d)

    results: list[dict[str, object]] = []
    for d in targets:
        results.append(
            _migrate_one(d, dry=args.check, refresh_shim=args.refresh_shim)
        )

    longest = max((len(str(r.get("skill"))) for r in results), default=0)
    for r in results:
        sid = str(r.get("skill")).ljust(longest)
        status = str(r.get("status"))
        extras = []
        if r.get("public_names"):
            extras.append(f"exports={len(r['public_names'])}")  # type: ignore[arg-type]
        if r.get("reason"):
            extras.append(f"reason={r['reason']!r}")
        print(f"  {sid}  {status:<14} {' '.join(extras)}")

    migrated = sum(
        1 for r in results
        if str(r.get("status")) in {
            "migrated",
            "would-migrate",
            "refreshed-shim",
            "would-refresh-shim",
        }
    )
    skipped = sum(1 for r in results if r.get("status") == "skipped")
    print()
    print(f"summary: migrated={migrated}  skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
