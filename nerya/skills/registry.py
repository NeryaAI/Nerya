"""Registry holds loaded manifests + (optional) action handlers.

Aligned with the Anthropic Skill spec (agent skill runtime): a skill
is a markdown playbook with minimal YAML frontmatter, plus standalone
scripts the agent invokes via ``run_shell``. The registry only loads
the manifest; it does **not** auto-import any Python from the skill
directory. ``actions == {}`` for every skill loaded this way and the
runtime never dispatches them — the agent reads the markdown and
decides what to do.

Procedural single-file ``SKILL.md`` skills (one ``run`` action) are
still supported via :mod:`nerya.skills.procedural` for ergonomic user
playbooks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core import yaml_io
from ..core.errors import SkillNotFoundError
from .manifest import ActionSpec, SkillManifest


_LEGACY_ENABLED_ALIASES: dict[str, tuple[str, ...]] = {
    "market_data": ("markets", "market_data_routing"),
    "portfolio": ("trading",),
    "risk": ("trading",),
    "trigger": ("triggers",),
    "script": ("coding",),
    "message": ("notify",),
    "strategy_review": ("strategy_author",),
    "evolution": ("evolve",),
    "onchain": ("markets",),
    "subagent": ("team",),
    "exchange": ("markets",),
    "sdk_writer": ("strategy_author", "coding"),
    "strategy": ("strategy_author",),
    "wallet": ("markets",),
    "exchange_author": ("coding",),
    "capability_developer": ("coding", "evolve"),
    "trace": ("analysis",),
    "operator": ("coding",),
    "strategy_validation": ("backtest", "quant_research"),
    "workspace": ("analysis",),
    "data_science": ("analysis", "quant_research"),
    "devops": ("coding",),
}


def _expand_legacy_enabled_ids(enabled: set[str]) -> set[str]:
    expanded = set(enabled)
    for legacy_id, playbook_ids in _LEGACY_ENABLED_ALIASES.items():
        if legacy_id in enabled:
            expanded.update(playbook_ids)
    return expanded


@dataclass
class SkillEntry:
    manifest: SkillManifest
    module: Any
    actions: dict[str, Callable] = field(default_factory=dict)

    def action(self, name: str) -> Callable:
        if name not in self.actions:
            raise SkillNotFoundError(f"action {self.manifest.id}.{name} not implemented")
        spec = self.manifest.actions.get(name)
        if spec is not None and spec.status == "proposal_only_unimplemented":
            raise SkillNotFoundError(
                f"action {self.manifest.id}.{name} is proposal_only_unimplemented; "
                "operator must implement and clear the status flag"
            )
        if self.manifest.status == "proposal_only_unimplemented":
            raise SkillNotFoundError(
                f"skill {self.manifest.id} is proposal_only_unimplemented; "
                "operator must implement and clear the status flag"
            )
        return self.actions[name]

    def spec(self, name: str) -> ActionSpec:
        if name not in self.manifest.actions:
            raise SkillNotFoundError(f"action spec {self.manifest.id}.{name} missing")
        return self.manifest.actions[name]


class SkillRegistry:
    def __init__(self) -> None:
        self.by_id: dict[str, SkillEntry] = {}

    def register(self, entry: SkillEntry) -> None:
        self.by_id[entry.manifest.id] = entry

    def get(self, skill_id: str) -> SkillEntry:
        if skill_id not in self.by_id:
            raise SkillNotFoundError(skill_id)
        return self.by_id[skill_id]

    def list(self) -> list[SkillEntry]:
        return list(self.by_id.values())

    # --- loading ---
    @classmethod
    def load_builtin(cls, workspace_paths=None, *, config=None) -> "SkillRegistry":
        """Load every shipped + user-installed skill into a fresh registry.

        Order of discovery:

        1. ``nerya/skills/builtin/**/SKILL.md`` — the Anthropic-spec
           builtins shipped with the runtime, including grouped
           namespaces such as ``builtin/finance/<vertical>/<skill>/``.
        2. ``workspace/skills/installed/<id>/SKILL.md`` — user-installed
           skills (managed by :mod:`nerya.skills.installer`).
        3. ``workspace/skills/<id>/SKILL.md`` and ``~/.nerya/skills/<id>/
           SKILL.md`` — out-of-tree user skills.
        4. Top-level ``*.md`` under any of the user roots — single-file
           procedural skills (one ``run`` action synthesised by
           :mod:`nerya.skills.procedural`).

        No skill directory is auto-imported: every skill carries a
        markdown playbook the agent reads, and any executable scripts
        are invoked through ``run_shell``. ``actions == {}`` for every
        such skill; only procedural single-file skills register a
        ``run`` handler.

        A skill whose frontmatter declares ``requires_integration:
        <name>`` is skipped entirely unless
        ``config.integration_enabled(<name>)`` is true. This keeps
        optional third-party surfaces
        invisible to the agent when the operator has not opted in.
        When ``config`` is ``None`` the guard defaults to "no
        integration enabled" — safe for any caller that boots the
        registry before config has been loaded.
        """

        reg = cls()
        skills_root = Path(__file__).parent
        builtin_root = skills_root / "builtin"
        enabled: set[str] | None = None
        if workspace_paths is not None:
            doc = yaml_io.load(workspace_paths.skills_enabled, default={}) or {}
            if doc.get("enabled"):
                enabled = _expand_legacy_enabled_ids(set(doc["enabled"]))

        def _integration_ok(manifest: SkillManifest) -> bool:
            req = (getattr(manifest, "requires_integration", "") or "").strip()
            if not req:
                return True
            if config is None:
                return False
            try:
                return bool(config.integration_enabled(req))
            except Exception:
                return False

        # 1. Shipped Anthropic-spec builtins.
        if builtin_root.exists():
            for _d, md in _walk_skill_dirs(builtin_root):
                try:
                    manifest = SkillManifest.from_skill_md(md)
                except Exception:
                    continue
                manifest.source = "builtin"
                if enabled is not None and manifest.id not in enabled:
                    continue
                if not _integration_ok(manifest):
                    continue
                reg.register(SkillEntry(
                    manifest=manifest, module=None, actions={},
                ))

        if workspace_paths is None:
            return reg

        # 2. User-installed skills under workspace/skills/installed/.
        installed_root = workspace_paths.skills_installed
        if installed_root.exists():
            for d in sorted(installed_root.iterdir()):
                if not d.is_dir():
                    continue
                md = d / "SKILL.md"
                if md.exists():
                    try:
                        manifest = SkillManifest.from_skill_md(md)
                    except Exception:
                        # Fall through to a procedural single-file load
                        # below if the structured manifest fails.
                        manifest = None
                    if manifest is not None:
                        manifest.source = "workspace_installed"
                        if enabled is None or manifest.id in enabled:
                            if _integration_ok(manifest):
                                reg.register(SkillEntry(
                                    manifest=manifest, module=None, actions={},
                                ))
                                continue
                    _register_procedural(reg, md, enabled=enabled, source="workspace_installed")

        # 3 + 4. Additional user roots (workspace/skills/ + ~/.nerya/skills/).
        for extra_root in _user_skill_roots(workspace_paths):
            if not extra_root.exists():
                continue
            # Top-level SKILL.md files = procedural single-file skills.
            for md in sorted(extra_root.glob("*.md")):
                if md.name.lower() == "skill.md":
                    _register_procedural(
                        reg,
                        md,
                        enabled=enabled,
                        source=(
                            "workspace" if extra_root == workspace_paths.skills else "user_home"
                        ),
                    )
            # Each subfolder may carry SKILL.md at *any* nesting depth, so
            # operators can group skills in namespaces such as
            # ``workspace/skills/finance/private_equity/ic_memo/SKILL.md``.
            # ``_walk_skill_dirs`` yields one ``(skill_dir, SKILL.md)`` per
            # discovered skill and never descends into a skill's own asset
            # subtree (``references/``, ``scripts/``, ``tests/``, …).
            for _d, md in _walk_skill_dirs(extra_root):
                try:
                    manifest = SkillManifest.from_skill_md(md)
                except Exception:
                    manifest = None
                if manifest is not None:
                    manifest.source = (
                        "workspace" if extra_root == workspace_paths.skills else "user_home"
                    )
                    if (
                        manifest.source == "user_home"
                        and manifest.id in reg.by_id
                        and reg.by_id[manifest.id].manifest.source == "workspace"
                    ):
                        continue
                    if enabled is None or manifest.id in enabled:
                        if _integration_ok(manifest):
                            reg.register(SkillEntry(
                                manifest=manifest, module=None, actions={},
                            ))
                            continue
                _register_procedural(
                    reg,
                    md,
                    enabled=enabled,
                    source=(
                        "workspace" if extra_root == workspace_paths.skills else "user_home"
                    ),
                )
        return reg


def list_bundled_skill_names() -> list[str]:
    """Return the allow-listed built-in skill names shipped with Nerya.

    Nerya's historical directory name is ``skills/builtin``. The
    AgentArchitecturePatterns vocabulary calls these bundled skills; this
    function is the compatibility allowlist surface for that concept.
    """

    bundled_root = Path(__file__).parent / "builtin"
    if not bundled_root.exists():
        return []
    names: list[str] = []
    for _d, md in _walk_skill_dirs(bundled_root):
        try:
            names.append(SkillManifest.from_skill_md(md).id)
        except Exception:
            continue
    return sorted(set(names))


def _user_skill_roots(workspace_paths) -> list[Path]:
    """Return additional procedural-skill roots ordered by precedence.

    Order: workspace top-level (overrides home), then ``~/.nerya/skills/``.
    The returned paths are never resolved against the workspace ``installed``
    subdirectory (those are handled separately).
    """

    import os
    out: list[Path] = []
    out.append(workspace_paths.skills)  # workspace/skills/
    home = os.environ.get("NERYA_USER_SKILLS_ROOT")
    if home:
        out.append(Path(home).expanduser())
    else:
        out.append(Path.home() / ".nerya" / "skills")
    return out


# Directories that, when found *inside* a skill folder, are the skill's own
# asset subtree and must NOT be re-scanned for nested skills. Keeping the
# walker conservative here is what stops a SKILL.md under
# ``references/``/``scripts/`` from being mis-registered as its own skill.
_SKILL_ASSET_DIRS: frozenset[str] = frozenset(
    {"references", "scripts", "tests", "templates", "__pycache__"}
)


def _walk_skill_dirs(root: Path):
    """Yield ``(skill_dir, SKILL.md path)`` for every skill below *root*.

    The walker treats *any* directory whose direct child is ``SKILL.md``
    as a skill and stops descending into it — that subtree is the
    skill's own assets (``scripts/``, ``references/``, …). When a
    directory has no ``SKILL.md`` of its own and is not an asset
    directory, the walker recurses into it so namespaces like
    ``finance/private_equity/ic_memo/SKILL.md`` are picked up.

    Hidden directories (``.something``), the ``installed`` subtree
    (handled separately by :meth:`SkillRegistry.load_builtin`), and
    ``__pycache__`` are always skipped.
    """

    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name == "installed":
            continue
        if entry.name in _SKILL_ASSET_DIRS:
            continue
        md = entry / "SKILL.md"
        if md.exists():
            yield entry, md
            continue
        # No SKILL.md here — keep recursing.
        yield from _walk_skill_dirs(entry)


def _register_procedural(
    reg: "SkillRegistry",
    md_path: Path,
    *,
    enabled: set[str] | None,
    source: str = "procedural",
) -> None:
    """Load a procedural ``SKILL.md`` and register it with a synthetic ``run`` handler."""

    from .procedural import load_procedural_skill, make_run_handler

    skill = load_procedural_skill(md_path)
    if skill is None:
        return
    if enabled is not None and skill.manifest.id not in enabled:
        return
    skill.manifest.source = source
    if (
        source == "user_home"
        and skill.manifest.id in reg.by_id
        and reg.by_id[skill.manifest.id].manifest.source == "workspace"
    ):
        return
    handler = make_run_handler(
        skill.body,
        skill.manifest.id,
        skill.manifest.title,
        list(skill.manifest.tags or []),
    )
    reg.register(SkillEntry(
        manifest=skill.manifest,
        module=None,
        actions={"run": handler},
    ))
