"""Optional memsearch-backed semantic index for Nerya memory.

The markdown files under ``memory/`` and strategy ``learnings.md`` files stay
the source of truth. memsearch is a derived, rebuildable index that is disabled
by default and only installed / started after an explicit operator action.

Embedding configuration
-----------------------
memsearch internally supports multiple embedding backends (openai, google,
voyage, ollama, local) but its OpenAI client only picks up a custom base URL
via the ``OPENAI_BASE_URL`` environment variable. To let operators point at
any OpenAI-compatible endpoint (Gitee AI, SiliconFlow, DeepSeek, proxies)
without shelling into the process, we:

* store provider / model / base_url / api_key_ref under
  ``memory.vector_search.embedding`` in ``config.yaml``;
* resolve ``api_key_ref`` (``vault://<name>``) through the same SecretVault
  the LLM plane uses, so users can reuse an existing LLM provider key;
* set the relevant env vars in the current process (for ``reindex`` /
  ``search``) or the watcher subprocess before instantiating ``MemSearch``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..core import yaml_io
from ..core.config import Config


_WATCHER_PROCESS: subprocess.Popen | None = None

_ENV_VARS_BY_PROVIDER: dict[str, tuple[str, str | None]] = {
    # provider -> (api_key_env_name, base_url_env_name or None)
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "google": ("GOOGLE_API_KEY", None),
    "voyage": ("VOYAGE_API_KEY", None),
    "ollama": ("OLLAMA_API_KEY", "OLLAMA_BASE_URL"),
    "local": ("", None),
}


def _cfg(config: Config) -> dict[str, Any]:
    raw = config.get("memory.vector_search", {}) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _embedding_cfg(config: Config) -> dict[str, Any]:
    raw = _cfg(config).get("embedding") or {}
    if not isinstance(raw, dict):
        return {}
    return dict(raw)


def _milvus_cfg(config: Config) -> dict[str, Any]:
    raw = _cfg(config).get("milvus") or {}
    if not isinstance(raw, dict):
        return {}
    return dict(raw)


def _relative_path(root: Path, raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = root / p
    return p.resolve()


def source_paths(config: Config) -> list[Path]:
    cfg = _cfg(config)
    configured = cfg.get("paths") or ["memory", "strategies"]
    paths: list[Path] = []
    for raw in configured:
        text = str(raw or "").strip()
        if not text:
            continue
        paths.append(_relative_path(config.paths.root, text))
    if not paths:
        paths = [config.paths.memory, config.paths.strategies]
    return paths


def dependency_available() -> bool:
    return importlib.util.find_spec("memsearch") is not None


def _is_local_milvus_uri(uri: str) -> bool:
    u = str(uri or "").strip().lower()
    return not u.startswith(("http://", "https://", "tcp://", "grpc://"))


def runtime_dependency_gap(config: Config) -> str:
    """Return '' when search/index can actually run, else a short reason.

    ``memsearch`` alone is not enough: a local (file-based) Milvus URI
    additionally needs ``milvus_lite``, which has no Windows wheels — on
    such hosts a bare import check passes but every search raises deep
    inside pymilvus. Detect that up front so callers get a clean
    ``dependency_missing`` instead of a stack trace.
    """
    if importlib.util.find_spec("memsearch") is None:
        return "memsearch_not_installed"
    uri = str(_milvus_cfg(config).get("uri") or "~/.memsearch/milvus.db")
    if _is_local_milvus_uri(uri) and importlib.util.find_spec("milvus_lite") is None:
        return "milvus_lite_not_installed"
    return ""


def _resolve_vault_key(config: Config, ref: str) -> str:
    """Resolve a ``vault://<name>`` ref through the workspace SecretVault.

    Returns empty string when the ref is missing / unresolvable. Never
    raises — callers fall back to plaintext / env lookup.
    """
    if not ref:
        return ""
    try:
        from ..security.secrets import SecretVault  # local import, optional
    except Exception:
        return ""
    if not ref.startswith("vault://"):
        return ""
    name = ref.split("vault://", 1)[-1].strip()
    if not name:
        return ""
    vault_path = config.paths.vault_enc
    if not vault_path.exists():
        return ""
    try:
        vault = SecretVault.open(vault_path)
        # No scope restriction: the vault is operator-managed and we want
        # to support reusing existing ``llm`` entries here.
        return vault.resolve(name)
    except Exception:
        return ""


def status(config: Config) -> dict[str, Any]:
    cfg = _cfg(config)
    process_running = bool(_WATCHER_PROCESS and _WATCHER_PROCESS.poll() is None)
    emb = _embedding_cfg(config)
    milvus = _milvus_cfg(config)
    api_key_ref = str(emb.get("api_key_ref") or "").strip()
    return {
        "ok": True,
        "enabled": bool(cfg.get("enabled", False)),
        "backend": cfg.get("backend") or "memsearch",
        "dependency_available": dependency_available(),
        "dependency_gap": runtime_dependency_gap(config),
        "install_package": cfg.get("install_package") or "memsearch",
        "watch_enabled": bool(cfg.get("watch_enabled", False)),
        "watcher_running": process_running,
        "paths": [str(p) for p in source_paths(config)],
        "embedding": {
            "provider": str(emb.get("provider") or "openai"),
            "model": str(emb.get("model") or "text-embedding-3-small"),
            "base_url": str(emb.get("base_url") or ""),
            "api_key_ref": api_key_ref,
            "has_key": bool(_resolve_vault_key(config, api_key_ref)),
        },
        "milvus": {
            "uri": str(milvus.get("uri") or "~/.memsearch/milvus.db"),
            "collection": str(milvus.get("collection") or "memsearch_chunks"),
            "has_token": bool(str(milvus.get("token") or "").strip()),
        },
    }


def _apply_embedding_patch(
    vector: dict[str, Any], patch: dict[str, Any] | None,
    *,
    config: Config | None = None,
) -> None:
    """Merge an embedding patch into ``vector["embedding"]``.

    When the patch contains a non-empty ``api_key_plain`` and a
    ``Config`` is supplied, the plaintext key is stored in the
    SecretVault under a deterministic name and ``api_key_ref`` is
    rewritten to ``vault://<name>``. This is what powers the dashboard's
    "paste a new key" affordance: the operator never has to manage a
    `vault://...` ref manually.
    """

    if not isinstance(patch, dict):
        return
    current = vector.get("embedding")
    if not isinstance(current, dict):
        current = {}
    if "provider" in patch and patch["provider"] is not None:
        current["provider"] = str(patch["provider"]).strip().lower() or "openai"
    if "model" in patch and patch["model"] is not None:
        current["model"] = str(patch["model"]).strip()
    if "base_url" in patch and patch["base_url"] is not None:
        current["base_url"] = str(patch["base_url"]).strip()
    if "api_key_ref" in patch and patch["api_key_ref"] is not None:
        current["api_key_ref"] = str(patch["api_key_ref"]).strip()
    plain = str(patch.get("api_key_plain") or "").strip()
    if plain and config is not None:
        provider = current.get("provider") or "openai"
        ref = _persist_embedding_key(config, provider=str(provider), value=plain)
        if ref:
            current["api_key_ref"] = ref
    vector["embedding"] = current


def _persist_embedding_key(config: Config, *, provider: str, value: str) -> str:
    """Stash a plaintext embedding key in the SecretVault.

    Returns the ``vault://<name>`` reference that should be written to
    the embedding config, or empty string if the vault is unreachable
    (the caller will fall through to whatever ``api_key_ref`` the
    operator typed manually).

    The vault entry is named ``memory_embedding_<provider>`` so re-saving
    rotates the key in place rather than creating an audit pile-up.
    """

    try:
        from ..security.secrets import SecretVault  # local import, optional dep
    except Exception:
        return ""
    try:
        vault = SecretVault.open(config.paths.vault_enc)
    except Exception:
        return ""
    name = f"memory_embedding_{(provider or 'openai').strip().lower()}"
    try:
        vault.put(
            name=name,
            value=value,
            kind="api_key",
            scope=["memory:embedding"],
            owner="dashboard",
        )
    except Exception:
        return ""
    return f"vault://{name}"


def _apply_milvus_patch(
    vector: dict[str, Any], patch: dict[str, Any] | None
) -> None:
    if not isinstance(patch, dict):
        return
    current = vector.get("milvus")
    if not isinstance(current, dict):
        current = {}
    if "uri" in patch and patch["uri"] is not None:
        current["uri"] = str(patch["uri"]).strip() or "~/.memsearch/milvus.db"
    if "token" in patch and patch["token"] is not None:
        current["token"] = str(patch["token"]).strip()
    if "collection" in patch and patch["collection"] is not None:
        current["collection"] = (
            str(patch["collection"]).strip() or "memsearch_chunks"
        )
    vector["milvus"] = current


def configure(
    config: Config,
    *,
    enabled: bool | None = None,
    watch_enabled: bool | None = None,
    paths: list[str] | None = None,
    install_package: str | None = None,
    embedding: dict[str, Any] | None = None,
    milvus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = yaml_io.load(config.paths.config, default={}) or {}
    if not isinstance(existing, dict):
        existing = {}
    memory = existing.setdefault("memory", {})
    if not isinstance(memory, dict):
        memory = {}
        existing["memory"] = memory
    vector = memory.setdefault("vector_search", {})
    if not isinstance(vector, dict):
        vector = {}
        memory["vector_search"] = vector

    vector.setdefault("backend", "memsearch")
    vector.setdefault("install_package", "memsearch")
    vector.setdefault("paths", ["memory", "strategies"])
    vector.setdefault(
        "embedding",
        {
            "provider": "openai",
            "model": "text-embedding-3-small",
            "base_url": "",
            "api_key_ref": "",
        },
    )
    vector.setdefault(
        "milvus",
        {
            "uri": "~/.memsearch/milvus.db",
            "token": "",
            "collection": "memsearch_chunks",
        },
    )
    if enabled is not None:
        vector["enabled"] = bool(enabled)
        if enabled:
            # Enhanced memory backends are operator-selected. memsearch and
            # agentmemory must not both inject recall for the same workspace.
            external = memory.setdefault("external", {})
            if isinstance(external, dict):
                external["enabled"] = False
                external["provider"] = ""
    if watch_enabled is not None:
        vector["watch_enabled"] = bool(watch_enabled)
    if paths is not None:
        cleaned = [str(p).strip() for p in paths if str(p or "").strip()]
        vector["paths"] = cleaned or ["memory", "strategies"]
    if install_package:
        vector["install_package"] = install_package
    _apply_embedding_patch(vector, embedding, config=config)
    _apply_milvus_patch(vector, milvus)

    yaml_io.dump(config.paths.config, existing)
    config.data.setdefault("memory", {})
    if not isinstance(config.data["memory"], dict):
        config.data["memory"] = {}
    config.data["memory"]["vector_search"] = vector
    if enabled:
        external_cfg = memory.get("external")
        if isinstance(external_cfg, dict):
            config.data["memory"]["external"] = external_cfg
    return status(config)


def install_dependency(config: Config) -> dict[str, Any]:
    cfg = _cfg(config)
    if not bool(cfg.get("enabled", False)):
        return {
            "ok": False,
            "error": "vector_search_disabled",
            "detail": "Enable memory.vector_search before installing memsearch.",
        }
    package = str(cfg.get("install_package") or "memsearch").strip()
    if not package:
        package = "memsearch"
    cmd = [sys.executable, "-m", "pip", "install", package]
    proc = subprocess.run(
        cmd,
        cwd=str(config.paths.root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    importlib.invalidate_caches()
    return {
        "ok": proc.returncode == 0,
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
        "dependency_available": dependency_available(),
    }


def _require_ready(config: Config) -> tuple[bool, dict[str, Any] | None]:
    cfg = _cfg(config)
    if not bool(cfg.get("enabled", False)):
        return False, {"ok": False, "error": "vector_search_disabled"}
    if str(cfg.get("backend") or "memsearch") != "memsearch":
        return False, {"ok": False, "error": "unsupported_backend"}
    gap = runtime_dependency_gap(config)
    if gap:
        detail = ""
        if gap == "milvus_lite_not_installed":
            detail = (
                "milvus-lite is required for the local Milvus URI but is "
                "not installed (no Windows wheels exist). Point "
                "memory.vector_search.milvus.uri at a remote Milvus server "
                "(http://...) or disable vector search."
            )
        return False, {
            "ok": False,
            "error": "dependency_missing",
            "dependency_gap": gap,
            "detail": detail,
            "install_package": cfg.get("install_package") or "memsearch",
        }
    return True, None


def _resolved_env(config: Config) -> dict[str, str]:
    """Env vars to inject so memsearch's embedding backend can authenticate.

    Returns a copy of ``os.environ`` plus any provider-specific keys / URLs
    derived from config. Empty values are left unset to avoid clobbering
    something the operator set in their shell.
    """
    emb = _embedding_cfg(config)
    provider = str(emb.get("provider") or "openai").strip().lower()
    base_url = str(emb.get("base_url") or "").strip()
    api_key_ref = str(emb.get("api_key_ref") or "").strip()
    resolved_key = _resolve_vault_key(config, api_key_ref)

    env = dict(os.environ)
    key_env, url_env = _ENV_VARS_BY_PROVIDER.get(provider, ("", None))
    if key_env and resolved_key:
        env[key_env] = resolved_key
    if url_env and base_url:
        env[url_env] = base_url
    return env


def _memsearch_kwargs(config: Config, *, paths: list[Path]) -> dict[str, Any]:
    emb = _embedding_cfg(config)
    milvus = _milvus_cfg(config)
    kwargs: dict[str, Any] = {
        "paths": [str(p) for p in paths if p.exists()],
        "embedding_provider": str(emb.get("provider") or "openai").strip().lower()
        or "openai",
    }
    model = str(emb.get("model") or "").strip()
    if model:
        kwargs["embedding_model"] = model
    milvus_uri = str(milvus.get("uri") or "").strip()
    if milvus_uri:
        kwargs["milvus_uri"] = milvus_uri
    milvus_token = str(milvus.get("token") or "").strip()
    if milvus_token:
        kwargs["milvus_token"] = milvus_token
    collection = str(milvus.get("collection") or "").strip()
    if collection:
        kwargs["collection"] = collection
    return kwargs


def _build_memsearch(config: Config, *, paths: list[Path]):
    """Instantiate a MemSearch with the configured embedding backend.

    Sets env vars in the *current* process so the async OpenAI / Ollama
    clients instantiated inside memsearch pick up the operator-configured
    base URL / API key. For watcher subprocesses we pass ``env=`` instead
    — see :func:`start_watcher`.
    """
    # Apply env vars in-process for reindex / search codepaths.
    os.environ.update(_resolved_env(config))
    from memsearch import MemSearch  # type: ignore

    return MemSearch(**_memsearch_kwargs(config, paths=paths))


async def _index_async(config: Config, paths: list[Path], *, force: bool = False) -> Any:
    mem = _build_memsearch(config, paths=paths)
    try:
        return await mem.index(force=force)
    except TypeError:
        return await mem.index()


async def _search_async(
    config: Config, paths: list[Path], query: str, *, top_k: int
) -> Any:
    mem = _build_memsearch(config, paths=paths)
    return await mem.search(query, top_k=top_k)


def reindex(config: Config, *, force: bool = False) -> dict[str, Any]:
    ready, error = _require_ready(config)
    if not ready:
        return error or {"ok": False}
    paths = source_paths(config)
    try:
        result = asyncio.run(_index_async(config, paths, force=force))
    except Exception as exc:  # noqa: BLE001 — surface a clean API error
        return {
            "ok": False,
            "error": "vector_backend_error",
            "detail": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": True,
        "indexed": True,
        "force": bool(force),
        "paths": [str(p) for p in paths],
        "result": result,
    }


def search(config: Config, *, query: str, top_k: int = 5) -> dict[str, Any]:
    ready, error = _require_ready(config)
    if not ready:
        return error or {"ok": False}
    clean = str(query or "").strip()
    if not clean:
        return {"ok": False, "error": "query_required"}
    import time as _time
    started = _time.time()
    try:
        rows = asyncio.run(
            _search_async(config, source_paths(config), clean, top_k=max(1, top_k))
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean API error
        return {
            "ok": False,
            "error": "vector_backend_error",
            "detail": f"{type(exc).__name__}: {exc}",
        }
    latency_ms = int((_time.time() - started) * 1000)
    # Emit a search event into the activity log so the dashboard's
    # live-feed shows operator searches as they happen. Best-effort —
    # failures here never block the result.
    try:
        from .writer import MemoryWriter
        MemoryWriter(config=config).record_search(
            query=clean,
            result_count=len(rows or []),
            latency_ms=latency_ms,
            source="api:memsearch",
        )
    except Exception:
        pass
    return {
        "ok": True,
        "query": clean,
        "results": rows,
        "count": len(rows or []),
        "latency_ms": latency_ms,
    }


def start_watcher(config: Config) -> dict[str, Any]:
    global _WATCHER_PROCESS
    ready, error = _require_ready(config)
    if not ready:
        return error or {"ok": False}
    if _WATCHER_PROCESS and _WATCHER_PROCESS.poll() is None:
        return {**status(config), "started": False, "detail": "already_running"}
    paths = [str(p) for p in source_paths(config) if p.exists()]
    if not paths:
        return {"ok": False, "error": "no_existing_source_paths"}
    log_path = config.paths.dev_log("memsearch_watch")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = _memsearch_kwargs(config, paths=source_paths(config))
    # Re-serialise to drop the Path objects we stripped in _memsearch_kwargs
    # — ``paths`` above is already stringified.
    code = (
        "import asyncio, json\n"
        "from memsearch import MemSearch\n"
        f"kwargs = {json.dumps(kwargs)}\n"
        "asyncio.run(MemSearch(**kwargs).watch())\n"
    )
    log = log_path.open("ab")
    _WATCHER_PROCESS = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(config.paths.root),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=_resolved_env(config),
    )
    configure(config, watch_enabled=True)
    return {
        **status(config),
        "started": True,
        "pid": _WATCHER_PROCESS.pid,
        "log_path": str(log_path),
    }


def stop_watcher(config: Config) -> dict[str, Any]:
    global _WATCHER_PROCESS
    stopped = False
    if _WATCHER_PROCESS and _WATCHER_PROCESS.poll() is None:
        _WATCHER_PROCESS.terminate()
        stopped = True
    _WATCHER_PROCESS = None
    configure(config, watch_enabled=False)
    return {**status(config), "stopped": stopped}
