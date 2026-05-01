"""Scaffold new script proposals under workspace/scripts/pending/<id>/."""

from __future__ import annotations

from pathlib import Path

from ..core import yaml_io
from ..core.atomic_write import atomic_write_text
from ..core.paths import WorkspacePaths


def scaffold(paths: WorkspacePaths, *, script_id: str, title: str,
             description: str = "", script_body: str | None = None) -> Path:
    target = paths.scripts_pending / script_id
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": script_id,
        "version": "0.1.0",
        "title": title,
        "description": description,
        "entry": "run",
        "state": "pending",
        "llm_policy": {
            "allowed_tiers": ["light"],
            "allowed_tasks": ["classify", "compress"],
            "max_calls_per_run": 5,
            "max_tokens_per_run": 4000,
            "max_cost_usd_per_day": 1,
            "high_tier_requires_approval": True,
        },
    }
    atomic_write_text(target / "manifest.yml", yaml_io.dumps(manifest))
    atomic_write_text(target / f"{script_id}.py", script_body or (
        "def run(**_):\n"
        "    return {'ok': True}\n"
    ))
    atomic_write_text(target / "README.md",
                      f"# {title}\n\n{description}\n")
    return target


def pending_path(paths: WorkspacePaths, script_id: str) -> Path:
    d = paths.scripts_pending / script_id
    return d / f"{script_id}.py"


def promote(paths: WorkspacePaths, script_id: str) -> Path:
    src = paths.scripts_pending / script_id
    if not src.exists():
        raise FileNotFoundError(str(src))
    dst = paths.scripts_approved / script_id
    dst.mkdir(parents=True, exist_ok=True)
    for p in src.iterdir():
        if p.is_file():
            (dst / p.name).write_bytes(p.read_bytes())
    manifest_path = dst / "manifest.yml"
    manifest = yaml_io.load(manifest_path, default={}) or {}
    manifest["state"] = "approved"
    yaml_io.dump(manifest_path, manifest)
    return dst
