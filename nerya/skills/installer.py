"""External skill installer.

Skills can be shipped outside the ``nerya/skills/builtin/`` tree. This
module turns three kinds of sources into a validated layout under
``workspace/skills/installed/<skill_id>/`` that the registry can then
load alongside the built-ins:

* **local directory** — a folder containing ``SKILL.md`` (Anthropic-
  spec frontmatter + markdown playbook) plus any standalone scripts
  the agent should invoke via ``run_shell``.
* **tarball** — a ``.tar.gz`` archive with the same layout.
* **git repository** — a remote git URL (optionally with a
  ``subdir=foo/bar`` hint) from which exactly one skill directory
  is extracted.

Install is intentionally not a silent network + exec:

1. We fetch into a quarantine directory.
2. We validate the manifest with :class:`SkillManifest`.
3. We reject legacy definition surfaces such as ``actions.py`` and
   ``skill.yml``. Executable helpers belong under reviewed ``scripts/``.
4. We write an ``install_report.json`` describing what was installed.
5. The skill is staged under ``workspace/skills/pending/<id>/`` and a
   ``skill_install_request`` proposal is emitted. An operator approves
   via the normal proposal pipeline; promotion moves it to
   ``workspace/skills/installed/<id>/``.

This means external skills use exactly the same approval-gated,
journal-backed path as any other mutation — the installer cannot
silently arm live code.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..core import jsonl
from ..core.errors import SkillActionError
from ..core.paths import WorkspacePaths
from ..core.sandbox import sandbox_exec
from ..core.time import now_iso
from ..evolution.patch_proposal import create_proposal
from .manifest import SkillManifest


_LEGACY_DEFINITION_SURFACES = {
    "actions.py",
    "skill.yml",
    "skill.yaml",
    "manifest.yml",
    "manifest.yaml",
}
_SKILL_SCAN_MAX_FILE_BYTES = 1_000_000
_BLOCKED_BINARY_EXTENSIONS = {
    ".class",
    ".dll",
    ".dylib",
    ".exe",
    ".jar",
    ".msi",
    ".node",
    ".pyd",
    ".so",
    ".wasm",
}
_SCRIPT_SCAN_EXTENSIONS = {".bat", ".cmd", ".cjs", ".js", ".mjs", ".ps1", ".py", ".sh", ".ts"}
_DANGEROUS_SCRIPT_MARKERS = {
    "base64 -d": "shell-decoder",
    "chmod -r": "recursive-permission-change",
    "curl ": "network-shell",
    "eval(": "dynamic-eval",
    "exec(": "dynamic-exec",
    "invoke-webrequest": "powershell-network",
    "os.system(": "shell-exec",
    "powershell": "powershell-exec",
    "rm -rf": "destructive-shell",
    "socket.": "raw-network",
    "subprocess.": "subprocess-exec",
}


@dataclass
class StaticFinding:
    severity: str
    rule_id: str
    path: str
    message: str


@dataclass
class InstallReport:
    skill_id: str
    version: str
    source_kind: str          # "dir" | "tar" | "git"
    source: str
    files: list[str] = field(default_factory=list)
    sha256: str = ""
    static_findings: list[dict[str, Any]] = field(default_factory=list)
    proposal_id: str | None = None
    staged_at: Path | None = None

    def asdict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "staged_at": None if self.staged_at is None else str(self.staged_at),
        }


def install_skill(
    paths: WorkspacePaths,
    *,
    source: str,
    kind: str = "auto",
    subdir: str | None = None,
    git_ref: str | None = None,
) -> InstallReport:
    """Install a skill from an external source.

    :param source: filesystem path (for ``dir`` / ``tar``) or git URL
        (for ``git``).
    :param kind: one of ``dir``, ``tar``, ``git``, or ``auto`` (default).
        ``auto`` inspects ``source`` and picks the right handler.
    :param subdir: for ``tar`` / ``git``, the skill-containing
        subdirectory inside the extracted tree. Required if the archive
        contains more than one skill.
    :param git_ref: optional git ref (branch, tag, or sha).
    :raises SkillActionError: on any validation failure — nothing is
        written to ``skills/pending`` when this is raised.
    """
    source, subdir, git_ref = _normalize_source_hints(
        source,
        subdir=subdir,
        git_ref=git_ref,
    )
    resolved_kind = _resolve_kind(source, kind)
    with tempfile.TemporaryDirectory(prefix="nerya_skill_") as qdir_str:
        qdir = Path(qdir_str)
        skill_dir = _fetch(resolved_kind, source, qdir,
                           subdir=subdir, git_ref=git_ref)
        manifest = _load_manifest(skill_dir)
        _reject_legacy_definition_surfaces(skill_dir)
        findings = _static_analyze(skill_dir)
        blocking = [f for f in findings if f.severity == "critical"]
        if blocking:
            preview = ", ".join(f"{f.path}:{f.rule_id}" for f in blocking[:3])
            raise SkillActionError(f"skill static analysis failed: {preview}")

        staged = paths.skills_pending / manifest.id
        if staged.exists():
            shutil.rmtree(staged)
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_dir, staged)

        files = sorted(p.relative_to(staged).as_posix()
                       for p in staged.rglob("*") if p.is_file())
        digest = _hash_dir(staged)

        report = InstallReport(
            skill_id=manifest.id,
            version=getattr(manifest, "version", "") or "",
            source_kind=resolved_kind,
            source=source,
            files=files,
            sha256=digest,
            static_findings=[asdict(f) for f in findings],
            staged_at=staged,
        )

        prop = create_proposal(
            paths,
            kind="skill_install_request",
            summary=f"Install external skill {manifest.id}",
            rationale=_render_rationale(report),
            extra_files={
                "install_report.json": json.dumps(report.asdict(), indent=2),
                "target.yml":
                    f"target: skills/installed/{manifest.id}\n",
            },
        )
        report.proposal_id = prop.id
        (staged / "install_report.json").write_text(
            json.dumps(report.asdict(), indent=2), encoding="utf-8"
        )
        jsonl.append(paths.journal("evolution"), {
            "kind": "skill_install_request",
            "skill_id": manifest.id,
            "source_kind": resolved_kind,
            "source": source,
            "staged": str(staged),
            "proposal_id": prop.id,
            "sha256": digest,
            "findings": len(findings),
            "ts": now_iso(),
        })
        return report


def promote_installed(paths: WorkspacePaths, skill_id: str) -> Path:
    """Move a pending skill into the ``installed`` tree and flip it on
    in ``skills/enabled.yml`` so the registry will load it on the next
    boot.

    This is what the proposal-promotion step calls once an operator
    approves a ``skill_install_request``. After promotion we also
    record an entry in ``skills/skills.lock.yml`` so
    the kernel can detect tree drift on the next boot.
    """
    from ..core import yaml_io
    pending = paths.skills_pending / skill_id
    if not pending.exists():
        raise SkillActionError(f"no pending skill at {pending}")
    target = paths.skills_installed / skill_id
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(pending), str(target))

    # enable it so the next registry load will pick it up
    doc = yaml_io.load(paths.skills_enabled, default={}) or {}
    enabled = list(doc.get("enabled") or [])
    if skill_id not in enabled:
        enabled.append(skill_id)
        doc["enabled"] = enabled
        yaml_io.dump(paths.skills_enabled, doc)

    # refresh the lock entry. Source/version come from
    # the staged report; sha256 is recomputed from the promoted tree so
    # boot-time integrity checks compare apples to apples (the staged
    # hash excludes ``install_report.json`` which only exists post-stage).
    try:
        from .lockfile import record_lock_entry
        report_path = target / "install_report.json"
        version = ""
        source_kind = ""
        source = ""
        if report_path.exists():
            data = json.loads(report_path.read_text(encoding="utf-8"))
            version = str(data.get("version") or "")
            source_kind = str(data.get("source_kind") or "")
            source = str(data.get("source") or "")
        record_lock_entry(
            paths,
            skill_id=skill_id,
            version=version,
            sha256="",
            source_kind=source_kind,
            source=source,
        )
    except Exception:
        # Lock recording is best-effort; never block promotion.
        pass

    return target


def list_installed(paths: WorkspacePaths) -> list[dict[str, Any]]:
    root = paths.skills_installed
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        report = d / "install_report.json"
        if report.exists():
            try:
                data = json.loads(report.read_text(encoding="utf-8"))
            except Exception:
                data = {"skill_id": d.name, "error": "report_unreadable"}
        else:
            data = {"skill_id": d.name}
        out.append(data)
    return out


# ---------------------------------------------------------------- internals

def _resolve_kind(source: str, kind: str) -> str:
    if kind != "auto":
        return kind
    if source.endswith(".tar.gz") or source.endswith(".tgz"):
        return "tar"
    if source.startswith(("http://", "https://", "git@", "git+", "ssh://")):
        return "git"
    p = Path(source)
    if p.exists() and p.is_dir():
        return "dir"
    raise SkillActionError(
        f"cannot auto-detect install kind for {source!r}; "
        "pass kind=dir|tar|git explicitly"
    )


def _normalize_source_hints(
    source: str,
    *,
    subdir: str | None,
    git_ref: str | None,
) -> tuple[str, str | None, str | None]:
    """Turn common paste-friendly URLs into installer primitives.

    Operators often paste a GitHub folder URL such as
    ``https://github.com/org/repo/tree/main/skills/foo``. ``git clone``
    cannot consume that URL directly, so we split it into the clone URL,
    branch/ref, and repository subdirectory. Explicit UI fields still win.
    """
    clean_source = str(source or "").strip()
    clean_subdir = _clean_optional(subdir)
    clean_ref = _clean_optional(git_ref)
    parsed = _parse_github_tree_url(clean_source)
    if not parsed:
        return clean_source, clean_subdir, clean_ref
    return (
        parsed["source"],
        clean_subdir or parsed.get("subdir") or None,
        clean_ref or parsed.get("git_ref") or None,
    )


def _clean_optional(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _parse_github_tree_url(source: str) -> dict[str, str] | None:
    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None

    parts = [unquote(p) for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    repo = repo[:-4] if repo.endswith(".git") else repo
    clone_url = f"https://github.com/{owner}/{repo}.git"
    if len(parts) < 4 or parts[2] not in {"tree", "blob"}:
        return {"source": clone_url}

    tail = parts[3:]
    if not tail:
        return {"source": clone_url}

    ref, subdir_parts = _split_github_ref_and_path(tail)
    if parts[2] == "blob" and subdir_parts and subdir_parts[-1].lower() == "skill.md":
        subdir_parts = subdir_parts[:-1]
    out = {"source": clone_url, "git_ref": ref}
    if subdir_parts:
        out["subdir"] = "/".join(subdir_parts)
    return out


def _split_github_ref_and_path(tail: list[str]) -> tuple[str, list[str]]:
    """Best-effort split for ``/tree/<ref>/<path>`` GitHub URLs.

    GitHub does not encode where a branch name ends. Skill repositories
    normally keep skills under a top-level ``skills/`` folder, so prefer
    that marker when present; otherwise use the standard first path segment
    as the ref, matching GitHub's common ``main``/``master`` links.
    """
    marker_index = next((i for i, part in enumerate(tail[1:], start=1) if part == "skills"), None)
    if marker_index is not None:
        return "/".join(tail[:marker_index]), tail[marker_index:]
    return tail[0], tail[1:]


def _has_manifest(p: Path) -> bool:
    """A directory is a skill iff it ships a SKILL.md."""
    return (p / "SKILL.md").exists()


def _fetch(kind: str, source: str, qdir: Path, *, subdir: str | None,
           git_ref: str | None) -> Path:
    if kind == "dir":
        src = Path(source)
        if not _has_manifest(src):
            raise SkillActionError(
                f"{src} does not look like a skill (no SKILL.md)"
            )
        dst = qdir / src.name
        shutil.copytree(src, dst)
        return dst
    if kind == "tar":
        arc = Path(source)
        if not arc.exists():
            raise SkillActionError(f"tarball not found: {arc}")
        with tarfile.open(arc, "r:gz") as tf:
            _safe_extract(tf, qdir)
        return _pick_skill_dir(qdir, subdir=subdir)
    if kind == "git":
        dst = qdir / "repo"
        cmd = ["git", "clone", "--depth", "1"]
        if git_ref:
            cmd.extend(["--branch", git_ref])
        cmd.extend([source, str(dst)])
        try:
            sandbox_exec(
                cmd,
                cwd=qdir,
                root=qdir,
                check=True,
                capture_output=True,
                timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise SkillActionError(f"git clone failed: {e}") from e
        return _pick_skill_dir(dst, subdir=subdir)
    raise SkillActionError(f"unknown install kind {kind!r}")


def _pick_skill_dir(root: Path, *, subdir: str | None) -> Path:
    if subdir:
        subdir = subdir.replace("\\", "/").strip("/")
        rel = Path(subdir)
        if rel.is_absolute() or ".." in rel.parts:
            raise SkillActionError(f"unsafe skill subdir: {subdir!r}")
        p = root / subdir
        if not _has_manifest(p):
            raise SkillActionError(f"no SKILL.md under {p}")
        return p
    candidates = sorted({p.parent for p in root.rglob("SKILL.md")})
    if not candidates:
        raise SkillActionError(f"no SKILL.md found under {root}")
    if len(candidates) > 1:
        rels = sorted(c.relative_to(root).as_posix() for c in candidates)
        raise SkillActionError(
            f"multiple skills under {root}; pass subdir=... "
            f"(candidates: {rels})"
        )
    return candidates[0]


def _safe_extract(tf: tarfile.TarFile, dst: Path) -> None:
    # refuse members with absolute or parent-traversal paths
    for m in tf.getmembers():
        mp = Path(m.name)
        if mp.is_absolute() or ".." in mp.parts:
            raise SkillActionError(f"refusing unsafe tar member: {m.name!r}")
    # "data" filter is the safe PEP 706 filter available in 3.12+
    try:
        tf.extractall(dst, filter="data")
    except TypeError:
        # very old python (<3.12) — fall back to raw extract
        tf.extractall(dst)


def _load_manifest(skill_dir: Path) -> SkillManifest:
    """Load the typed manifest. SKILL.md frontmatter is the only format."""
    md = skill_dir / "SKILL.md"
    if not md.exists():
        raise SkillActionError(
            f"no manifest found under {skill_dir} (need SKILL.md)"
        )
    try:
        return SkillManifest.from_skill_md(md)
    except Exception as e:
        raise SkillActionError(f"invalid skill manifest {md}: {e}") from e


def _reject_legacy_definition_surfaces(skill_dir: Path) -> None:
    legacy = sorted(
        p.name for p in skill_dir.iterdir()
        if p.is_file() and p.name in _LEGACY_DEFINITION_SURFACES
    )
    if legacy:
        raise SkillActionError(
            "legacy skill definition surface is not accepted; "
            "use SKILL.md plus reviewed scripts/ helpers instead "
            f"(found: {legacy})"
        )


def _static_analyze(skill_dir: Path) -> list[StaticFinding]:
    """Scan user-installable skills before staging.

    The scanner is intentionally heuristic. Critical findings block staging;
    high/medium findings are written into ``install_report.json`` so the
    operator reviews executable helpers before approving promotion.
    """

    findings: list[StaticFinding] = []
    for p in sorted(skill_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(skill_dir).as_posix()
        try:
            size = p.stat().st_size
        except OSError as exc:
            findings.append(StaticFinding(
                severity="critical",
                rule_id="unreadable-file",
                path=rel,
                message=f"cannot stat file before staging: {exc}",
            ))
            continue
        if size > _SKILL_SCAN_MAX_FILE_BYTES:
            findings.append(StaticFinding(
                severity="critical",
                rule_id="oversized-file",
                path=rel,
                message=(
                    f"file exceeds scanner limit "
                    f"({_SKILL_SCAN_MAX_FILE_BYTES} bytes)"
                ),
            ))
        suffix = p.suffix.lower()
        if suffix in _BLOCKED_BINARY_EXTENSIONS:
            findings.append(StaticFinding(
                severity="critical",
                rule_id="blocked-binary-extension",
                path=rel,
                message="binary/native helper files are not accepted in external skills",
            ))
            continue
        if suffix not in _SCRIPT_SCAN_EXTENSIONS:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            findings.append(StaticFinding(
                severity="high",
                rule_id="script-unreadable",
                path=rel,
                message=f"cannot read script for static review: {exc}",
            ))
            continue
        lowered = text.lower()
        for marker, marker_kind in _DANGEROUS_SCRIPT_MARKERS.items():
            if marker in lowered:
                findings.append(StaticFinding(
                    severity="high",
                    rule_id="dangerous-script-pattern",
                    path=rel,
                    message=f"script contains {marker_kind} marker {marker!r}",
                ))
                break
    return findings


def _hash_dir(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode("utf-8"))
            h.update(b"\x00")
            h.update(p.read_bytes())
    return h.hexdigest()


def _render_rationale(r: InstallReport) -> str:
    finding_note = (
        "Static analysis recorded review findings; inspect "
        "`install_report.json` and every executable helper before approving."
        if r.static_findings
        else "Static analysis found no review findings. Operator review the "
             "staged layout at `skills/pending/` before approving."
    )
    return (
        f"# Skill install request — {r.skill_id}\n\n"
        f"- source_kind: `{r.source_kind}`\n"
        f"- source: `{r.source}`\n"
        f"- sha256: `{r.sha256}`\n"
        f"- files: {len(r.files)}\n"
        f"- static findings: {len(r.static_findings)}\n\n"
        f"{finding_note}"
    )


__all__ = [
    "InstallReport",
    "StaticFinding",
    "install_skill",
    "promote_installed",
    "list_installed",
]
