from __future__ import annotations

import subprocess
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from nerya.api import routes_workspace
from nerya.api.route_scopes import required_scope
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.security.secrets import SecretVault
from nerya.workspace.sync import (
    WorkspaceSyncConfig,
    WorkspaceSyncError,
    WorkspaceSyncManager,
    build_manifest,
)


pytestmark = pytest.mark.smoke


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True, text=True)


def _configure(root, *, provider: str, remote: str) -> WorkspaceSyncManager:
    manager = WorkspaceSyncManager(root)
    manager.save_config({
        "enabled": True,
        "provider": provider,
        "remote": remote,
        "branch": "main",
    })
    return manager


def test_manifest_syncs_authored_workspace_files_but_never_credentials(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "global.md").write_text("remember me", encoding="utf-8")
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "secrets.enc").write_text("ciphertext", encoding="utf-8")
    (tmp_path / "accounts").mkdir()
    (tmp_path / "accounts" / "accounts.yml").write_text("api_secret: leaked", encoding="utf-8")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "runtime.json").write_text("{}", encoding="utf-8")
    (tmp_path / "workspace-sync.yml").write_text("remote: machine-specific\n", encoding="utf-8")
    (tmp_path / "nerya.yml").write_text("milvus:\n  token: plaintext-secret\n", encoding="utf-8")

    manifest = build_manifest(tmp_path, WorkspaceSyncConfig(enabled=True))

    assert set(manifest) == {"memory/global.md"}


def test_config_rejects_plaintext_credentials_and_password_urls(tmp_path):
    manager = WorkspaceSyncManager(tmp_path)

    with pytest.raises(WorkspaceSyncError, match="SecretVault"):
        manager.save_config({"provider": "webdav", "remote": "https://dav.test", "password": "secret"})
    with pytest.raises(WorkspaceSyncError, match="must not contain"):
        manager.save_config({"provider": "webdav", "remote": "https://u:p@dav.test"})
    with pytest.raises(WorkspaceSyncError, match="must not contain"):
        manager.save_config({"provider": "git", "remote": "https://token@github.test/org/repo.git"})
    with pytest.raises(WorkspaceSyncError, match="safe relative"):
        manager.save_config({
            "provider": "webdav",
            "remote": "https://dav.test",
            "remote_path": "../other-user.tar.gz",
        })


def test_git_provider_round_trip_and_detects_local_conflicts(tmp_path):
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("-C", str(seed), "init", "-b", "main")
    _git("-C", str(seed), "config", "user.name", "Test")
    _git("-C", str(seed), "config", "user.email", "test@nerya.local")
    (seed / "README.md").write_text("keep me\n", encoding="utf-8")
    _git("-C", str(seed), "add", "README.md")
    _git("-C", str(seed), "commit", "-m", "seed normal repository")
    _git("-C", str(seed), "remote", "add", "origin", str(remote))
    _git("-C", str(seed), "push", "-u", "origin", "main")
    first = tmp_path / "first"
    second = tmp_path / "second"
    (first / "memory").mkdir(parents=True)
    (first / "memory" / "global.md").write_text("version one", encoding="utf-8")
    first_sync = _configure(first, provider="git", remote=str(remote))

    pushed = first_sync.run("push")
    assert pushed["results"][0]["files"] == 1
    assert (first_sync._git_checkout / "README.md").read_text(encoding="utf-8") == "keep me\n"

    blind = tmp_path / "git-blind"
    (blind / "memory").mkdir(parents=True)
    (blind / "memory" / "global.md").write_text("blind overwrite", encoding="utf-8")
    with pytest.raises(WorkspaceSyncError) as caught:
        _configure(blind, provider="git", remote=str(remote)).run("push")
    assert caught.value.code == "remote_changed"

    second.mkdir()
    second_sync = _configure(second, provider="git", remote=str(remote))
    pulled = second_sync.run("pull")
    assert pulled["results"][0]["written"] == 1
    assert (second / "memory" / "global.md").read_text(encoding="utf-8") == "version one"

    (second / "memory" / "global.md").write_text("local edit", encoding="utf-8")
    (first / "memory" / "global.md").write_text("remote edit", encoding="utf-8")
    first_sync.run("push")

    with pytest.raises(WorkspaceSyncError) as caught:
        second_sync.run("pull")
    assert caught.value.code == "sync_conflict"
    assert caught.value.conflicts == ("memory/global.md",)
    assert (second / "memory" / "global.md").read_text(encoding="utf-8") == "local edit"


class _FakeDavClient:
    payload: bytes | None = None
    etag: str | None = None
    emit_etag = True
    last_auth = None

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def head(self, url, **_kwargs):
        type(self).last_auth = _kwargs.get("auth")
        status = 200 if type(self).payload is not None else 404
        return httpx.Response(status, request=httpx.Request("HEAD", url))

    def get(self, url, *, headers=None, **_kwargs):
        type(self).last_auth = _kwargs.get("auth")
        headers = headers or {}
        if type(self).payload is None:
            return httpx.Response(404, request=httpx.Request("GET", url))
        if type(self).etag and headers.get("If-None-Match") == type(self).etag:
            return httpx.Response(304, request=httpx.Request("GET", url))
        return httpx.Response(
            200,
            content=type(self).payload,
            headers={"ETag": type(self).etag or ""},
            request=httpx.Request("GET", url),
        )

    def put(self, url, *, content, headers=None, **_kwargs):
        type(self).last_auth = _kwargs.get("auth")
        headers = headers or {}
        expected = headers.get("If-Match")
        if expected and expected != type(self).etag:
            return httpx.Response(412, request=httpx.Request("PUT", url))
        type(self).payload = content.read()
        type(self).etag = '"dav-v1"' if type(self).emit_etag else None
        response_headers = {"ETag": type(self).etag} if type(self).etag else {}
        return httpx.Response(
            204,
            headers=response_headers,
            request=httpx.Request("PUT", url),
        )


def test_webdav_provider_round_trip(monkeypatch, tmp_path):
    _FakeDavClient.payload = None
    _FakeDavClient.etag = None
    _FakeDavClient.emit_etag = False
    _FakeDavClient.last_auth = None
    monkeypatch.setattr("nerya.workspace.sync.httpx.Client", _FakeDavClient)
    remote = "https://dav.test/workspace"
    first = tmp_path / "dav-first"
    (first / "strategies" / "alpha").mkdir(parents=True)
    (first / "strategies" / "alpha" / "strategy.yml").write_text("id: alpha\n", encoding="utf-8")
    first_paths = WorkspacePaths(first)
    vault = SecretVault.open(first_paths.vault_enc)
    vault.put(name="dav_user", value="nerya-user", kind="webdav", scope=["workspace_sync"])
    vault.put(name="dav_password", value="nerya-pass", kind="webdav", scope=["workspace_sync"])
    first_sync = WorkspaceSyncManager(first)
    first_sync.save_config({
        "enabled": True,
        "provider": "webdav",
        "remote": remote,
        "username_ref": "vault://dav_user",
        "password_ref": "vault://dav_password",
    })
    first_sync.run("push")
    assert _FakeDavClient.last_auth == ("nerya-user", "nerya-pass")

    blind = tmp_path / "dav-blind"
    blind.mkdir()
    with pytest.raises(WorkspaceSyncError) as caught:
        _configure(blind, provider="webdav", remote=remote).run("push")
    assert caught.value.code == "remote_changed"

    second = tmp_path / "dav-second"
    second.mkdir()
    second_sync = _configure(second, provider="webdav", remote=remote)
    second_sync.run("pull")
    assert (second / "strategies" / "alpha" / "strategy.yml").read_text(encoding="utf-8") == "id: alpha\n"
    (second / "strategies" / "alpha" / "strategy.yml").write_text("id: alpha-v2\n", encoding="utf-8")
    second_sync.run("push")


def test_workspace_sync_routes_and_scopes(tmp_path):
    config = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    client = SimpleNamespace(config=config)
    routes = {(method, path): handler for method, path, handler in routes_workspace.routes()}

    saved = routes[("POST", "/workspace/sync/config")](client, {
        "enabled": True,
        "provider": "git",
        "remote": str(tmp_path / "remote.git"),
    })

    assert saved["ok"] is True
    assert saved["config"]["provider"] == "git"
    assert routes[("GET", "/workspace/sync")](client, {})["configured"] is True
    assert required_scope("POST", "/workspace/sync/config") == "write:config"
    assert required_scope("POST", "/workspace/sync/run") == "admin:ops"
