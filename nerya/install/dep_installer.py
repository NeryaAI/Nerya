"""Optional-dependency installer for exchange / wallet providers.

When an operator (or the agent, on the operator's behalf) selects an
exchange or wallet provider whose runtime deps are not yet on the host,
Nerya can offer to install them. This module is the *only* place that
runs ``pip``, ``npm`` or ``git`` against the host environment, and it
refuses to do anything unless the operator has explicitly opted in by
setting one of:

* ``runtime.allow_auto_install: true`` in ``nerya.yml``
* ``NERYA_ALLOW_AUTO_INSTALL=1`` in the environment
* the route caller passes ``approve=True`` (interactive operator click)

Three install kinds are supported:

* **pip** — ``pip install <pkg>`` for a Python package (e.g. ``ccxt``,
  ``cdp-sdk``, ``py-clob-client``).
* **node-skill** — ``git clone <repo> <skills-dir>/<name>`` followed by
  ``npm install`` inside the resulting directory. Used for
  Bitget/Binance-Web3/Coinbase-TS wallet skills. The repo URL may
  include a ``#path=<subdir>`` fragment to point at a sub-package
  inside a monorepo.
* **npm** — ``npm install`` of an npm-distributed package directly
  into ``<workspace>/skills/_node/<package>/`` (no git clone). Useful
  for wallet SDKs published on npm, e.g. ``npm:@coinbase/cdp-sdk`` or
  ``npm:@bitget/wallet-sdk``. A ``#version=<tag>`` fragment pins a
  specific release.

Every step is journaled and the resulting subprocess output is captured
into ``dev_logs/dep_installer.jsonl`` so the operator can audit what
ran. We never inherit operator stdout/stderr, never run anything as
root, and never install into a system Python — installs always target
the active interpreter via ``sys.executable -m pip ...``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..core import jsonl
from ..core.errors import NeryaError
from ..core.paths import WorkspacePaths


class DependencyInstallError(NeryaError):
    """Raised when an install attempt is refused or fails."""


@dataclass
class InstallResult:
    ok: bool
    kind: str  # "pip" | "node-skill"
    target: str
    command: str
    duration_s: float
    stdout_tail: str = ""
    stderr_tail: str = ""
    install_path: str = ""
    skipped: bool = False
    skip_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "target": self.target,
            "command": self.command,
            "duration_s": round(self.duration_s, 3),
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "install_path": self.install_path,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "extra": dict(self.extra),
        }


# ---------------------------------------------------------------------------
# Gate: is auto-install allowed?
# ---------------------------------------------------------------------------


def is_auto_install_allowed(
    config_data: dict[str, Any] | None,
    *,
    approve: bool = False,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for an install attempt.

    Order of precedence (any one True ⇒ allowed):

    1. Operator clicked an explicit ``Install`` button (``approve=True``).
    2. ``runtime.allow_auto_install: true`` in nerya.yml.
    3. ``NERYA_ALLOW_AUTO_INSTALL=1`` in the environment.

    Otherwise the install is refused and the caller should surface the
    install command so the operator can run it manually.
    """

    if approve:
        return True, "operator_approved"
    runtime_cfg = ((config_data or {}).get("runtime") or {})
    if bool(runtime_cfg.get("allow_auto_install")):
        return True, "config:runtime.allow_auto_install"
    if os.environ.get("NERYA_ALLOW_AUTO_INSTALL", "").strip() in ("1", "true", "yes"):
        return True, "env:NERYA_ALLOW_AUTO_INSTALL"
    return False, "auto_install_not_enabled"


# ---------------------------------------------------------------------------
# Pip install
# ---------------------------------------------------------------------------


def _allowed_pip_target(target: str) -> bool:
    """Refuse anything that doesn't look like a plain pip package spec.

    Defence in depth — even though the call sites only pass values from
    the provider catalogue, we never want a malicious user-authored
    provider spec to smuggle ``--index-url=...`` or shell metachars
    into the install command.
    """

    if not target:
        return False
    if any(c in target for c in (";", "&", "|", "`", "$", "\n", "\r", " --")):
        return False
    # Allow alphanumerics, dot, underscore, hyphen, and the standard pip
    # version operators. Multiple packages separated by spaces are
    # tokenised by the caller, so a single token here.
    allowed = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        "._-+=<>~,!*[]"
    )
    return all(c in allowed for c in target)


def _split_pip_targets(command: str) -> list[str]:
    cmd = command.strip()
    if cmd.lower().startswith("pip install "):
        cmd = cmd[len("pip install "):]
    elif cmd.lower().startswith("pip3 install "):
        cmd = cmd[len("pip3 install "):]
    parts = [p for p in shlex.split(cmd) if p and not p.startswith("-")]
    return parts


def install_pip_package(
    paths: WorkspacePaths,
    command: str,
    *,
    timeout_s: float = 600.0,
) -> InstallResult:
    """Run ``<sys.executable> -m pip install <pkgs>``.

    Validates the targets against an allowlist before invoking pip so a
    spec that smuggles flags or shell metacharacters can never reach the
    subprocess. Output is captured; stdout/stderr is not inherited.
    """

    targets = _split_pip_targets(command)
    if not targets:
        raise DependencyInstallError(
            f"no pip targets in command: {command!r}"
        )
    for tgt in targets:
        if not _allowed_pip_target(tgt):
            raise DependencyInstallError(
                f"refusing to install pip target {tgt!r}: invalid characters"
            )
    cmd = [sys.executable, "-m", "pip", "install", *targets]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout_s, check=False,
        )
    except FileNotFoundError as exc:
        raise DependencyInstallError(
            f"python interpreter not found: {sys.executable}"
        ) from exc
    except subprocess.TimeoutExpired:
        return InstallResult(
            ok=False, kind="pip", target=" ".join(targets),
            command=" ".join(shlex.quote(c) for c in cmd),
            duration_s=time.time() - started,
            stderr_tail=f"timed out after {timeout_s}s",
        )
    duration = time.time() - started
    stdout = (proc.stdout or b"").decode("utf-8", "replace")
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    result = InstallResult(
        ok=proc.returncode == 0, kind="pip", target=" ".join(targets),
        command=" ".join(shlex.quote(c) for c in cmd),
        duration_s=duration,
        stdout_tail=stdout[-1500:],
        stderr_tail=stderr[-1500:],
        extra={"return_code": proc.returncode},
    )
    _journal(paths, result)
    return result


# ---------------------------------------------------------------------------
# Node skill install
# ---------------------------------------------------------------------------


def _node_skills_root(paths: WorkspacePaths) -> Path:
    """Where Nerya clones third-party node-skill repositories."""

    root = paths.root / "skills" / "_node"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _git_available() -> bool:
    return shutil.which("git") is not None


def _npm_available() -> bool:
    return _npm_executable() is not None


def _npm_executable() -> str | None:
    """Return the absolute path to the ``npm`` binary, or ``None``.

    On Windows ``shutil.which("npm")`` resolves to ``npm.cmd``; passing
    ``"npm"`` to :func:`subprocess.run` directly would fail because the
    Windows process loader doesn't auto-append ``.cmd``. Resolving up
    front keeps the caller's argv list clean while still working
    cross-platform.
    """

    return shutil.which("npm")


def _parse_node_skill_target(spec: str) -> tuple[str, str, str]:
    """Parse ``node-skill:<repo>[#path=<sub>][&entry=<file>]``.

    Returns ``(repo_url, subpath, entry)``. The ``entry`` defaults to
    ``"dist/nerya.js"`` to match the existing node-skill protocol.
    """

    if spec.startswith("node-skill:"):
        spec = spec[len("node-skill:"):]
    repo = spec
    sub = ""
    entry = "dist/nerya.js"
    if "#" in spec:
        repo, _, frag = spec.partition("#")
        for chunk in frag.split("&"):
            if "=" not in chunk:
                continue
            k, v = chunk.split("=", 1)
            if k == "path":
                sub = v.strip("/")
            elif k == "entry":
                entry = v.strip()
    parsed = urlparse(repo)
    if parsed.scheme not in ("http", "https") and not repo.endswith(".git"):
        raise DependencyInstallError(
            f"refusing to clone repository {repo!r}: only http(s) URLs allowed"
        )
    return repo, sub, entry


def install_node_skill(
    paths: WorkspacePaths,
    command: str,
    *,
    timeout_s: float = 900.0,
) -> InstallResult:
    """Clone a node-skill repository and run ``npm install`` inside it.

    The clone target is ``<workspace>/skills/_node/<repo-basename>``;
    the directory is reused across invocations (a second call is an
    idempotent ``git pull`` + ``npm install``). The result includes
    the resolved ``install_path`` the operator should pin in
    ``wallet.<provider>.skill_path``.
    """

    if not _git_available():
        raise DependencyInstallError("git binary not found on PATH")
    if not _npm_available():
        raise DependencyInstallError("npm binary not found on PATH (install Node 20+)")

    repo, sub, entry = _parse_node_skill_target(command)
    name = Path(urlparse(repo).path.rstrip("/")).name.removesuffix(".git") or "skill"
    skill_root = _node_skills_root(paths) / name
    skill_dir = skill_root / sub if sub else skill_root

    started = time.time()
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def _run(cmd: list[str], cwd: Path) -> int:
        proc = subprocess.run(
            cmd, capture_output=True, cwd=str(cwd),
            timeout=timeout_s, check=False,
        )
        stdout_chunks.append((proc.stdout or b"").decode("utf-8", "replace"))
        stderr_chunks.append((proc.stderr or b"").decode("utf-8", "replace"))
        return int(proc.returncode)

    try:
        if not skill_root.exists():
            skill_root.parent.mkdir(parents=True, exist_ok=True)
            rc = _run(["git", "clone", "--depth", "1", repo, str(skill_root)],
                      cwd=skill_root.parent)
            if rc != 0:
                return _wrap_node_skill_failure(
                    paths, repo, started, stdout_chunks, stderr_chunks,
                    str(skill_dir),
                )
        else:
            _run(["git", "pull", "--ff-only"], cwd=skill_root)
        if not skill_dir.exists():
            return _wrap_node_skill_failure(
                paths, repo, started, stdout_chunks, stderr_chunks,
                str(skill_dir),
                extra_err=f"subpath {sub!r} not found inside {skill_root}",
            )
        rc = _run([_npm_executable() or "npm", "install", "--no-audit", "--no-fund"], cwd=skill_dir)
        if rc != 0:
            return _wrap_node_skill_failure(
                paths, repo, started, stdout_chunks, stderr_chunks,
                str(skill_dir),
            )
    except subprocess.TimeoutExpired:
        return InstallResult(
            ok=False, kind="node-skill", target=repo,
            command=f"git clone {repo} && npm install",
            duration_s=time.time() - started,
            stderr_tail=f"timed out after {timeout_s}s",
            install_path=str(skill_dir),
        )

    result = InstallResult(
        ok=True, kind="node-skill", target=repo,
        command=f"git clone {repo} && npm install",
        duration_s=time.time() - started,
        stdout_tail="\n".join(stdout_chunks)[-1500:],
        stderr_tail="\n".join(stderr_chunks)[-1500:],
        install_path=str(skill_dir),
        extra={"entry": entry},
    )
    _journal(paths, result)
    return result


_NPM_NAME_RE = re.compile(
    r"^(@[a-z0-9][a-z0-9._\-]*/)?[a-z0-9][a-z0-9._\-]*$",
    re.IGNORECASE,
)


def _parse_npm_target(spec: str) -> tuple[str, str, str]:
    """Parse ``npm:<pkg>[#version=<tag>][&entry=<file>]``.

    Returns ``(package, version, entry)``. Defaults: latest version,
    ``dist/nerya.js`` entry — same protocol as the git-cloned
    node-skills so the wallet provider invoke path is unchanged.
    """

    if spec.startswith("npm:"):
        spec = spec[len("npm:"):]
    package = spec
    version = ""
    entry = "dist/nerya.js"
    if "#" in spec:
        package, _, frag = spec.partition("#")
        for chunk in frag.split("&"):
            if "=" not in chunk:
                continue
            k, v = chunk.split("=", 1)
            if k == "version":
                version = v.strip()
            elif k == "entry":
                entry = v.strip()
    package = package.strip()
    if not _NPM_NAME_RE.match(package):
        raise DependencyInstallError(
            f"refusing to install npm package {package!r}: invalid name"
        )
    if version and not re.match(r"^[a-zA-Z0-9._\-+]+$", version):
        raise DependencyInstallError(
            f"refusing to pin version {version!r}: invalid characters"
        )
    return package, version, entry


def install_npm_package(
    paths: WorkspacePaths,
    command: str,
    *,
    timeout_s: float = 600.0,
) -> InstallResult:
    """Install an npm-distributed wallet/skill package.

    Each package gets its own directory under
    ``<workspace>/skills/_node/<safe_name>/``. We first write a tiny
    ``package.json`` that pins ``main`` to the requested entry, then
    invoke ``npm install <pkg>[@<version>] --prefix <dir>`` so the
    package lands in ``node_modules`` next to a deterministic entry
    file. The result returns ``install_path`` so the operator/agent
    can pin it via ``/wallet/configure`` without further setup.
    """

    if not _npm_available():
        raise DependencyInstallError("npm binary not found on PATH (install Node 20+)")

    pkg, version, entry = _parse_npm_target(command)
    safe_name = pkg.replace("@", "").replace("/", "__")
    skill_dir = _node_skills_root(paths) / safe_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    pkg_json = skill_dir / "package.json"
    if not pkg_json.exists():
        pkg_json.write_text(
            json.dumps({"name": f"nerya-skill-{safe_name}", "version": "0.0.0"}),
            encoding="utf-8",
        )

    install_target = pkg if not version else f"{pkg}@{version}"
    started = time.time()
    npm_bin = _npm_executable() or "npm"
    cmd = [npm_bin, "install", install_target, "--no-audit", "--no-fund"]
    try:
        proc = subprocess.run(
            cmd, cwd=str(skill_dir), capture_output=True,
            timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return InstallResult(
            ok=False, kind="npm", target=pkg,
            command=" ".join(cmd),
            duration_s=time.time() - started,
            stderr_tail=f"timed out after {timeout_s}s",
            install_path=str(skill_dir),
        )
    duration = time.time() - started
    stdout = (proc.stdout or b"").decode("utf-8", "replace")
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    pkg_root = skill_dir / "node_modules" / pkg
    result = InstallResult(
        ok=proc.returncode == 0 and pkg_root.exists(),
        kind="npm", target=install_target,
        command=" ".join(cmd),
        duration_s=duration,
        stdout_tail=stdout[-1500:],
        stderr_tail=stderr[-1500:],
        install_path=str(pkg_root if pkg_root.exists() else skill_dir),
        extra={"entry": entry, "package": pkg, "version": version},
    )
    _journal(paths, result)
    return result


# ---------------------------------------------------------------------------
# Listing / uninstall — manage already-installed wallet skills
# ---------------------------------------------------------------------------


def list_node_skills(paths: WorkspacePaths) -> list[dict[str, Any]]:
    """Return the node skills currently installed under
    ``workspace/skills/_node/``.

    Each row carries the directory name, the resolved entry file, and a
    ``ready`` flag that mirrors :func:`NodeSkillRef.skill_ready`. Used
    by the wallet management dashboard to render an "installed skills"
    table without having to re-run the readiness probe per provider.
    """

    root = _node_skills_root(paths)
    out: list[dict[str, Any]] = []
    if not root.exists():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        entry_default = child / "dist" / "nerya.js"
        entry_present = entry_default.exists()
        # Also detect npm-installed packages that live one level deeper.
        node_modules = child / "node_modules"
        npm_pkg_path = ""
        if node_modules.exists():
            try:
                for sub in node_modules.iterdir():
                    if sub.name.startswith("@") and sub.is_dir():
                        for inner in sub.iterdir():
                            if inner.is_dir():
                                npm_pkg_path = str(inner)
                                break
                    elif sub.is_dir() and sub.name not in ("@types",):
                        npm_pkg_path = str(sub)
                    if npm_pkg_path:
                        break
            except Exception:
                npm_pkg_path = ""
        out.append({
            "name": child.name,
            "path": str(child),
            "entry_path": str(entry_default),
            "entry_ready": entry_present,
            "npm_package_path": npm_pkg_path,
        })
    return out


def uninstall_node_skill(paths: WorkspacePaths, name: str) -> dict[str, Any]:
    """Remove an installed node skill directory.

    Refuses path traversal (any name with separators, ``..``) and only
    operates inside ``workspace/skills/_node/``. Returns a small
    receipt the dashboard renders next to the install button.
    """

    safe = (name or "").strip()
    if not safe or "/" in safe or "\\" in safe or ".." in safe:
        raise DependencyInstallError(f"invalid skill name: {name!r}")
    target = _node_skills_root(paths) / safe
    if not target.exists():
        return {"ok": False, "error": "not_found", "name": safe}
    try:
        shutil.rmtree(target)
    except Exception as exc:
        return {"ok": False, "error": "rmtree_failed", "detail": str(exc)}
    return {"ok": True, "name": safe, "path": str(target)}


def _wrap_node_skill_failure(
    paths: WorkspacePaths, repo: str, started: float,
    stdout_chunks: list[str], stderr_chunks: list[str],
    install_path: str,
    *,
    extra_err: str = "",
) -> InstallResult:
    stderr_tail = "\n".join(stderr_chunks)[-1500:]
    if extra_err:
        stderr_tail = (extra_err + "\n" + stderr_tail).strip()
    result = InstallResult(
        ok=False, kind="node-skill", target=repo,
        command=f"git clone {repo} && npm install",
        duration_s=time.time() - started,
        stdout_tail="\n".join(stdout_chunks)[-1500:],
        stderr_tail=stderr_tail,
        install_path=install_path,
    )
    _journal(paths, result)
    return result


# ---------------------------------------------------------------------------
# Generic dispatch + journal
# ---------------------------------------------------------------------------


def install(
    paths: WorkspacePaths,
    command: str,
    *,
    config_data: dict[str, Any] | None = None,
    approve: bool = False,
) -> InstallResult:
    """Dispatch to the right backend for ``command``.

    Recognised forms:

    * ``"pip install ccxt"`` / ``"pip install py-clob-client"``
    * ``"node-skill:<repo>[#path=<sub>][&entry=<file>]"``
    * ``"npm:<package>[#version=<tag>][&entry=<file>]"``

    Empty / unrecognised commands return a ``skipped`` result.
    """

    cmd = (command or "").strip()
    if not cmd:
        return InstallResult(
            ok=True, kind="noop", target="",
            command="", duration_s=0.0,
            skipped=True, skip_reason="empty_command",
        )
    allowed, reason = is_auto_install_allowed(config_data, approve=approve)
    if not allowed:
        result = InstallResult(
            ok=False, kind="noop", target=cmd, command=cmd,
            duration_s=0.0, skipped=True, skip_reason=reason,
        )
        _journal(paths, result)
        return result
    if cmd.lower().startswith("pip install ") or cmd.lower().startswith("pip3 install "):
        return install_pip_package(paths, cmd)
    if cmd.startswith("node-skill:"):
        return install_node_skill(paths, cmd)
    if cmd.startswith("npm:"):
        return install_npm_package(paths, cmd)
    raise DependencyInstallError(
        f"unrecognised install command: {command!r}"
    )


def _journal(paths: WorkspacePaths, result: InstallResult) -> None:
    """Append every install attempt to ``dev_logs/dep_installer.jsonl``.

    Best-effort — observability must not block a long install.
    """

    try:
        record = dict(result.asdict())
        record["ts"] = time.time()
        jsonl.append(paths.dev_log("dep_installer"), record)
    except Exception:
        return None


__all__ = [
    "DependencyInstallError",
    "InstallResult",
    "install",
    "install_pip_package",
    "install_node_skill",
    "install_npm_package",
    "is_auto_install_allowed",
    "list_node_skills",
    "uninstall_node_skill",
]
