"""Shared operator/agent Skill management, without importing Skill code.

Builtins are immutable package assets: editing one writes a Workspace override.
Agent scope is the actual role's allowed_skills, not an unused parallel store.
Validated mutations are applied immediately so the operator has one save path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Literal

from ..core import yaml_io
from ..subagents.registry import describe_role
from .lockfile import remove_lock_entry
from .manifest import _slugify
from .registry import SkillRegistry, _enabled_ok, enabled_skill_names, catalog_generation
from .discovery import catalog_group, catalog_parent, catalog_roots, rank_skill_metadata

Scope = Literal["all", "builtin", "workspace", "agent"]
Action = Literal["create", "update", "delete", "enable", "disable"]
MAX_FILE = 524288
ASSETS = {"scripts", "references", "templates", "tests"}
EXCLUDE = {".git", "__pycache__", "node_modules", "pending", "installed"}


def _safe(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if not relative or rel.is_absolute() or "\\" in relative or any(p in {"..", "."} or p.startswith(".") for p in rel.parts):
        raise ValueError("Invalid Skill-relative path")
    if root.is_symlink():
        raise ValueError("Symlink Skill roots are not exposed")
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Symlink Skill paths are not exposed")
    return path


def _text(path: Path) -> str:
    if not path.is_file() or path.stat().st_size > MAX_FILE:
        raise ValueError("Skill file is missing or exceeds 512 KiB")
    data = path.read_bytes()
    if b"\0" in data or len(data) > MAX_FILE:
        raise ValueError("Only bounded UTF-8 Skill text files are exposed")
    return data.decode("utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"


def _definitions(config):
    registry = SkillRegistry.load_builtin(config.paths, config=config, include_disabled=True)
    found = []
    for entry in registry.definitions:
        manifest = entry.manifest
        try:
            _text(_safe(manifest.path, manifest.entry_file))
        except (ValueError, OSError, UnicodeError):
            continue
        found.append({"id": manifest.id, "title": manifest.title,
                      "description": manifest.description, "source": manifest.source,
                      "root": manifest.path, "entry": manifest.entry_file,
                      "revision": manifest.revision, "registry_entry": entry,
                      "catalog_parent": catalog_parent(manifest.metadata),
                      "catalog_group": catalog_group(manifest.metadata)})
    return found


def _effective(definitions):
    return {row["id"]: row for row in definitions}


def _role(config, agent_id):
    if not agent_id:
        raise ValueError("agent_id is required for Agent Skill scope")
    if not re.fullmatch(r"[A-Za-z0-9_]+", agent_id):
        raise ValueError("Invalid Agent identifier")
    for suffix in (".agent.md", ".role.yaml"):
        _safe(config.paths.root, f"subagents/{agent_id}{suffix}")
    role = describe_role(config.paths, agent_id)
    if not role:
        raise ValueError("Agent role not found")
    return role


def _matches(skill_id, names):
    return _enabled_ok(skill_id, set(names))


def _enabled(config, ids):
    _safe(config.paths.root, "skills/enabled.yml")
    names = enabled_skill_names(config.paths)
    return {sid for sid in ids if _enabled_ok(sid, names)}


def _binding_revision(config, role):
    return hashlib.sha256(json.dumps({"skills": role["allowed_skills"],
        "prompt": _sha(_safe(config.paths.root, f"subagents/{role['name']}.agent.md")),
        "meta": _sha(_safe(config.paths.root, f"subagents/{role['name']}.role.yaml"))}, sort_keys=True).encode()).hexdigest()


def catalog(config, scope: Scope = "all", agent_id: str = "", query: str = "",
            offset: int = 0, limit: int = 100, include_unassigned: bool = False,
            view: Literal["core", "professional", "all"] = "all", parent: str = "", hierarchical: bool = False):
    if scope not in {"all", "builtin", "workspace", "agent"} or offset < 0 or not 1 <= limit <= 200:
        raise ValueError("Invalid Skill scope or pagination")
    if view not in {"core", "professional", "all"}:
        raise ValueError("Invalid Skill view")
    if not isinstance(query, str) or len(query) > 512:
        raise ValueError("Skill search query must be at most 512 characters")
    query = query.strip()
    definitions = _definitions(config)
    effective = _effective(definitions)
    available = _enabled(config, effective)
    role = _role(config, agent_id) if scope == "agent" else None
    candidates = definitions if scope == "builtin" else list(effective.values())
    rows = []
    scoped = {}
    for row in candidates:
        assigned = _matches(row["id"], role["allowed_skills"]) if role else None
        if scope == "builtin" and row["source"] != "builtin":
            continue
        if scope == "workspace" and row["source"] not in {"workspace", "workspace_installed"}:
            continue
        if role and not assigned and not include_unassigned:
            continue
        scoped[row["id"]] = row
        rows.append({"id": row["id"], "title": row["title"], "description": row["description"], "source": row["source"],
                     "enabled": row["id"] in available, "assigned": assigned,
                     "effective_source": effective[row["id"]]["source"],
                     "shadowed": [{"source": other["source"], "path": str(other["root"] / other["entry"])}
                                  for other in definitions if other["id"] == row["id"]
                                  and other is not row and effective[row["id"]] is row],
                     "revision": _sha(_safe(row["root"], row["entry"])), "entry": row["entry"],
                     "edit_effect": "workspace_override" if row["source"] in {"builtin", "user_home"} else "workspace_update"})
    # Group after scope/assignment filtering, before search and pagination. A
    # leaf-only role must never disappear or acquire its parent's permissions.
    roots = catalog_roots((sid, row["root"], row.get("catalog_parent", "")) for sid, row in scoped.items())
    for row in rows:
        root = roots[row["id"]]
        row["catalog_parent"] = root if root != row["id"] else ""
        row["catalog_group"] = scoped[root].get("catalog_group", "core")
        row["method_count"] = sum(sid != root and owner == root for sid, owner in roots.items()) if row["id"] == root else 0
    scores = {item["skill_id"]: item for item in rank_skill_metadata(query, ((row["id"], row["description"]) for row in rows))} if query else {}
    order = {}
    if hierarchical and not parent:
        matching = {row["catalog_parent"] or row["id"] for row in rows if not query or row["id"] in scores}
        for row in rows:
            if row["id"] in scores:
                owner = row["catalog_parent"] or row["id"]
                order[owner] = max(order.get(owner, 0), scores[row["id"]]["score"])
        rows = [row for row in rows if not row["catalog_parent"] and row["id"] in matching
                and (view == "all" or row["catalog_group"] == view)]
        query = ""
    if parent:
        rows = [row for row in rows if row["catalog_parent"] == parent]
    elif not query and view != "all":
        rows = [row for row in rows if not row["catalog_parent"] and row["catalog_group"] == view]
    if query:
        rows = [row for row in rows if row["id"] in scores]
        order = {key: value["score"] for key, value in scores.items()}
    rows.sort(key=lambda row: (-order.get(row["id"], 0), row["id"]))
    return {"ok": True, "skills": rows[offset:offset + limit], "total": len(rows),
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "scope": scope, "agent_id": agent_id,
            "role_access_mode": ("allowlist" if role["allowed_skills"] else "legacy_unrestricted") if role else None,
            "catalog_generation": catalog_generation(effective[sid]["registry_entry"] for sid in available),
            "binding_revision": _binding_revision(config, role) if role else "",
            "enabled_revision": _sha(_safe(config.paths.root, "skills/enabled.yml"))}


def _resolve(config, skill_id, scope, agent_id=""):
    definitions = _definitions(config)
    candidates = [row for row in definitions if _slugify(row["id"]) == _slugify(skill_id) and
                  (scope != "builtin" or row["source"] == "builtin")]
    if not candidates:
        raise ValueError("Skill not found; discover the catalog first")
    row = candidates[-1]
    if scope == "workspace" and row["source"] not in {"workspace", "workspace_installed"}:
        raise ValueError("No Workspace Skill with that identifier")
    if scope == "agent" and not _matches(skill_id, _role(config, agent_id)["allowed_skills"]):
        raise ValueError("Skill is not assigned to this Agent")
    return row


def _files(row):
    root = row["root"]
    paths = [row["entry"]]
    if row["entry"] == "SKILL.md":
        for name in ASSETS:
            folder = _safe(root, name)
            if folder.is_dir():
                for base, directories, names in os.walk(folder, followlinks=False):
                    directories[:] = [d for d in directories if not d.startswith(".") and d not in EXCLUDE
                                      and not (Path(base) / d).is_symlink()]
                    for filename in names:
                        file = Path(base) / filename
                        if not file.is_symlink() and not filename.startswith(".") and file.suffix not in {".pyc", ".pyo"}:
                            paths.append(file.relative_to(root).as_posix())
    return sorted(set(paths))


def read(config, skill_id: str, scope: Scope = "all", agent_id: str = "", file: str = "SKILL.md",
         offset: int = 0, limit: int = 16000):
    if scope not in {"all", "builtin", "workspace", "agent"} or offset < 0 or not 1 <= limit <= 32000:
        raise ValueError("Invalid Skill scope or text pagination")
    row = _resolve(config, skill_id, scope, agent_id)
    if file == "SKILL.md":
        file = row["entry"]
    path = _safe(row["root"], file)
    if file != row["entry"] and PurePosixPath(file).parts[0] not in ASSETS:
        raise ValueError("Only the playbook and its references/templates/scripts/tests may be read")
    from ..mcp.catalog import public_result
    text = public_result(_text(path))
    return {"ok": True, "id": row["id"], "source": row["source"], "file": file,
            "revision": _sha(path), "text": text[offset:offset + limit], "offset": offset,
            "next_offset": offset + limit if offset + limit < len(text) else None,
            "total_chars": len(text), "files": _files(row), "scope": scope,
            "shared_definition": scope == "agent"}


def manage(config, action: Action, skill_id: str, scope: Scope = "workspace", agent_id: str = "",
           content: str = "", file: str = "SKILL.md", revision: str = "", summary: str = ""):
    if action not in {"create", "update", "delete", "enable", "disable"} or scope not in {"all", "builtin", "workspace", "agent"}:
        raise ValueError("Invalid Skill operation")
    if action != "create":
        skill_id = _resolve(config, skill_id, "all")["id"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", skill_id):
        raise ValueError("Invalid Skill identifier")
    if len(content.encode()) > MAX_FILE or "\0" in content:
        raise ValueError("Skill content must be UTF-8 text up to 512 KiB")
    extra, target, deleted = {}, "", []
    whole_skill_delete = False
    delete_root: Path | None = None
    delete_source = ""
    if action in {"enable", "disable"}:
        definitions = _effective(_definitions(config))
        if skill_id not in definitions:
            raise ValueError("Skill not found")
        if scope == "agent":
            role = _role(config, agent_id)
            if revision != _binding_revision(config, role):
                raise ValueError("stale_revision: refresh the Agent Skill catalog")
            names = set(role["allowed_skills"])
            # Expand namespace grants only when disabling one leaf, preserving its siblings.
            if action == "disable":
                parents = {n for n in names if skill_id == n or skill_id.startswith(n + ".")}
                names -= parents
                names |= {sid for sid in definitions if _matches(sid, parents) and sid != skill_id
                          and not sid.startswith(skill_id + ".") and not skill_id.startswith(sid + ".")}
            else:
                names.add(skill_id)
            target = f"subagents/{agent_id}.role.yaml"
            path = _safe(config.paths.root, target)
            meta = yaml_io.load(path, default={}) or {}
            meta.update(name=agent_id, tier=role["tier"], allowed_skills=sorted(names))
            extra["after/" + target] = yaml_io.dumps(meta)
            if not (config.paths.subagents / f"{agent_id}.agent.md").exists():
                extra[f"after/subagents/{agent_id}.agent.md"] = role["prompt"]
        else:
            target = "skills/enabled.yml"
            path = _safe(config.paths.root, target)
            if revision != _sha(path):
                raise ValueError("stale_revision: refresh the Skill catalog")
            names = _enabled(config, definitions)
            if action == "disable":
                names = {name for name in names if name != skill_id and not name.startswith(skill_id + ".")
                         and not skill_id.startswith(name + ".")}
            else:
                names.add(skill_id)
            doc = yaml_io.load(path, default={}) or {}
            doc["enabled"] = sorted(names)
            extra["after/" + target] = yaml_io.dumps(doc)
    else:
        if scope == "agent":
            raise ValueError("Agent scope manages assignments; edit the shared definition in Workspace scope")
        if action == "create":
            if skill_id in _effective(_definitions(config)) or revision != "missing":
                raise ValueError("Skill already exists; use update after reading it")
            row = {"root": config.paths.skills / skill_id, "entry": "SKILL.md", "source": "workspace"}
            if file != "SKILL.md":
                raise ValueError("Create the SKILL.md playbook first")
        else:
            row = _resolve(config, skill_id, scope)
            if file == "SKILL.md":
                file = row["entry"]
            path = _safe(row["root"], file)
            if file != row["entry"] and PurePosixPath(file).parts[0] not in ASSETS:
                raise ValueError("Invalid Skill asset path")
            if revision != _sha(path):
                raise ValueError("stale_revision: read the current Skill file first")
        is_override = row["source"] in {"builtin", "user_home"}
        if action == "delete" and is_override:
            raise ValueError("Shipped/global Skills cannot be deleted here; disable them or edit a Workspace override")
        dest = config.paths.skills / skill_id if is_override else row["root"]
        if is_override and (dest / "SKILL.md").exists():
            raise ValueError("A Workspace override already exists; read and edit Workspace scope instead")
        _safe(config.paths.root, dest.relative_to(config.paths.root).as_posix() + "/" + file)
        target = (dest / file).relative_to(config.paths.root).as_posix()
        if action == "delete":
            deleted = [target]
            if file == row["entry"]:
                deleted = [(dest / p).relative_to(config.paths.root).as_posix() for p in _files(row)]
                whole_skill_delete = True
                delete_root = dest
                delete_source = row["source"]
                enabled_path = _safe(config.paths.root, "skills/enabled.yml")
                enabled_doc = yaml_io.load(enabled_path, default={}) or {}
                if isinstance(enabled_doc, dict) and isinstance(enabled_doc.get("enabled"), list):
                    definitions = _effective(_definitions(config))
                    names = _enabled(config, definitions)
                    names = {
                        name for name in names
                        if name != skill_id
                        and not name.startswith(skill_id + ".")
                        and not skill_id.startswith(name + ".")
                    }
                    enabled_doc["enabled"] = sorted(names)
                    extra["after/skills/enabled.yml"] = yaml_io.dumps(enabled_doc)
        else:
            if file == row["entry"]:
                # Validate the actual SKILL.md contract without executing any script.
                frontmatter_text = content
                marker = "<!-- nerya-skill-frontmatter-start -->\n"
                if frontmatter_text.startswith(marker):
                    frontmatter_text = frontmatter_text[len(marker):]
                if not frontmatter_text.startswith("---\n") or "\n---" not in frontmatter_text[4:]:
                    raise ValueError("SKILL.md requires YAML frontmatter with name and description")
                front = yaml_io.loads(frontmatter_text.split("---", 2)[1])
                if not isinstance(front, dict) or _slugify(str(front.get("name") or "")) != _slugify(skill_id) or not str(front.get("description", "")).strip():
                    raise ValueError("Skill frontmatter name must match skill_id and include description")
            if is_override:
                # A complete override retains all text references; never copy sibling sub-skills.
                for relative in _files(row):
                    extra["after/" + (dest / relative).relative_to(config.paths.root).as_posix()] = _text(_safe(row["root"], relative))
            extra["after/" + target] = content
    if sum(len(v.encode()) for v in extra.values()) > 2_097_152:
        raise ValueError("Skill change exceeds 2 MiB; use a smaller Skill package")
    # Validate every write before touching disk. Saving a Skill must never execute
    # its scripts; runtime execution remains a separate explicit action.
    import ast
    checks = []
    for relative, value in extra.items():
        if relative.endswith(".py"):
            ast.parse(value, filename=relative)
            checks.append({"path": relative, "check": "python_syntax", "ok": True})
        elif relative.endswith((".yml", ".yaml")):
            yaml_io.loads(value)
            checks.append({"path": relative, "check": "safe_yaml", "ok": True})
        else:
            checks.append({"path": relative, "check": "bounded_utf8_and_scoped_path", "ok": True})
    checks += [{"path": relative, "check": "scoped_deletion", "ok": True} for relative in deleted]
    report = {"ok": True, "checks": checks, "blockers": [],
              "warnings": ["Static validation only; Skill scripts were not executed during save."]}

    for relative in deleted:
        _safe(config.paths.root, relative).unlink(missing_ok=True)
    if whole_skill_delete and delete_root is not None:
        if delete_source == "workspace_installed":
            remove_lock_entry(config.paths, skill_id)
        for child in sorted((p for p in delete_root.rglob("*") if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True):
            try:
                child.rmdir()
            except OSError:
                pass
        try:
            delete_root.rmdir()
        except OSError:
            pass
    for staged, value in extra.items():
        relative = staged.removeprefix("after/")
        path = _safe(config.paths.root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.unlink(missing_ok=True)
        tmp.write_text(value, encoding="utf-8")
        os.replace(tmp, path)

    return {"ok": True, "applied": True, "action": action, "skill_id": skill_id,
            "scope": scope, "agent_id": agent_id, "target": target,
            "deleted_files": deleted, "validation": report}
