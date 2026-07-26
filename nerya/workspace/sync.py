"""Safe, explicit workspace synchronization over Git or WebDAV.

The sync surface intentionally copies a curated workspace snapshot instead of
the whole runtime directory. Credentials, ledgers, queues, databases and other
ephemeral state never enter the snapshot. WebDAV credentials resolve from the
SecretVault and Git authentication uses the user's existing credential helper;
plaintext credentials never enter the workspace sync config.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

import httpx

from ..core import yaml_io
from ..core.atomic_write import atomic_write_text
from ..core.paths import WorkspacePaths


CONFIG_NAME = "workspace-sync.yml"
MANIFEST_NAME = ".nerya-sync-manifest.json"
_MAX_WEBDAV_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_SNAPSHOT_BYTES = 1024 * 1024 * 1024

DEFAULT_INCLUDES = (
    "agents/**",
    "subagents/**",
    "skills/**",
    "memory/**",
    "strategies/**",
    "triggers/**",
    "scripts/**",
    "messages/**",
    "providers/**",
    "connectors/mcp_servers.yml",
)

# These paths cannot be opted back in through configuration. In particular,
# accounts may contain legacy plaintext exchange credentials, so the entire
# directory stays local even though newer installs normally store vault refs.
HARD_EXCLUDES = (
    ".git/**",
    "state/**",
    "journals/**",
    "inbox/**",
    "outbox/**",
    "approvals/**",
    "vault/**",
    "security/**",
    "accounts/**",
    "dev_logs/**",
    "artifacts/**",
    "**/.oauth_cache.json",
    ".env",
    "**/.env",
    "*.db",
    "**/*.db",
    "*.key",
    "**/*.key",
    "*.pem",
    "**/*.pem",
    "nerya.yml",
    MANIFEST_NAME,
)


class WorkspaceSyncError(RuntimeError):
    def __init__(self, code: str, detail: str, *, conflicts: Iterable[str] = ()):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.conflicts = tuple(conflicts)


@dataclass(frozen=True)
class WorkspaceSyncConfig:
    enabled: bool = False
    provider: str = "git"
    remote: str = ""
    branch: str = "main"
    git_path: str = "nerya-workspace"
    remote_path: str = "nerya-workspace.tar.gz"
    username_ref: str = ""
    password_ref: str = ""
    includes: tuple[str, ...] = DEFAULT_INCLUDES
    excludes: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "WorkspaceSyncConfig":
        doc = dict(raw or {})
        provider = str(doc.get("provider") or "git").strip().lower()
        if provider not in {"git", "webdav"}:
            raise WorkspaceSyncError("invalid_config", "provider must be 'git' or 'webdav'")
        remote = str(doc.get("remote") or "").strip()
        branch = str(doc.get("branch") or "main").strip()
        git_path = str(doc.get("git_path") or "nerya-workspace").strip().strip("/")
        remote_path = str(doc.get("remote_path") or "nerya-workspace.tar.gz").strip()
        includes = _string_tuple(doc.get("includes"), DEFAULT_INCLUDES)
        excludes = _string_tuple(doc.get("excludes"), ())
        username_ref = _vault_ref(doc.get("username_ref"), "username_ref")
        password_ref = _vault_ref(doc.get("password_ref"), "password_ref")

        if remote:
            parts = urlsplit(remote)
            has_forbidden_userinfo = parts.password is not None or (
                parts.username is not None
                and (provider == "webdav" or parts.scheme in {"http", "https"})
            )
            if has_forbidden_userinfo:
                raise WorkspaceSyncError(
                    "invalid_config",
                    "remote URL must not contain a password or token; use vault references",
                )
            if provider == "webdav" and parts.scheme not in {"http", "https"}:
                raise WorkspaceSyncError("invalid_config", "WebDAV remote must use http or https")
        if provider == "git" and not branch:
            raise WorkspaceSyncError("invalid_config", "Git branch is required")
        if provider == "git" and _unsafe_rel(git_path):
            raise WorkspaceSyncError("invalid_config", "Git git_path must be a safe relative path")
        if provider == "webdav" and (not remote_path or remote_path.endswith("/")):
            raise WorkspaceSyncError("invalid_config", "WebDAV remote_path must name an archive file")
        if provider == "webdav" and _unsafe_rel(remote_path):
            raise WorkspaceSyncError("invalid_config", "WebDAV remote_path must be a safe relative path")

        return cls(
            enabled=bool(doc.get("enabled", False)),
            provider=provider,
            remote=remote,
            branch=branch,
            git_path=git_path,
            remote_path=remote_path,
            username_ref=username_ref,
            password_ref=password_ref,
            includes=includes,
            excludes=excludes,
        )

    def public_dict(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["includes"] = list(self.includes)
        doc["excludes"] = list(self.excludes)
        return doc


def _string_tuple(value: Any, default: Iterable[str]) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise WorkspaceSyncError("invalid_config", "include/exclude patterns must be string arrays")
    return tuple(item.strip() for item in value if item.strip())


def _vault_ref(value: Any, field_name: str) -> str:
    ref = str(value or "").strip()
    if ref and (not ref.startswith("vault://") or len(ref) <= len("vault://")):
        raise WorkspaceSyncError("invalid_config", f"{field_name} must be a vault:// reference")
    return ref


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _is_hard_excluded(path: str) -> bool:
    return _matches(path, HARD_EXCLUDES)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(root: Path, config: WorkspaceSyncConfig) -> dict[str, str]:
    """Return the safe, deterministic set of regular files to synchronize."""
    manifest: dict[str, str] = {}
    if not root.exists():
        return manifest
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if _is_hard_excluded(rel) or _matches(rel, config.excludes):
            continue
        if not _matches(rel, config.includes):
            continue
        manifest[rel] = _hash_file(path)
    return manifest


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


class WorkspaceSyncManager:
    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, root: Path | str):
        self.paths = WorkspacePaths(Path(root).expanduser().resolve())
        self.root = self.paths.root
        self.config_path = self.paths.workspace_sync_config
        self.state_dir = self.paths.workspace_sync_state
        self.state_path = self.paths.workspace_sync_status

    @property
    def lock(self) -> threading.RLock:
        key = str(self.root)
        with self._locks_guard:
            return self._locks.setdefault(key, threading.RLock())

    def load_config(self) -> WorkspaceSyncConfig:
        raw = yaml_io.load(self.config_path, default={})
        if not isinstance(raw, dict):
            raise WorkspaceSyncError("invalid_config", f"{CONFIG_NAME} must contain a mapping")
        return WorkspaceSyncConfig.from_mapping(raw)

    def save_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        forbidden = {
            key
            for key in raw
            if any(word in key.lower() for word in ("password", "token", "secret", "username"))
            and not key.lower().endswith("_ref")
        }
        if forbidden:
            raise WorkspaceSyncError(
                "plaintext_credentials_forbidden",
                "store WebDAV credentials in SecretVault and configure vault:// references",
            )
        config = WorkspaceSyncConfig.from_mapping(raw)
        self.root.mkdir(parents=True, exist_ok=True)
        yaml_io.dump(self.config_path, config.public_dict())
        return self.status(config=config)

    def status(self, *, config: WorkspaceSyncConfig | None = None) -> dict[str, Any]:
        config = config or self.load_config()
        state = _load_json(self.state_path, {})
        credential_ready: bool | None = None
        if config.provider == "webdav":
            try:
                self._resolve_vault_ref(config.password_ref)
                credential_ready = bool(config.password_ref)
            except WorkspaceSyncError:
                credential_ready = False
        return {
            "ok": True,
            "config": config.public_dict(),
            "configured": bool(config.remote),
            "credential_ready": credential_ready,
            "git_available": shutil.which("git") is not None,
            "last_sync": state.get("last_sync"),
            "config_path": str(self.config_path),
            "safety": {
                "hard_excludes": list(HARD_EXCLUDES),
                "credentials": "vault_only",
            },
        }

    def run(self, action: str, *, force: bool = False) -> dict[str, Any]:
        action = str(action or "sync").strip().lower()
        if action not in {"pull", "push", "sync"}:
            raise WorkspaceSyncError("invalid_action", "action must be pull, push, or sync")
        with self.lock:
            config = self.load_config()
            if not config.enabled:
                raise WorkspaceSyncError("sync_disabled", "workspace sync is disabled")
            if not config.remote:
                raise WorkspaceSyncError("not_configured", "sync remote is required")
            started = _now_iso()
            results: list[dict[str, Any]] = []
            if action in {"pull", "sync"}:
                try:
                    results.append(self._pull(config, force=force))
                except WorkspaceSyncError as exc:
                    if action != "sync" or exc.code != "remote_not_found":
                        raise
                    results.append({
                        "ok": True,
                        "action": "pull",
                        "provider": config.provider,
                        "changed": False,
                        "remote_empty": True,
                    })
            if action in {"push", "sync"}:
                results.append(self._push(config, force=force))
            last = {
                "ok": True,
                "action": action,
                "provider": config.provider,
                "started_at": started,
                "finished_at": _now_iso(),
                "results": results,
            }
            state = _load_json(self.state_path, {})
            state["last_sync"] = last
            self.state_dir.mkdir(parents=True, exist_ok=True)
            _write_json(self.state_path, state)
            return last

    def _pull(self, config: WorkspaceSyncConfig, *, force: bool) -> dict[str, Any]:
        if config.provider == "git":
            snapshot, revision = self._git_pull_snapshot(config)
            result = self._apply_snapshot(snapshot, force=force)
            result.update({"provider": "git", "revision": revision})
            return result
        snapshot, etag, digest = self._webdav_pull_snapshot(config)
        if snapshot is None:
            return {"ok": True, "action": "pull", "provider": "webdav", "changed": False, "etag": etag}
        try:
            result = self._apply_snapshot(snapshot, force=force)
        finally:
            shutil.rmtree(snapshot.parent, ignore_errors=True)
        state = _load_json(self.state_path, {})
        state["remote_etag"] = etag
        state["remote_digest"] = digest
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.state_path, state)
        result.update({"provider": "webdav", "etag": etag})
        return result

    def _push(self, config: WorkspaceSyncConfig, *, force: bool) -> dict[str, Any]:
        manifest = build_manifest(self.root, config)
        if config.provider == "git":
            revision = self._git_push_snapshot(config, manifest, force=force)
            self._remember_manifest(manifest)
            return {
                "ok": True,
                "action": "push",
                "provider": "git",
                "files": len(manifest),
                "revision": revision,
            }
        etag, digest = self._webdav_push_snapshot(config, manifest, force=force)
        state = _load_json(self.state_path, {})
        state["remote_etag"] = etag
        state["remote_digest"] = digest
        state["manifest"] = manifest
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.state_path, state)
        return {
            "ok": True,
            "action": "push",
            "provider": "webdav",
            "files": len(manifest),
            "etag": etag,
        }

    def _remember_manifest(self, manifest: dict[str, str]) -> None:
        state = _load_json(self.state_path, {})
        state["manifest"] = manifest
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.state_path, state)

    def _apply_snapshot(self, snapshot: Path, *, force: bool) -> dict[str, Any]:
        manifest_path = snapshot / MANIFEST_NAME
        manifest = _load_json(manifest_path, None)
        if not isinstance(manifest, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in manifest.items()
        ):
            raise WorkspaceSyncError("invalid_snapshot", "remote snapshot has no valid manifest")
        if any(_unsafe_rel(rel) or _is_hard_excluded(rel) for rel in manifest):
            raise WorkspaceSyncError("unsafe_snapshot", "remote snapshot contains a protected path")

        previous = _load_json(self.state_path, {}).get("manifest") or {}
        conflicts: list[str] = []
        for rel, remote_hash in manifest.items():
            source = snapshot / rel
            if not source.is_file() or source.is_symlink() or _hash_file(source) != remote_hash:
                raise WorkspaceSyncError("invalid_snapshot", f"snapshot file failed verification: {rel}")
            local = self.root / rel
            if not local.exists():
                continue
            if local.is_symlink() or not local.is_file():
                conflicts.append(rel)
                continue
            local_hash = _hash_file(local)
            old_hash = previous.get(rel)
            if local_hash != remote_hash and (old_hash is None or local_hash != old_hash):
                conflicts.append(rel)
        for rel, old_hash in previous.items():
            local = self.root / rel
            if _unsafe_rel(rel) or _is_hard_excluded(rel):
                continue
            if rel not in manifest and local.is_file() and _hash_file(local) != old_hash:
                conflicts.append(rel)
        if conflicts and not force:
            raise WorkspaceSyncError(
                "sync_conflict",
                "local files changed since the last sync",
                conflicts=sorted(set(conflicts)),
            )

        written = 0
        deleted = 0
        for rel in sorted(manifest):
            source = snapshot / rel
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            written += 1
        for rel in sorted(set(previous) - set(manifest)):
            target = self.root / rel
            if target.is_file() and (force or _hash_file(target) == previous[rel]):
                target.unlink()
                deleted += 1
        self._remember_manifest(manifest)
        return {
            "ok": True,
            "action": "pull",
            "changed": bool(written or deleted),
            "written": written,
            "deleted": deleted,
            "files": len(manifest),
        }

    # ---- Git provider -------------------------------------------------

    @property
    def _git_checkout(self) -> Path:
        return self.paths.workspace_sync_git_checkout

    def _git(self, args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        if shutil.which("git") is None:
            raise WorkspaceSyncError("git_unavailable", "git executable was not found")
        env = dict(os.environ)
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(cwd) if cwd else None,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceSyncError("git_timeout", "git operation timed out") from exc
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "git command failed").strip()
            raise WorkspaceSyncError("git_failed", detail[-2000:])
        return proc

    def _ensure_git_checkout(self, config: WorkspaceSyncConfig) -> Path:
        checkout = self._git_checkout
        if not (checkout / ".git").exists():
            if checkout.exists():
                shutil.rmtree(checkout)
            checkout.parent.mkdir(parents=True, exist_ok=True)
            clone = self._git(
                ["clone", "--branch", config.branch, "--single-branch", config.remote, str(checkout)],
                check=False,
            )
            if clone.returncode != 0:
                checkout.mkdir(parents=True, exist_ok=True)
                self._git(["init", "-b", config.branch], cwd=checkout)
                self._git(["remote", "add", "origin", config.remote], cwd=checkout)
        else:
            current = self._git(["remote", "get-url", "origin"], cwd=checkout).stdout.strip()
            if current != config.remote:
                raise WorkspaceSyncError("remote_mismatch", "cached Git remote differs from workspace config")
        self._git(["config", "user.name", "Nerya Workspace Sync"], cwd=checkout)
        self._git(["config", "user.email", "workspace-sync@nerya.local"], cwd=checkout)
        return checkout

    def _git_pull_snapshot(self, config: WorkspaceSyncConfig) -> tuple[Path, str | None]:
        checkout = self._ensure_git_checkout(config)
        fetch = self._git(["fetch", "origin", config.branch], cwd=checkout, check=False)
        if fetch.returncode != 0:
            self._raise_git_fetch(fetch)
        self._git(["checkout", "-B", config.branch, f"origin/{config.branch}"], cwd=checkout)
        snapshot = checkout / config.git_path
        if not (snapshot / MANIFEST_NAME).is_file():
            raise WorkspaceSyncError("remote_not_found", "Git repository has no Nerya workspace snapshot")
        revision_proc = self._git(["rev-parse", "HEAD"], cwd=checkout, check=False)
        revision = revision_proc.stdout.strip() if revision_proc.returncode == 0 else None
        return snapshot, revision

    def _git_push_snapshot(
        self,
        config: WorkspaceSyncConfig,
        manifest: dict[str, str],
        *,
        force: bool,
    ) -> str | None:
        checkout = self._ensure_git_checkout(config)
        fetch = self._git(["fetch", "origin", config.branch], cwd=checkout, check=False)
        if fetch.returncode == 0:
            local_head = self._git(["rev-parse", "HEAD"], cwd=checkout, check=False).stdout.strip()
            remote_head = self._git(["rev-parse", f"origin/{config.branch}"], cwd=checkout).stdout.strip()
            if local_head and local_head != remote_head:
                if not force:
                    raise WorkspaceSyncError("remote_changed", "Git remote changed; pull or sync before pushing")
                self._git(["checkout", "-B", config.branch, f"origin/{config.branch}"], cwd=checkout)
        else:
            detail = (fetch.stderr or fetch.stdout or "Git fetch failed").strip()
            if not _missing_remote_branch(detail):
                raise WorkspaceSyncError("git_fetch_failed", detail[-2000:])

        snapshot = checkout / config.git_path
        if (snapshot / MANIFEST_NAME).is_file() and "manifest" not in _load_json(self.state_path, {}) and not force:
            raise WorkspaceSyncError(
                "remote_changed",
                "Git workspace snapshot already exists; pull or sync before the first push",
            )
        if snapshot.is_dir():
            shutil.rmtree(snapshot)
        elif snapshot.exists():
            snapshot.unlink()
        snapshot.mkdir(parents=True)
        for rel in manifest:
            source = self.root / rel
            target = snapshot / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        _write_json(snapshot / MANIFEST_NAME, manifest)
        self._git(["add", "-A"], cwd=checkout)
        changed = self._git(["status", "--porcelain"], cwd=checkout).stdout.strip()
        if changed:
            self._git(["commit", "-m", f"Sync Nerya workspace {_now_iso()}"], cwd=checkout)
        push_args = ["push", "-u", "origin", config.branch]
        if force:
            push_args.insert(1, "--force-with-lease")
        self._git(push_args, cwd=checkout)
        rev = self._git(["rev-parse", "HEAD"], cwd=checkout, check=False)
        return rev.stdout.strip() if rev.returncode == 0 else None

    def _raise_git_fetch(self, proc: subprocess.CompletedProcess[str]) -> None:
        detail = (proc.stderr or proc.stdout or "Git fetch failed").strip()
        code = "remote_not_found" if _missing_remote_branch(detail) else "git_fetch_failed"
        raise WorkspaceSyncError(code, detail[-2000:])

    # ---- WebDAV provider ----------------------------------------------

    def _webdav_url(self, config: WorkspaceSyncConfig) -> str:
        base = config.remote.rstrip("/") + "/"
        return base + "/".join(quote(part, safe="") for part in config.remote_path.split("/"))

    def _resolve_vault_ref(self, ref: str) -> str:
        if not ref:
            return ""
        from ..security.secrets import SecretVault

        try:
            vault = SecretVault.open(self.paths.vault_enc)
            return vault.resolve(ref.removeprefix("vault://"))
        except Exception as exc:  # noqa: BLE001
            raise WorkspaceSyncError("credential_unavailable", f"could not resolve {ref}") from exc

    def _webdav_auth(self, config: WorkspaceSyncConfig) -> tuple[str, str] | None:
        username = self._resolve_vault_ref(config.username_ref)
        password = self._resolve_vault_ref(config.password_ref)
        if password and not username:
            username = "anonymous"
        return (username, password) if username or password else None

    def _webdav_pull_snapshot(
        self,
        config: WorkspaceSyncConfig,
    ) -> tuple[Path | None, str | None, str | None]:
        state = _load_json(self.state_path, {})
        headers = {"If-None-Match": state["remote_etag"]} if state.get("remote_etag") else {}
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            response = client.get(self._webdav_url(config), auth=self._webdav_auth(config), headers=headers)
        if response.status_code == 304:
            return None, state.get("remote_etag"), state.get("remote_digest")
        if response.status_code == 404:
            raise WorkspaceSyncError("remote_not_found", "WebDAV workspace snapshot does not exist")
        if response.status_code >= 400:
            raise WorkspaceSyncError("webdav_failed", f"WebDAV GET failed with HTTP {response.status_code}")
        body = response.content
        if len(body) > _MAX_WEBDAV_ARCHIVE_BYTES:
            raise WorkspaceSyncError("snapshot_too_large", "WebDAV snapshot archive exceeds 512 MiB")
        digest = hashlib.sha256(body).hexdigest()
        temp_dir = Path(tempfile.mkdtemp(prefix="nerya-sync-pull-"))
        archive = temp_dir / "snapshot.tar.gz"
        archive.write_bytes(body)
        snapshot = temp_dir / "snapshot"
        snapshot.mkdir()
        try:
            with tarfile.open(archive, "r:gz") as tf:
                total_size = 0
                for member in tf.getmembers():
                    if (
                        member.issym()
                        or member.islnk()
                        or not (member.isfile() or member.isdir())
                        or _unsafe_rel(member.name)
                    ):
                        raise WorkspaceSyncError("unsafe_snapshot", f"unsafe archive member: {member.name}")
                    total_size += member.size
                    if total_size > _MAX_SNAPSHOT_BYTES:
                        raise WorkspaceSyncError("snapshot_too_large", "expanded snapshot exceeds 1 GiB")
                for member in tf.getmembers():
                    target = snapshot / member.name
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = tf.extractfile(member)
                    if source is None:
                        raise WorkspaceSyncError("invalid_snapshot", f"missing archive member: {member.name}")
                    with source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
        except (tarfile.TarError, OSError) as exc:
            raise WorkspaceSyncError("invalid_snapshot", "invalid WebDAV snapshot archive") from exc
        return snapshot, response.headers.get("etag"), digest

    def _webdav_push_snapshot(
        self,
        config: WorkspaceSyncConfig,
        manifest: dict[str, str],
        *,
        force: bool,
    ) -> tuple[str | None, str]:
        state = _load_json(self.state_path, {})
        headers: dict[str, str] = {"Content-Type": "application/gzip"}
        if state.get("remote_etag") and not force:
            headers["If-Match"] = state["remote_etag"]
        elif state.get("remote_digest") and not force:
            with httpx.Client(timeout=120, follow_redirects=True) as client:
                current = client.get(self._webdav_url(config), auth=self._webdav_auth(config))
            if current.status_code >= 400:
                raise WorkspaceSyncError(
                    "webdav_failed",
                    f"WebDAV remote verification failed with HTTP {current.status_code}",
                )
            if len(current.content) > _MAX_WEBDAV_ARCHIVE_BYTES:
                raise WorkspaceSyncError("snapshot_too_large", "WebDAV snapshot archive exceeds 512 MiB")
            current_digest = hashlib.sha256(current.content).hexdigest()
            if current_digest != state["remote_digest"]:
                raise WorkspaceSyncError("remote_changed", "WebDAV snapshot changed; pull before pushing")
        elif not force:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                probe = client.head(self._webdav_url(config), auth=self._webdav_auth(config))
                if probe.status_code in {405, 501}:
                    probe = client.get(
                        self._webdav_url(config),
                        auth=self._webdav_auth(config),
                        headers={"Range": "bytes=0-0"},
                    )
            if probe.status_code not in {404, 410}:
                if probe.status_code < 400:
                    raise WorkspaceSyncError(
                        "remote_changed",
                        "WebDAV snapshot already exists; pull or sync before the first push",
                    )
                raise WorkspaceSyncError(
                    "webdav_failed",
                    f"WebDAV remote probe failed with HTTP {probe.status_code}",
                )
        with tempfile.TemporaryDirectory(prefix="nerya-sync-push-") as td:
            staging = Path(td)
            _write_json(staging / MANIFEST_NAME, manifest)
            archive = staging / "snapshot.tar.gz"
            with tarfile.open(archive, "w:gz") as tf:
                tf.add(staging / MANIFEST_NAME, arcname=MANIFEST_NAME, recursive=False)
                for rel in manifest:
                    tf.add(self.root / rel, arcname=rel, recursive=False)
            digest = _hash_file(archive)
            with archive.open("rb") as content, httpx.Client(timeout=120, follow_redirects=True) as client:
                response = client.put(
                    self._webdav_url(config),
                    content=content,
                    auth=self._webdav_auth(config),
                    headers=headers,
                )
        if response.status_code in {409, 412}:
            raise WorkspaceSyncError("remote_changed", "WebDAV snapshot changed; pull or sync before pushing")
        if response.status_code >= 400:
            raise WorkspaceSyncError("webdav_failed", f"WebDAV PUT failed with HTTP {response.status_code}")
        return response.headers.get("etag"), digest


def _unsafe_rel(raw: str) -> bool:
    path = PurePosixPath(raw)
    return path.is_absolute() or not raw or "\\" in raw or ".." in path.parts


def _missing_remote_branch(detail: str) -> bool:
    lowered = detail.lower()
    return any(
        marker in lowered
        for marker in (
            "couldn't find remote ref",
            "could not find remote ref",
            "remote ref does not exist",
            "no such ref was fetched",
        )
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "CONFIG_NAME",
    "DEFAULT_INCLUDES",
    "HARD_EXCLUDES",
    "WorkspaceSyncConfig",
    "WorkspaceSyncError",
    "WorkspaceSyncManager",
    "build_manifest",
]
