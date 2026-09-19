"""Shared operator/agent Skill management, without importing Skill code.

Builtins are immutable package assets: editing one stages a Workspace override.
Agent scope is the actual role's allowed_skills, not an unused parallel store.
All mutations are content-addressed proposals; the operator reviews/applies them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Literal

from ..core import yaml_io
from ..evolution.patch_proposal import create_proposal
from ..subagents.registry import describe_role
from .manifest import SkillManifest

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


def _walk(root: Path):
    if not root.is_dir() or root.is_symlink():
        return
    for current, directories, files in os.walk(root, followlinks=False):
        base = Path(current)
        directories[:] = sorted(d for d in directories if not d.startswith(".") and d not in ASSETS | EXCLUDE
                                and not (base / d).is_symlink())
        if "SKILL.md" in files and not (base / "SKILL.md").is_symlink():
            yield base, base / "SKILL.md"


def _definitions(config):
    from .registry import _user_skill_roots
    roots = [(Path(__file__).parent / "builtin", "builtin")]
    user = _user_skill_roots(config.paths)
    # Match the runtime's workspace-over-home precedence.
    roots += [(p, "user_home") for p in user[1:]]
    roots += [(config.paths.skills_installed, "workspace_installed"), (config.paths.skills, "workspace")]
    found = []
    for root, source in roots:
        for directory, md in _walk(root):
            try:
                _text(md)
                manifest = SkillManifest.from_skill_md(md)
                found.append({"id": manifest.id, "description": manifest.description,
                              "source": source, "root": directory, "entry": "SKILL.md"})
            except (ValueError, OSError):
                continue
        # Existing standalone procedural markdown skills remain readable too.
        if source != "builtin" and root.is_dir() and not root.is_symlink():
            for md in sorted(root.glob("*.md")):
                if md.name == "SKILL.md" or md.is_symlink():
                    continue
                from .procedural import load_procedural_skill
                skill = load_procedural_skill(md)
                if skill:
                    found.append({"id": skill.manifest.id, "description": skill.manifest.description,
                                  "source": source, "root": root, "entry": md.name})
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
    return any(skill_id == name or skill_id.startswith(name + ".") for name in names)


def _enabled(config, ids):
    path = _safe(config.paths.root, "skills/enabled.yml")
    doc = yaml_io.load(path, default={}) or {}
    names = doc.get("enabled")
    return set(ids) if names is None else {sid for sid in ids if _matches(sid, names)}


def _binding_revision(config, role):
    return hashlib.sha256(json.dumps({"skills": role["allowed_skills"],
        "prompt": _sha(_safe(config.paths.root, f"subagents/{role['name']}.agent.md")),
        "meta": _sha(_safe(config.paths.root, f"subagents/{role['name']}.role.yaml"))}, sort_keys=True).encode()).hexdigest()


def catalog(config, scope: Scope = "all", agent_id: str = "", query: str = "",
            offset: int = 0, limit: int = 100, include_unassigned: bool = False):
    if scope not in {"all", "builtin", "workspace", "agent"} or offset < 0 or not 1 <= limit <= 200:
        raise ValueError("Invalid Skill scope or pagination")
    definitions = _definitions(config)
    effective = _effective(definitions)
    available = _enabled(config, effective)
    role = _role(config, agent_id) if scope == "agent" else None
    candidates = definitions if scope == "builtin" else list(effective.values())
    rows = []
    for row in candidates:
        assigned = _matches(row["id"], role["allowed_skills"]) if role else None
        if scope == "builtin" and row["source"] != "builtin":
            continue
        if scope == "workspace" and row["source"] not in {"workspace", "workspace_installed"}:
            continue
        if role and not assigned and not include_unassigned:
            continue
        if query and query.lower() not in (row["id"] + " " + row["description"]).lower():
            continue
        rows.append({"id": row["id"], "description": row["description"], "source": row["source"],
                     "enabled": row["id"] in available, "assigned": assigned,
                     "revision": _sha(_safe(row["root"], row["entry"])), "entry": row["entry"],
                     "edit_effect": "workspace_override" if row["source"] in {"builtin", "user_home"} else "workspace_update"})
    rows.sort(key=lambda row: row["id"])
    return {"ok": True, "skills": rows[offset:offset + limit], "total": len(rows),
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "scope": scope, "agent_id": agent_id,
            "binding_revision": _binding_revision(config, role) if role else "",
            "enabled_revision": _sha(_safe(config.paths.root, "skills/enabled.yml"))}


def _resolve(config, skill_id, scope, agent_id=""):
    definitions = _definitions(config)
    candidates = [row for row in definitions if row["id"] == skill_id and
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
    return {"ok": True, "id": skill_id, "source": row["source"], "file": file,
            "revision": _sha(path), "text": text[offset:offset + limit], "offset": offset,
            "next_offset": offset + limit if offset + limit < len(text) else None,
            "total_chars": len(text), "files": _files(row), "scope": scope,
            "shared_definition": scope == "agent"}


def manage(config, action: Action, skill_id: str, scope: Scope = "workspace", agent_id: str = "",
           content: str = "", file: str = "SKILL.md", revision: str = "", summary: str = ""):
    if action not in {"create", "update", "delete", "enable", "disable"} or scope not in {"all", "builtin", "workspace", "agent"}:
        raise ValueError("Invalid Skill operation")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", skill_id):
        raise ValueError("Invalid Skill identifier")
    if len(content.encode()) > MAX_FILE or "\0" in content:
        raise ValueError("Skill content must be UTF-8 text up to 512 KiB")
    extra, target, deleted = {}, "", []
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
            # The existing candidate/approval pipeline consumes metadata.deleted_files.
        else:
            if file == row["entry"]:
                # Validate the actual SKILL.md contract without executing any script.
                if not content.startswith("---\n") or "\n---" not in content[4:]:
                    raise ValueError("SKILL.md requires YAML frontmatter with name and description")
                front = yaml_io.loads(content.split("---", 2)[1])
                if not isinstance(front, dict) or front.get("name") != skill_id or not str(front.get("description", "")).strip():
                    raise ValueError("Skill frontmatter name must match skill_id and include description")
            if is_override:
                # A complete override retains all text references; never copy sibling sub-skills.
                for relative in _files(row):
                    extra["after/" + (dest / relative).relative_to(config.paths.root).as_posix()] = _text(_safe(row["root"], relative))
            extra["after/" + target] = content
    if sum(len(v.encode()) for v in extra.values()) > 2_097_152:
        raise ValueError("Proposal exceeds 2 MiB; use a smaller Skill package")
    # Validation is deliberately static: it does not run proposed Skill scripts.
    # Existing approval still gates application of the validated candidate bundle.
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
              "warnings": ["Static validation only; proposed scripts have not been executed."]}
    extra["validation_report.json"] = json.dumps(report)
    proposal = create_proposal(config.paths, kind="skill_proposal", summary=summary or f"{action} Skill {skill_id}",
        initial_state="pending_review", target=target, extra_files=extra,
        rationale=f"Scope: {scope}; Agent: {agent_id or 'workspace'}. No live change has been applied.",
        metadata={"skill_id": skill_id, "scope": scope, "agent_id": agent_id, "source_revision": revision, "deleted_files": deleted},
        evidence_refs=[f"workspace:{target}#sha256={revision}"],
        test_plan="Review SKILL.md and script changes; validate the staged candidate before approval.",
        rollback="Use the proposal's existing rollback snapshot after an approved apply.")
    return {"ok": True, "proposal": proposal.asdict(), "applied": False,
            "next_action": "Review and validate the proposal in Action Inbox before applying"}
