"""SkillKernel — top-level orchestrator: registry + runtime wiring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.config import Config
from .registry import SkillRegistry
from .runtime import SkillRuntime


@dataclass
class SkillKernel:
    config: Config
    registry: SkillRegistry
    runtime: SkillRuntime

    @classmethod
    def boot(cls, config: Config) -> "SkillKernel":
        registry = SkillRegistry.load_builtin(config.paths, config=config)
        runtime = SkillRuntime(config, registry)
        return cls(config=config, registry=registry, runtime=runtime)

    def call(self, skill_id: str, action: str, **kwargs) -> dict[str, Any]:
        return self.runtime.call(skill_id, action, **kwargs)

    def list(self) -> list[dict[str, Any]]:
        out = []
        for e in self.registry.list():
            manifest = e.manifest
            metadata = manifest.metadata or {}
            nerya_meta = metadata.get("nerya") if isinstance(metadata, dict) else None
            style = ""
            if isinstance(nerya_meta, dict):
                style = str(nerya_meta.get("style") or "")
            out.append({
                "id": manifest.id, "title": manifest.title,
                "description": manifest.description,
                "version": manifest.version,
                "status": manifest.status,
                "tags": list(manifest.tags or []),
                "style": style,
                "source": manifest.source,
                "path": str(manifest.path) if manifest.path else "",
                "has_playbook": bool(manifest.instructions),
                "metadata": metadata,
                "actions": [
                    {
                        "name": name,
                        "status": spec.status,
                        "tags": list(spec.tags or []),
                        "risk_gate": spec.risk_gate,
                        "approval_gate": spec.approval_gate,
                        "context_policy": spec.context_policy,
                    }
                    for name, spec in manifest.actions.items()
                ],
                "permissions": manifest.permissions,
            })
        return out

    # ------------------------------------------------------------------
    # compatibility helpers
    # ------------------------------------------------------------------

    def view(self, skill_id: str) -> dict[str, Any] | None:
        """Detailed skill view: manifest + agent-action map + path."""

        for entry in self.registry.list():
            manifest = entry.manifest
            if manifest.id != skill_id:
                continue
            actions: list[dict[str, Any]] = []
            for name, spec in manifest.actions.items():
                actions.append({
                    "name": name,
                    "title": spec.title,
                    "description": spec.description,
                    "risk_gate": spec.risk_gate,
                    "approval_gate": spec.approval_gate,
                    "permissions": list(spec.permissions or []),
                    "input_schema": spec.input_schema,
                    "output_schema": spec.output_schema,
                    "tags": list(spec.tags or []),
                    "status": spec.status,
                })
            metadata = manifest.metadata or {}
            nerya_meta = metadata.get("nerya") if isinstance(metadata, dict) else None
            style = ""
            if isinstance(nerya_meta, dict):
                style = str(nerya_meta.get("style") or "")
            return {
                "id": manifest.id,
                "title": manifest.title,
                "version": manifest.version,
                "description": manifest.description,
                "source": manifest.source,
                "permissions": list(manifest.permissions or []),
                "actions": actions,
                "tags": list(manifest.tags or []),
                "status": manifest.status,
                "path": str(manifest.path) if manifest.path else "",
                "style": style,
                "metadata": metadata,
                "instructions": manifest.instructions or "",
            }
        return None

    def doctor(self) -> dict[str, Any]:
        """Self-check the registry: surface proposal-only skills, missing
        Python handlers, and unenabled installed skills.

        Used by ``nerya skill doctor`` and the dashboard skill manager.
        """

        from ..core import yaml_io

        problems: list[dict[str, Any]] = []
        ok: list[str] = []
        for entry in self.registry.list():
            manifest = entry.manifest
            issues: list[str] = []
            if manifest.is_proposal_only():
                issues.append("manifest is proposal_only (no implementation yet)")
            for name, _spec in manifest.actions.items():
                if name not in (entry.actions or {}):
                    issues.append(f"action '{name}' has no Python handler")
            if issues:
                problems.append({
                    "id": manifest.id,
                    "issues": issues,
                    "status": manifest.status,
                })
            else:
                ok.append(manifest.id)

        # also surface installed skills that exist on disk but are
        # filtered out by the enabled.yml allow-list, so operators can
        # see why a skill they expect is not active.
        enabled_doc = yaml_io.load(self.config.paths.skills_enabled, default={}) or {}
        enabled = set(enabled_doc.get("enabled") or [])
        skipped: list[str] = []
        installed_root = self.config.paths.skills_installed
        if installed_root.exists():
            for d in installed_root.iterdir():
                if not d.is_dir():
                    continue
                md = d / "SKILL.md"
                if not md.exists():
                    continue
                doc: dict = {}
                try:
                    text = md.read_text(encoding="utf-8")
                    import re as _re
                    m = _re.match(
                        r"^---\s*\n(?P<fm>.*?)\n---\s*\n",
                        text,
                        _re.DOTALL,
                    )
                    if m:
                        doc = yaml_io.loads(m.group("fm")) or {}
                except Exception:
                    doc = {}
                if not doc:
                    continue
                sid = str(doc.get("id") or doc.get("name") or d.name)
                if enabled and sid not in enabled:
                    skipped.append(sid)

        return {
            "ok": ok,
            "problems": problems,
            "skipped_disabled": sorted(skipped),
            "registered": [e.manifest.id for e in self.registry.list()],
        }

    def reload(self) -> int:
        """Re-read every skill manifest + actions module from disk.

        Used by ``nerya skill sync`` after an install/promote so the
        live process picks up new manifests without a restart.
        """

        new = SkillRegistry.load_builtin(self.config.paths, config=self.config)
        self.registry = new
        self.runtime.registry = new
        return len(new.list())
