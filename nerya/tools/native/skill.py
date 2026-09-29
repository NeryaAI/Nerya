"""Native skill index/view tools — playbook-style skill discovery.

In the new architecture (per
are no longer schema containers — they are *playbooks*:

* ``SKILL.md`` is markdown. Its standard ``name`` and ``description``
  frontmatter fields identify the skill and explain when to use it.
* The body is free-form prose: how to think, what to do, common
  pitfalls, scripts available.
* Optional ``scripts/`` subfolder holds executables / helpers the
  playbook references.

The model sees a *one-line index* up front and pulls the full
playbook only when ``skill_view`` is called. That mirrors how IDE
Skills and agent skills work, and keeps the system prompt
short.

Tools provided here:

* ``skill_index``    — list available skills (id, description). Read-only,
                       concurrency-safe.
* ``skill_view``     — fetch the body of one skill's playbook.
                       Read-only, concurrency-safe.
* ``script_inspect`` — read script metadata / first lines (for
                       playbooks that reference helper scripts).
                       Read-only.
* ``script_run``     — invoke a script under the skill's
                       ``scripts/`` directory. Risk = EXEC.
"""

from __future__ import annotations

import json
import hashlib
import base64
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from xml.sax.saxutils import escape as xml_escape

from ...core.sandbox import sandbox_exec
from ...security.runtime_env import build_process_env
from ...skills.discovery import catalog_ids, catalog_parent
from ...skills.registry import SkillRegistry, _enabled_ok, load_entry, discover_entries
from ...skills.manifest import _slugify
from ..tool_errors import schema_validation_result
from .paths import WorkspaceEscapeError, resolve_workspace_path
from ..types import (
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolResultPart,
)


# ---------------------------------------------------------------------------
# SKILL.md parser
# ---------------------------------------------------------------------------


@dataclass
class SkillRecord:
    """Indexed view of a skill on disk."""

    skill_id: str
    description: str
    path: str = ""
    body_chars: int = 0
    has_scripts: bool = False
    scripts: list[str] = field(default_factory=list)
    catalog_parent: str = ""
    title: str = ""
    source: str = ""
    revision: str = ""

    def asdict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "raw_name": self.title,
            "source": self.source,
            "revision": self.revision,
            "description": self.description,
            "path": self.path,
            "body_chars": self.body_chars,
            "has_scripts": self.has_scripts,
            "scripts": list(self.scripts),
        }


def _list_scripts(skill_dir: Path) -> list[str]:
    sd = skill_dir / "scripts"
    if not sd.is_dir():
        return []
    out: list[str] = []
    for p in sorted(sd.iterdir()):
        if p.is_file() and not p.name.startswith("__"):
            out.append(p.name)
    return out


def index_skills(
    roots: Iterable[Path],
    *,
    skill_files: Optional[Iterable[Path]] = None,
) -> list[SkillRecord]:
    """Walk ``roots`` and return one record per ``SKILL.md`` discovered.

    Explicit files and root scans remain for standalone callers. Production
    binds SkillIndex.registry_provider to the live SkillKernel so a reload
    replaces its directory snapshot instead of rescanning an old file list.
    """

    registry = SkillRegistry()
    entries = ([load_entry(Path(path)) for path in skill_files]
               if skill_files is not None
               else [entry for root in roots for entry in discover_entries(Path(root))])
    # Standalone callers keep their explicit root/file order (first root wins).
    for entry in entries:
        if entry is not None and entry.manifest.id not in registry.by_id:
            registry.register(entry)
    return _records_from_registry(registry)


def _records_from_registry(registry: SkillRegistry) -> list[SkillRecord]:
    found = []
    for entry in registry.list():
        manifest = entry.manifest
        if manifest.path is None:
            continue
        scripts = _list_scripts(manifest.path)
        found.append(SkillRecord(
            skill_id=manifest.id, title=manifest.title,
            description=manifest.description, source=manifest.source,
            revision=manifest.revision,
            path=str(manifest.path / manifest.entry_file),
            body_chars=len(manifest.instructions), has_scripts=bool(scripts), scripts=scripts,
            catalog_parent=catalog_parent(manifest.metadata),
        ))
    return sorted(found, key=lambda record: record.skill_id)


def allowed_skill_names(call: ToolCall):
    """Trusted runtime metadata only; missing/empty lists preserve legacy access.

    A non-empty list is a namespace allow-list, independent of tool permissions.
    Malformed metadata fails closed; model-supplied arguments cannot widen it.
    """
    value = (call.metadata or {}).get("allowed_skills")
    if value is None or value == [] or value == ():
        return None
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) for v in value):
        return {""}
    return set(value)


def skill_allowed(skill_id: str, allowed_skills) -> bool:
    return _enabled_ok(skill_id, set(allowed_skills) if allowed_skills else None)


def skill_access_error(call: ToolCall, skill_id: str) -> ToolResult | None:
    if skill_allowed(skill_id, allowed_skill_names(call)):
        return None
    return ToolResult.from_error(
        tool_use_id=call.id, name=call.name,
        error=ToolError(kind=ToolErrorKind.PERMISSION_DENIED,
                        message=f"Skill is outside this role's allowed_skills: {skill_id!r}"),
    )


def skill_asset_access_error(call: ToolCall, record: SkillRecord, target: Path,
                             skill_index: SkillIndex) -> ToolResult | None:
    """A hub path must not bypass a narrower role grant on a nested skill."""
    root = Path(record.path).parent.resolve()
    resolved = target.resolve()
    if not resolved.is_relative_to(root):
        return schema_validation_result(call, "Skill files must stay inside the skill directory.")
    if resolved == Path(record.path).resolve():
        return None
    # Legacy single-file skills share a root; a sibling playbook is still a
    # separate skill even though reading it would not escape that directory.
    if resolved.parent == root:
        for other in skill_index.records():
            if resolved.name == Path(other.path).name and resolved == Path(other.path).resolve():
                denied = skill_access_error(call, other.skill_id)
                if denied is not None:
                    return denied
    directory = resolved.parent
    while directory != root and directory.is_relative_to(root):
        entry = load_entry(directory / "SKILL.md")
        if entry is not None:
            denied = skill_access_error(call, entry.manifest.id)
            if denied is not None:
                return denied
            if skill_index.get(entry.manifest.id) is None:
                return ToolResult.from_error(
                    tool_use_id=call.id, name=call.name,
                    error=ToolError(kind=ToolErrorKind.NOT_FOUND,
                                    message="Nested Skill is not in the active catalog."),
                )
        directory = directory.parent
    return None


# ---------------------------------------------------------------------------
# SkillIndex (cached)
# ---------------------------------------------------------------------------


class SkillIndex:
    """Cached SKILL.md index used by ``skill_index`` / ``skill_view``."""

    def __init__(
        self,
        roots: Iterable[Path],
        *,
        skill_files: Optional[Iterable[Path]] = None,
        registry_provider: Callable[[], SkillRegistry] | None = None,
        refresh_registry: Callable[[], Any] | None = None,
    ) -> None:
        self._registry_provider = registry_provider
        self._refresh_registry = refresh_registry
        self.catalog_generation = ""
        self._roots = [Path(r) for r in roots]
        self._skill_files = (
            [Path(path) for path in skill_files]
            if skill_files is not None
            else None
        )
        self._records: list[SkillRecord] = []
        self._by_id: dict[str, SkillRecord] = {}
        self._loaded_at = 0.0

    def reload(self) -> None:
        if self._registry_provider is not None:
            if self._refresh_registry is not None:
                self._refresh_registry()
            registry = self._registry_provider()
            records = _records_from_registry(registry)
            generation = registry.catalog_generation
        else:
            records = index_skills(self._roots, skill_files=self._skill_files)
            generation = hashlib.sha256(json.dumps(
                [(r.skill_id, r.source, r.path, r.revision) for r in records],
            ).encode()).hexdigest()
        self._publish(records, generation)

    def _publish(self, records, generation) -> None:
        self._records = records
        self._by_id = {r.skill_id: r for r in records}
        self.catalog_generation = generation
        self._loaded_at = time.time()

    def _ensure_loaded(self, refresh: bool = False) -> None:
        if refresh or not self._loaded_at:
            self.reload()
        elif self._registry_provider is not None:
            registry = self._registry_provider()
            generation = registry.catalog_generation
            if generation != self.catalog_generation:
                self._publish(_records_from_registry(registry), generation)

    def records(self, *, refresh: bool = False, allowed_skills=None) -> list[SkillRecord]:
        self._ensure_loaded(refresh)
        return [r for r in self._records if skill_allowed(r.skill_id, allowed_skills)]

    def get(self, skill_id: str, *, refresh: bool = False) -> Optional[SkillRecord]:
        self._ensure_loaded(refresh)
        if skill_id in self._by_id:
            return self._by_id[skill_id]
        canonical = _slugify(skill_id)
        return next((r for r in self._records if _slugify(r.skill_id) == canonical), None)

    def catalog(self, *, refresh: bool = False, allowed_skills=None) -> list[SkillRecord]:
        """Filter role access before folding; a leaf grant never grants its hub."""
        rows = self.records(refresh=refresh, allowed_skills=allowed_skills)
        visible = catalog_ids(
            (r.skill_id, Path(r.path).parent, r.catalog_parent) for r in rows
        )
        return [r for r in rows if r.skill_id in visible]

    def render_for_prompt(self, *, max_chars: int | None = None, allowed_skills=None) -> str:
        """Render the standard progressive-disclosure skill catalog.

        Only metadata is injected. The markdown body is loaded through the
        Skill/skill_view tool when the model selects a skill.
        """
        entries: list[str] = []
        used = len("<available_skills>\n</available_skills>")
        for r in self.catalog(allowed_skills=allowed_skills):
            entry = (
                "  <skill>\n"
                f"    <name>{xml_escape(r.skill_id)}</name>\n"
                f"    <description>{xml_escape(r.description)}</description>\n"
                f"    <location>{xml_escape(r.path)}</location>\n"
                "  </skill>"
            )
            if max_chars is not None and used + len(entry) + 1 > max_chars:
                break
            entries.append(entry)
            used += len(entry) + 1
        return "<available_skills>\n" + "\n".join(entries) + "\n</available_skills>"


# ---------------------------------------------------------------------------
# skill_index
# ---------------------------------------------------------------------------


def skill_index_handler(call: ToolCall, *, skill_index: SkillIndex) -> ToolResult:
    args = call.arguments or {}
    refresh = bool(args.get("refresh") or False)
    rows = skill_index.catalog(refresh=refresh, allowed_skills=allowed_skill_names(call))
    text_lines = [f"Discovered {len(rows)} skill(s)."]
    for r in rows:
        line = f"- {r.skill_id}"
        if r.description:
            line += f" — {r.description[:160]}"
        text_lines.append(line)
    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.text_part("\n".join(text_lines)),
            ToolResultPart.json_part({"skills": [r.asdict() for r in rows],
                                      "catalog_generation": skill_index.catalog_generation}),
        ],
    )


# ---------------------------------------------------------------------------
# skill_view
# ---------------------------------------------------------------------------


def skill_view_handler(call: ToolCall, *, skill_index: SkillIndex) -> ToolResult:
    args = call.arguments or {}
    sid = str(args.get("skill_id") or args.get("id") or "").strip()
    if not sid:
        return schema_validation_result(call, "skill_view requires 'skill_id'")
    denied = skill_access_error(call, sid)
    if denied is not None:
        return denied
    record = skill_index.get(sid, refresh=bool(args.get("refresh")))
    if record is None:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message=f"skill not found: {sid!r}",
            ),
        )
    # Optional asset read: builtin skill directories live inside the
    # installed package, outside the workspace sandbox, so read_file
    # cannot reach references/ playbooks. ``file`` reads an asset path
    # relative to (and confined to) the skill's own directory.
    target = Path(record.path)
    rel = str(args.get("file") or "").strip()
    if rel:
        base = Path(record.path).parent.resolve()
        try:
            candidate = resolve_workspace_path(rel, root=base)
        except WorkspaceEscapeError:
            return schema_validation_result(
                call, f"'file' must stay inside the skill directory: {rel!r}",
            )
        if not candidate.is_file():
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.NOT_FOUND,
                    message=f"skill asset not found: {sid}/{rel}",
                ),
            )
        target = candidate
    denied = skill_asset_access_error(call, record, target, skill_index)
    if denied is not None:
        return denied
    try:
        text = target.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"failed to read skill file: {exc}",
            ),
        )
    revision = hashlib.sha256(text.encode("utf-8")).hexdigest()
    page: dict[str, Any] = {}
    if "offset" in args or "limit" in args:
        offset, limit = args.get("offset", 0), args.get("limit", 200)
        if (type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 400):
            return schema_validation_result(call, "offset must be non-negative; limit must be 1..400.")
        lines = text.splitlines()
        text = "\n".join(lines[offset:offset + limit])
        end = min(offset + limit, len(lines))
        page = {"offset": offset, "total_lines": len(lines),
                "next_offset": end if end < len(lines) else None}
    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.text_part(text),
            ToolResultPart.json_part(
                {"skill": record.asdict(), "path": str(target), "revision": revision,
                 "catalog_generation": skill_index.catalog_generation,
                 "body_policy": "latest_on_read", **page}
            ),
        ],
    )


# ---------------------------------------------------------------------------
# script_inspect / script_run
# ---------------------------------------------------------------------------


def _script_path(skill_index: SkillIndex, skill_id: str, name: str) -> Optional[Path]:
    rec = skill_index.get(skill_id)
    if rec is None:
        return None
    root = Path(rec.path).parent.resolve()
    base = root / "scripts"
    try:
        relative = Path(name)
        if relative.parts and relative.parts[0] == "scripts":
            relative = Path(*relative.parts[1:])
        candidate = resolve_workspace_path(str(relative), root=base)
        candidate.relative_to(root)
    except (ValueError, OSError):
        return None
    if not candidate.is_file():
        return None
    return candidate


def is_browser_skill_script_run(payload: dict[str, Any]) -> bool:
    """Return true for scripts dispatched from the built-in browser skill.

    Browser operations are intentionally exposed through the browser
    skill scripts. They need to be low-friction for agent browser work,
    while other skill scripts remain under the normal EXEC approval
    policy.
    """

    sid = str(payload.get("skill_id") or payload.get("id") or "").strip().lower()
    name = str(payload.get("name") or payload.get("script") or "").strip()
    return sid == "browser" and name in {"browser_session.py", "scripts/browser_session.py"}


def script_inspect_handler(call: ToolCall, *, skill_index: SkillIndex) -> ToolResult:
    args = call.arguments or {}
    sid = str(args.get("skill_id") or "").strip()
    name = str(args.get("name") or args.get("script") or "").strip()
    if not sid or not name:
        return schema_validation_result(
            call, "script_inspect requires 'skill_id' and 'name'",
        )
    denied = skill_access_error(call, sid)
    if denied is not None:
        return denied
    if args.get("refresh"):
        skill_index.reload()
    p = _script_path(skill_index, sid, name)
    if p is None:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message=f"script not found: {sid}/{name}",
            ),
        )
    denied = skill_asset_access_error(call, skill_index.get(sid), p, skill_index)
    if denied is not None:
        return denied
    try:
        raw = p.read_bytes()
        text = raw.decode("utf-8", errors="replace")
    except OSError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"failed to read script: {exc}",
            ),
        )
    head = "\n".join(text.splitlines()[:80])
    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.text_part(head),
            ToolResultPart.json_part(
                {
                    "skill_id": skill_index.get(sid).skill_id,
                    "catalog_generation": skill_index.catalog_generation,
                    "revision": hashlib.sha256(raw).hexdigest(),
                    "body_policy": "latest_on_read",
                    "name": name,
                    "path": str(p),
                    "size": p.stat().st_size,
                    "lines_total": text.count("\n") + 1,
                    "lines_shown": min(80, text.count("\n") + 1),
                }
            ),
        ],
    )


def script_run_handler(
    call: ToolCall,
    *,
    skill_index: SkillIndex,
    cwd: Optional[Path] = None,
    timeout_default: float = 60.0,
    conversation_id: str = "",
) -> ToolResult:
    args = call.arguments or {}
    sid = str(args.get("skill_id") or "").strip()
    name = str(args.get("name") or args.get("script") or "").strip()
    argv_extra = args.get("args") or []
    if not sid or not name:
        return schema_validation_result(
            call, "script_run requires 'skill_id' and 'name'",
        )
    if not isinstance(argv_extra, list):
        return schema_validation_result(call, "'args' must be a list of strings")
    denied = skill_access_error(call, sid)
    if denied is not None:
        return denied
    if args.get("refresh"):
        skill_index.reload()
    p = _script_path(skill_index, sid, name)
    if p is None:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message=f"script not found: {sid}/{name}",
            ),
        )
    denied = skill_asset_access_error(call, skill_index.get(sid), p, skill_index)
    if denied is not None:
        return denied
    timeout = float(args.get("timeout_sec") or args.get("timeout") or timeout_default)
    if p.suffix.lower() == ".py":
        cmd = [sys.executable, str(p), *[str(a) for a in argv_extra]]
        # Bundled skills are Python packages (and may use relative imports).
        # Only the installed builtin tree uses -m; workspace overrides remain files.
        package = Path(__file__).resolve().parents[2]
        try:
            relative = p.resolve().relative_to(package / 'skills' / 'builtin').with_suffix('')
        except ValueError:
            relative = None
        if relative is not None and all(part.isidentifier() for part in relative.parts):
            cmd = [sys.executable, '-m', '.'.join(('nerya', 'skills', 'builtin', *relative.parts)), *[str(a) for a in argv_extra]]
    elif p.suffix.lower() in {".sh", ".bash"}:
        cmd = ["bash", str(p), *[str(a) for a in argv_extra]]
    elif p.suffix.lower() == ".ps1":
        import shutil
        interpreter = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        cmd = [interpreter, "-NoProfile", "-NonInteractive", "-File", str(p),
               *[str(a) for a in argv_extra]]
    elif p.suffix.lower() in {".js", ".mjs", ".cjs"}:
        cmd = ["node", str(p), *[str(a) for a in argv_extra]]
    else:
        cmd = [str(p), *[str(a) for a in argv_extra]]
    started = time.time()
    root = cwd or p.parent
    try:
        env = build_process_env(None, root)
    except Exception:
        env = None
    trace = None
    builtin_browser = Path(__file__).resolve().parents[2] / 'skills/builtin/browser/scripts/browser_session.py'
    if sid == 'browser' and p.resolve() == builtin_browser.resolve():
        import os
        from ...integrations import browser_trace
        trace = browser_trace.create(root, conversation_id, call.id)
        env = dict(env if env is not None else os.environ)
        env['NERYA_BROWSER_TRACE_TOKEN'] = trace.token
    try:
        proc = sandbox_exec(
            cmd,
            cwd=root,
            root=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        if trace:
            trace.finish('failed')
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.TIMEOUT,
                message=f"script timed out after {timeout:.1f}s",
                detail={"stdout": (exc.stdout or "")[-2000:], "stderr": (exc.stderr or "")[-2000:]},
            ),
        )
    except OSError as exc:
        if trace:
            trace.finish('failed')
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.IO_ERROR,
                message=f"failed to invoke script: {exc}",
            ),
        )
    duration = time.time() - started
    raw_stdout = proc.stdout or ""
    stdout = raw_stdout[-8000:]
    stderr = (proc.stderr or "")[-8000:]
    stdout_json: Any = None
    if raw_stdout.strip():
        try:
            stdout_json = json.loads(raw_stdout)
        except Exception:
            stdout_json = None
    success = proc.returncode == 0
    if trace:
        trace.finish('completed' if success else 'failed')
    builtin_browser = Path(__file__).resolve().parents[2] / 'skills/builtin/browser/scripts/browser_session.py'
    if sid == 'browser' and p.resolve() == builtin_browser.resolve() and isinstance(stdout_json, dict):
        observation = dict(stdout_json)
        image = observation.pop('image', None)
        parts = [ToolResultPart.json_part(observation)]
        if isinstance(image, str) and image.startswith('data:image/png;base64,') and len(image) <= 14 * 1024 * 1024:
            try:
                encoded = image.split(',', 1)[1]
                decoded = base64.b64decode(encoded, validate=True)
                if decoded.startswith(b'\x89PNG\r\n\x1a\n'):
                    parts.append(ToolResultPart(type='image', data={'type':'base64','media_type':'image/png','data':encoded}, media_type='image/png', metadata={'name':'browser-viewport','provider_input':True}))
            except (ValueError, TypeError):
                observation['image_error'] = 'invalid_browser_image'
        # Browser evidence is already structured and bounded. Do not duplicate
        # it (or a base64 image) inside shell stdout, the UI or model text.
        return ToolResult(tool_use_id=call.id, name=call.name,
                          is_error=not (success and observation.get('ok') is True),
                          semantic_success=success and observation.get('ok') is True,
                          content=parts, elapsed_ms=round(duration * 1000),
                          metadata={"external_content":True},
                          error=None if success and observation.get('ok') is True else ToolError(
                              kind=ToolErrorKind.EXECUTION_ERROR,
                              message=json.dumps(observation, ensure_ascii=False), retryable=False,
                              recovery_hint={"action":"inspect_state_before_retry"}))
    text_summary = (
        f"$ {' '.join(cmd[:3])}{' …' if len(cmd) > 3 else ''}\n"
        f"exit={proc.returncode}  duration={duration:.2f}s\n"
        f"---- stdout ----\n{stdout}\n---- stderr ----\n{stderr}"
    )
    json_payload = {
        "skill_id": sid,
        "name": name,
        "exit_code": proc.returncode,
        "duration_sec": round(duration, 3),
        "stdout": stdout,
        "stderr": stderr,
    }
    if stdout_json is not None:
        json_payload["stdout_json"] = stdout_json
    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        is_error=not success,
        content=[
            ToolResultPart.text_part(text_summary),
            ToolResultPart.json_part(json_payload),
        ],
    )


__all__ = [
    "SkillIndex",
    "SkillRecord",
    "index_skills",
    "is_browser_skill_script_run",
    "script_inspect_handler",
    "script_run_handler",
    "skill_index_handler",
    "skill_view_handler",
]
