"""Native skill index/view tools — playbook-style skill discovery.

In the new architecture (per
are no longer schema containers — they are *playbooks*:

* ``SKILL.md`` is markdown. Its frontmatter carries an
  ``id`` / ``description`` / ``triggers`` / ``tags`` block.
* The body is free-form prose: how to think, what to do, common
  pitfalls, scripts available.
* Optional ``scripts/`` subfolder holds executables / helpers the
  playbook references.

The model sees a *one-line index* up front and pulls the full
playbook only when ``skill_view`` is called. That mirrors how IDE
Skills and agent skills work, and keeps the system prompt
short.

Tools provided here:

* ``skill_index``    — list available skills (id, title, description,
                       triggers, tags). Read-only, concurrency-safe.
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
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ...core.sandbox import sandbox_exec
from ...security.runtime_env import build_process_env
from ..tool_errors import schema_validation_result
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


_FRONTMATTER_RE = re.compile(
    r"^\s*(?:<!--.*?-->\s*)*---\s*\n(?P<fm>.*?)\n---\s*\n",
    re.DOTALL,
)


@dataclass
class SkillRecord:
    """Indexed view of a skill on disk."""

    skill_id: str
    title: str
    description: str
    triggers: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    path: str = ""
    body_chars: int = 0
    has_scripts: bool = False
    scripts: list[str] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "title": self.title,
            "description": self.description,
            "triggers": list(self.triggers),
            "tags": list(self.tags),
            "permissions": list(self.permissions),
            "path": self.path,
            "body_chars": self.body_chars,
            "has_scripts": self.has_scripts,
            "scripts": list(self.scripts),
        }


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    body = text[m.end():]
    fm_text = m.group("fm")
    # Lazy-import yaml; keep this module light.
    try:
        import yaml  # type: ignore[import-untyped]
        doc = yaml.safe_load(fm_text) or {}
        if not isinstance(doc, dict):
            doc = {}
    except Exception:
        doc = {}
    return doc, body


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

    When ``skill_files`` is provided it is the source of truth; production
    passes the active :class:`SkillRegistry` paths so enabled/integration
    filtering and nested namespaces cannot drift from this index. Root
    scanning remains as a small compatibility path for standalone callers.
    """

    found: list[SkillRecord] = []
    seen_ids: set[str] = set()
    files = (
        [Path(path) for path in skill_files]
        if skill_files is not None
        else [
            child / "SKILL.md"
            for root in roots
            if root.exists() and root.is_dir()
            for child in sorted(root.iterdir())
            if child.is_dir()
        ]
    )
    for md in files:
        if not md.is_file():
            continue
        child = md.parent
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = _parse_frontmatter(text)
        sid = str(fm.get("id") or fm.get("name") or child.name).strip()
        if not sid or sid in seen_ids:
            continue
        seen_ids.add(sid)
        scripts = _list_scripts(child)
        found.append(
            SkillRecord(
                skill_id=sid,
                title=str(fm.get("title") or fm.get("name") or sid),
                description=str(fm.get("description") or "").strip(),
                triggers=list(fm.get("triggers") or fm.get("when_to_use") or []),
                tags=list(fm.get("tags") or []),
                permissions=list(fm.get("permissions") or []),
                path=str(md),
                body_chars=len(body),
                has_scripts=bool(scripts),
                scripts=scripts,
            )
        )
    found.sort(key=lambda r: r.skill_id)
    return found


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
    ) -> None:
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
        self._records = index_skills(
            self._roots,
            skill_files=self._skill_files,
        )
        self._by_id = {r.skill_id: r for r in self._records}
        self._loaded_at = time.time()

    def records(self, *, refresh: bool = False) -> list[SkillRecord]:
        if refresh or not self._records:
            self.reload()
        return list(self._records)

    def get(self, skill_id: str, *, refresh: bool = False) -> Optional[SkillRecord]:
        if refresh or not self._by_id:
            self.reload()
        return self._by_id.get(skill_id)

    def render_for_prompt(self, *, max_chars: int = 4000) -> str:
        out: list[str] = [
            "## Skills available",
            "Call skill_view(<id>) to load a skill's playbook; this also "
            "unlocks that skill's specialized tools for the rest of the "
            "session (only a small core toolset is shown up front).",
        ]
        used = sum(len(s) + 1 for s in out)
        for r in self.records():
            head = f"- **{r.skill_id}** — {r.title}"
            if r.description:
                desc = r.description.replace("\n", " ").strip()[:240]
                head += f": {desc}"
            if used + len(head) + 1 > max_chars:
                break
            out.append(head)
            used += len(head) + 1
        return "\n".join(out)


# ---------------------------------------------------------------------------
# skill_index
# ---------------------------------------------------------------------------


def skill_index_handler(call: ToolCall, *, skill_index: SkillIndex) -> ToolResult:
    args = call.arguments or {}
    refresh = bool(args.get("refresh") or False)
    tag_filter = args.get("tag")
    rows = skill_index.records(refresh=refresh)
    if isinstance(tag_filter, str) and tag_filter.strip():
        wanted = tag_filter.strip().lower()
        rows = [r for r in rows if wanted in (t.lower() for t in r.tags)]
    text_lines = [f"Discovered {len(rows)} skill(s)."]
    for r in rows:
        line = f"- {r.skill_id}: {r.title}"
        if r.description:
            line += f" — {r.description[:160]}"
        text_lines.append(line)
    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.text_part("\n".join(text_lines)),
            ToolResultPart.json_part({"skills": [r.asdict() for r in rows]}),
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
    record = skill_index.get(sid)
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
        candidate = (base / rel).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
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
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.IO_ERROR,
                message=f"failed to read skill file: {exc}",
            ),
        )
    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.text_part(text),
            ToolResultPart.json_part(
                {"skill": record.asdict(), "path": str(target)}
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
    base = Path(rec.path).parent / "scripts"
    candidate = (base / name).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
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
    return sid == "browser" and bool(name)


_SAFE_SCRIPT_PERMISSIONS = frozenset({"read", "network", "rss", "web", "http"})


def is_low_risk_builtin_skill_script_run(
    payload: dict[str, Any],
    *,
    skill_index: SkillIndex,
    trusted_roots: Iterable[Path],
) -> bool:
    """Return true for explicitly low-risk scripts from trusted built-in skills."""

    sid = str(payload.get("skill_id") or payload.get("id") or "").strip()
    name = str(payload.get("name") or payload.get("script") or "").strip()
    if not sid or not name:
        return False
    rec = skill_index.get(sid)
    if rec is None or name not in set(rec.scripts):
        return False
    permissions = {
        str(item).strip().lower()
        for item in rec.permissions
        if str(item).strip()
    }
    if not permissions or not permissions <= _SAFE_SCRIPT_PERMISSIONS:
        return False
    script_path = _script_path(skill_index, sid, name)
    if script_path is None:
        return False
    resolved_script = script_path.resolve()
    for root in trusted_roots:
        try:
            resolved_script.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


def script_inspect_handler(call: ToolCall, *, skill_index: SkillIndex) -> ToolResult:
    args = call.arguments or {}
    sid = str(args.get("skill_id") or "").strip()
    name = str(args.get("name") or args.get("script") or "").strip()
    if not sid or not name:
        return schema_validation_result(
            call, "script_inspect requires 'skill_id' and 'name'",
        )
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
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.IO_ERROR,
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
                    "skill_id": sid,
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
    timeout = float(args.get("timeout_sec") or args.get("timeout") or timeout_default)
    if p.suffix.lower() == ".py":
        cmd = [sys.executable, str(p), *[str(a) for a in argv_extra]]
    elif p.suffix.lower() in {".sh", ".bash"}:
        cmd = ["bash", str(p), *[str(a) for a in argv_extra]]
    else:
        cmd = [str(p), *[str(a) for a in argv_extra]]
    started = time.time()
    root = cwd or p.parent
    try:
        env = build_process_env(None, root)
    except Exception:
        env = None
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
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.TIMEOUT,
                message=f"script timed out after {timeout:.1f}s",
                detail={"stdout": (exc.stdout or "")[-2000:], "stderr": (exc.stderr or "")[-2000:]},
            ),
        )
    except FileNotFoundError as exc:
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
    "is_low_risk_builtin_skill_script_run",
    "script_inspect_handler",
    "script_run_handler",
    "skill_index_handler",
    "skill_view_handler",
]
