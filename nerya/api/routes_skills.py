from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ..core import yaml_io
from ..core.time import now_iso
from ..skills.installer import install_skill, list_installed, promote_installed
from ..skills.lock_signing import (
    LockSignatureError,
    fingerprint_lock,
    load_signature,
    remove_signature,
    resolve_signing_key,
    sign_lock,
    verify_lock_signature,
)
from ..skills.lockfile import load_lock, record_lock_entry, verify_lock
from ..skills.manifest import SkillManifest


def _install(client, payload):
    payload = payload or {}
    source = payload.get("source")
    if not source:
        return {"error": "source is required"}
    report = install_skill(
        client.config.paths,
        source=source,
        kind=payload.get("kind", "auto"),
        subdir=payload.get("subdir"),
        git_ref=payload.get("git_ref"),
    )
    return report.asdict()


def _promote(client, payload):
    payload = payload or {}
    skill_id = payload.get("skill_id")
    if not skill_id:
        return {"error": "skill_id is required"}
    dst = promote_installed(client.config.paths, skill_id)
    return {"ok": True, "skill_id": skill_id, "installed_at": str(dst)}


def _installed(client, _p):
    return {"installed": list_installed(client.config.paths)}


def _slugify_skill_name(name: str) -> str:
    text = name.strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "_", text)
    text = text.strip("_-.")
    return text or name.strip()


def _render_skill_md(name: str, description: str, body: str) -> str:
    frontmatter = yaml_io.dumps({
        "name": name,
        "description": description,
        "version": "0.1.0",
    })
    body = body.strip()
    if not body:
        body = (
            f"# {name}\n\n"
            "## When to Use\n\n"
            "Describe the trigger conditions for this skill.\n\n"
            "## Workflow\n\n"
            "1. Inspect the relevant context.\n"
            "2. Follow the documented steps.\n"
            "3. Return a concise result with evidence.\n"
        )
    elif not body.lstrip().startswith("#"):
        body = f"# {name}\n\n{body}"
    return f"---\n{frontmatter}---\n{body.rstrip()}\n"


def _parse_skill_md(content: str) -> SkillManifest:
    with tempfile.TemporaryDirectory(prefix="nerya_skill_create_") as tmp:
        md = Path(tmp) / "SKILL.md"
        md.write_text(content, encoding="utf-8")
        return SkillManifest.from_skill_md(md)


def _create(client, payload):
    payload = payload or {}
    raw_name = str(payload.get("name") or "").strip()
    if not raw_name:
        return {"ok": False, "error": "name is required"}
    if len(raw_name) > 120:
        return {"ok": False, "error": "name is too long"}

    raw_content = payload.get("skill_md")
    if isinstance(raw_content, str) and raw_content.strip():
        content = raw_content
    else:
        content = _render_skill_md(
            raw_name,
            str(payload.get("description") or "").strip(),
            str(payload.get("body") or "").strip(),
        )
    if len(content.encode("utf-8")) > 512_000:
        return {"ok": False, "error": "skill_md too large"}

    try:
        parsed = _parse_skill_md(content)
    except Exception as exc:
        return {"ok": False, "error": "invalid_skill_md", "detail": str(exc)}

    expected_id = _slugify_skill_name(raw_name)
    if parsed.id != expected_id:
        return {
            "ok": False,
            "error": "skill_id_mismatch",
            "detail": f"frontmatter name resolves to {parsed.id!r}, expected {expected_id!r}",
        }

    if parsed.id in {"installed", "pending", "rejected", "enabled", "trust"}:
        return {"ok": False, "error": "reserved_skill_id", "skill_id": parsed.id}

    paths = client.config.paths
    target = (paths.skills / parsed.id).resolve()
    if not _is_relative_to(target, paths.skills.resolve()):
        return {"ok": False, "error": "unsafe_skill_path"}

    existing_entry = _get_entry(client, parsed.id)
    existing_path = existing_entry.manifest.path.resolve() if existing_entry and existing_entry.manifest.path else None
    if existing_entry is not None and existing_path != target:
        return {
            "ok": False,
            "error": "skill_already_loaded",
            "detail": f"{parsed.id!r} is already loaded from {existing_path or 'runtime'}",
        }

    overwrite = bool(payload.get("overwrite"))
    if target.exists() and not overwrite:
        return {"ok": False, "error": "skill_exists", "skill_id": parsed.id}
    if target.exists() and not target.is_dir():
        return {"ok": False, "error": "skill_path_not_directory", "path": str(target)}

    target.mkdir(parents=True, exist_ok=True)
    md_path = target / "SKILL.md"
    tmp = target / ".SKILL.md.tmp"
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, md_path)

    reloaded = client.skills.reload()
    out = _detail_payload(client, parsed.id)
    out.update({"created_at": now_iso(), "reloaded": reloaded})
    return out


def _lock_status(client, _payload):
    """supply-chain trust polish: lock + signature snapshot."""
    paths = client.config.paths
    fp = fingerprint_lock(paths)
    drift = verify_lock(paths)
    envelope = load_signature(paths)
    sig = envelope.to_dict() if envelope else None
    return {
        "ok": True,
        "lock": {
            "exists": paths.skills_lock.exists(),
            "entries": fp["entries"],
            "digest": fp["digest"],
            "byte_count": fp["byte_count"],
        },
        "drift": drift.asdict(),
        "signature": sig,
    }


def _lock_sign(client, payload):
    paths = client.config.paths
    explicit = payload.get("key")
    key_id = str(payload.get("key_id") or "operator-default")
    algo = str(payload.get("algorithm") or "hmac-sha256")
    explicit_bytes = explicit.encode("utf-8") if isinstance(explicit, str) and explicit else None
    key = resolve_signing_key(explicit=explicit_bytes, key_id=key_id, algorithm=algo)
    if key is None:
        return {"ok": False, "error": "no_signing_key",
                "detail": "set NERYA_LOCK_SIGNING_KEY env or pass 'key'"}
    try:
        envelope = sign_lock(paths, key=key, extra=payload.get("extra") or {})
    except LockSignatureError as exc:
        return {"ok": False, "error": "sign_failed", "detail": str(exc)}
    return {"ok": True, "envelope": envelope.to_dict()}


def _lock_verify(client, payload):
    paths = client.config.paths
    explicit = payload.get("key")
    explicit_bytes = explicit.encode("utf-8") if isinstance(explicit, str) and explicit else None
    envelope = load_signature(paths)
    algo = envelope.algorithm if envelope else "hmac-sha256"
    key_id = envelope.key_id if envelope else "operator-default"
    key = resolve_signing_key(explicit=explicit_bytes, key_id=key_id, algorithm=algo)
    report = verify_lock_signature(paths, key=key)
    out = report.to_dict()
    out["ok"] = report.ok
    return out


def _lock_clear_signature(client, _payload):
    removed = remove_signature(client.config.paths)
    return {"ok": True, "removed": removed}


def _lock_inspect(client, _payload):
    paths = client.config.paths
    return {"ok": True, "entries": [
        {
            "skill_id": sid,
            "version": e.version,
            "sha256": e.sha256,
            "publisher": e.publisher,
            "source_kind": e.source_kind,
            "installed_at": e.installed_at,
            "signed": bool(e.signature),
        }
        for sid, e in sorted(load_lock(paths).items())
    ]}


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _source_info(client, skill_path: Path | None) -> dict[str, Any]:
    paths = client.config.paths
    if skill_path is None:
        return {
            "source": "unknown",
            "path": "",
            "editable": False,
            "editable_reason": "missing skill path",
        }

    root = skill_path.resolve()
    workspace_skills = paths.skills.resolve()
    installed = paths.skills_installed.resolve()
    pending = paths.skills_pending.resolve()
    rejected = paths.skills_rejected.resolve()
    builtin_root = (Path(__file__).resolve().parents[1] / "skills" / "builtin").resolve()

    if _is_relative_to(root, installed):
        return {
            "source": "workspace_installed",
            "path": str(root),
            "relative_path": _rel(root, paths.root),
            "editable": True,
            "editable_reason": "workspace-installed SKILL.md",
        }
    if _is_relative_to(root, workspace_skills):
        blocked = _is_relative_to(root, pending) or _is_relative_to(root, rejected)
        blocked = blocked or root.name.startswith("_")
        return {
            "source": "workspace",
            "path": str(root),
            "relative_path": _rel(root, paths.root),
            "editable": not blocked,
            "editable_reason": (
                "workspace SKILL.md" if not blocked else
                "pending/rejected/internal workspace skill"
            ),
        }
    if _is_relative_to(root, builtin_root):
        return {
            "source": "builtin",
            "path": str(root),
            "relative_path": _rel(root, Path(__file__).resolve().parents[1]),
            "editable": False,
            "editable_reason": "built-in skills are edited in the repo, not the workspace",
        }
    return {
        "source": "external",
        "path": str(root),
        "relative_path": str(root),
        "editable": False,
        "editable_reason": "external skill roots are read-only from this workspace",
    }


def _file_row(path: Path, root: Path, *, kind: str) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": _rel(path, root),
        "kind": kind,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def _list_skill_files(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    skill_md = root / "SKILL.md"
    if skill_md.exists():
        rows.append(_file_row(skill_md, root, kind="playbook"))

    for child in sorted(root.iterdir()):
        if child.name in {"SKILL.md", "install_report.json"}:
            continue
        if child.is_file():
            rows.append(_file_row(child, root, kind="file"))

    for dirname, kind in (
        ("scripts", "script"),
        ("references", "reference"),
        ("templates", "template"),
    ):
        base = root / dirname
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            rows.append(_file_row(path, root, kind=kind))
    return rows[:300]


def _get_entry(client, skill_id: str):
    try:
        return client.skills.registry.get(skill_id)
    except Exception:
        return None


def _detail_payload(client, skill_id: str) -> dict[str, Any]:
    entry = _get_entry(client, skill_id)
    if entry is None:
        return {"ok": False, "error": "skill_not_found", "skill_id": skill_id}

    manifest = entry.manifest
    root = manifest.path
    info = _source_info(client, root)
    raw = ""
    if root is not None:
        md = root / "SKILL.md"
        if md.exists():
            raw = md.read_text(encoding="utf-8", errors="replace")

    detail = client.skills.view(skill_id) or {}
    detail.update({
        "description": manifest.description,
        "instructions": manifest.instructions or "",
        "source": info["source"],
        "path": info["path"],
        "relative_path": info.get("relative_path", ""),
        "editable": bool(info["editable"]),
        "editable_reason": info["editable_reason"],
        "files": _list_skill_files(root) if root is not None else [],
        "skill_md": raw,
    })
    return {"ok": True, "skill": detail}


def _detail(client, payload):
    skill_id = str((payload or {}).get("skill_id") or (payload or {}).get("id") or "").strip()
    if not skill_id:
        return {"ok": False, "error": "skill_id is required"}
    return _detail_payload(client, skill_id)


def _update(client, payload):
    payload = payload or {}
    skill_id = str(payload.get("skill_id") or "").strip()
    content = payload.get("skill_md")
    if not skill_id:
        return {"ok": False, "error": "skill_id is required"}
    if not isinstance(content, str):
        return {"ok": False, "error": "skill_md is required"}
    if len(content.encode("utf-8")) > 512_000:
        return {"ok": False, "error": "skill_md too large"}

    entry = _get_entry(client, skill_id)
    if entry is None or entry.manifest.path is None:
        return {"ok": False, "error": "skill_not_found", "skill_id": skill_id}

    info = _source_info(client, entry.manifest.path)
    if not info.get("editable"):
        return {
            "ok": False,
            "error": "skill_not_editable",
            "detail": info.get("editable_reason", ""),
        }

    md_path = entry.manifest.path / "SKILL.md"
    try:
        tmp_parse = md_path.parent / ".SKILL.md.validate.tmp"
        tmp_parse.write_text(content, encoding="utf-8")
        try:
            parsed = SkillManifest.from_skill_md(tmp_parse)
        finally:
            tmp_parse.unlink(missing_ok=True)
    except Exception as exc:
        return {"ok": False, "error": "invalid_skill_md", "detail": str(exc)}

    if parsed.id != skill_id:
        return {
            "ok": False,
            "error": "skill_id_mismatch",
            "detail": f"frontmatter name resolves to {parsed.id!r}, expected {skill_id!r}",
        }

    tmp = md_path.with_name(".SKILL.md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, md_path)

    if info.get("source") == "workspace_installed":
        existing = load_lock(client.config.paths).get(skill_id)
        record_lock_entry(
            client.config.paths,
            skill_id=skill_id,
            version=parsed.version,
            source_kind=(existing.source_kind if existing else "workspace_edit"),
            source=(existing.source if existing else str(md_path.parent)),
            publisher=(existing.publisher if existing else ""),
            signature="",
        )

    reloaded = client.skills.reload()
    out = _detail_payload(client, skill_id)
    out.update({"updated_at": now_iso(), "reloaded": reloaded})
    return out


def routes():
    return [
        ("GET", "/skills", lambda client, _p: {"skills": client.skills.list()}),
        ("GET", "/skills/detail", _detail),
        ("POST", "/skills/call",
         lambda client, payload: client.skill.call(
             payload["skill_id"], payload["action"],
             payload=payload.get("payload") or {},
             caller=payload.get("caller", "http"),
             strategy_id=payload.get("strategy_id"),
             session_id=payload.get("session_id"),
         )),
        ("POST", "/skills/update", _update),
        ("POST", "/skills/create", _create),
        ("POST", "/skills/install", _install),
        ("POST", "/skills/promote", _promote),
        ("GET", "/skills/installed", _installed),
        ("GET", "/skills/lock/status", _lock_status),
        ("POST", "/skills/lock/status", _lock_status),
        ("GET", "/skills/lock/inspect", _lock_inspect),
        ("POST", "/skills/lock/sign", _lock_sign),
        ("POST", "/skills/lock/verify", _lock_verify),
        ("POST", "/skills/lock/clear_signature", _lock_clear_signature),
    ]
