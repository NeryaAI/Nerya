from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import routes_memory
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.memory import memsearch_index

pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def test_memsearch_vector_index_is_disabled_by_default(tmp_path):
    cfg = _config(tmp_path)

    out = memsearch_index.status(cfg)

    assert out["ok"] is True
    assert out["enabled"] is False
    assert out["backend"] == "memsearch"


def test_memsearch_search_reports_milvus_lite_gap_for_local_uri(tmp_path, monkeypatch):
    # memsearch importable but milvus_lite missing (the Windows situation):
    # a local file URI must yield a clean dependency_missing error instead
    # of a pymilvus stack trace.
    cfg = _config(tmp_path)
    cfg.data.setdefault("memory", {})["vector_search"] = {
        "enabled": True,
        "backend": "memsearch",
        "milvus": {"uri": "~/.memsearch/milvus.db"},
    }

    def fake_find_spec(name, *args, **kwargs):
        if name == "memsearch":
            return object()
        if name == "milvus_lite":
            return None
        raise AssertionError(f"unexpected probe: {name}")

    monkeypatch.setattr(memsearch_index.importlib.util, "find_spec", fake_find_spec)

    out = memsearch_index.search(cfg, query="anything")
    assert out["ok"] is False
    assert out["error"] == "dependency_missing"
    assert out["dependency_gap"] == "milvus_lite_not_installed"
    assert "remote Milvus" in out["detail"]

    status = memsearch_index.status(cfg)
    assert status["dependency_gap"] == "milvus_lite_not_installed"


def test_memsearch_remote_uri_does_not_require_milvus_lite(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.data.setdefault("memory", {})["vector_search"] = {
        "enabled": True,
        "backend": "memsearch",
        "milvus": {"uri": "http://milvus.internal:19530"},
    }

    def fake_find_spec(name, *args, **kwargs):
        if name == "memsearch":
            return object()
        if name == "milvus_lite":
            raise AssertionError("milvus_lite must not be probed for remote URIs")
        return None

    monkeypatch.setattr(memsearch_index.importlib.util, "find_spec", fake_find_spec)

    assert memsearch_index.runtime_dependency_gap(cfg) == ""


def test_memsearch_search_wraps_backend_exception(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.data.setdefault("memory", {})["vector_search"] = {
        "enabled": True,
        "backend": "memsearch",
        "milvus": {"uri": "http://milvus.internal:19530"},
    }
    monkeypatch.setattr(memsearch_index, "runtime_dependency_gap", lambda _cfg: "")

    async def boom(*_args, **_kwargs):
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(memsearch_index, "_search_async", boom)

    out = memsearch_index.search(cfg, query="anything")
    assert out["ok"] is False
    assert out["error"] == "vector_backend_error"
    assert "backend exploded" in out["detail"]


def test_memsearch_install_refuses_when_disabled(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("install should not run while disabled")

    monkeypatch.setattr(memsearch_index.subprocess, "run", fake_run)

    out = memsearch_index.install_dependency(cfg)

    assert out["ok"] is False
    assert out["error"] == "vector_search_disabled"
    assert called is False


def test_memory_vector_config_route_enables_without_installing(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    client = SimpleNamespace(config=cfg)
    route_map = {(method, path): handler for method, path, handler in routes_memory.routes()}
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("configure must not install dependencies")

    monkeypatch.setattr(memsearch_index.subprocess, "run", fake_run)

    out = route_map[("POST", "/memory/vector/config")](
        client,
        {"enabled": True, "paths": ["memory"]},
    )

    assert out["ok"] is True
    assert out["enabled"] is True
    assert cfg.get("memory.vector_search.enabled") is True
    assert called is False


def test_memory_vector_enable_disables_agentmemory(tmp_path):
    cfg = _config(tmp_path)
    cfg.data.setdefault("memory", {})["external"] = {
        "enabled": True,
        "provider": "agentmemory",
    }

    out = memsearch_index.configure(cfg, enabled=True)

    assert out["enabled"] is True
    assert cfg.get("memory.vector_search.enabled") is True
    assert cfg.get("memory.external.enabled") is False
    assert cfg.get("memory.external.provider") == ""


def test_memory_vector_config_persists_plaintext_key_in_vault(tmp_path, monkeypatch):
    """Pasted plaintext keys land in the SecretVault and the embedding
    config gets a ``vault://memory_embedding_<provider>`` ref written
    in their place. This is what backs the dashboard's "paste a fresh
    key" affordance for the embedding model."""

    cfg = _config(tmp_path)
    # SecretVault.open uses NERYA_VAULT_PASSPHRASE → set a deterministic
    # passphrase so re-opening the vault below works.
    monkeypatch.setenv("NERYA_VAULT_PASSPHRASE", "test-passphrase")

    out = memsearch_index.configure(
        cfg,
        embedding={
            "provider": "openai",
            "model": "text-embedding-3-small",
            "api_key_plain": "sk-plaintext-12345",
        },
    )

    assert out["ok"] is True
    ref = out["embedding"]["api_key_ref"]
    assert ref == "vault://memory_embedding_openai"
    # has_key is True because the vault resolves the new ref.
    assert out["embedding"]["has_key"] is True

    # And the plaintext value is *not* visible on the public surface.
    import json
    raw = (cfg.paths.vault_enc).read_bytes()
    assert b"sk-plaintext-12345" not in raw
    # The ciphertext envelope is JSON-shaped — sanity check.
    json.loads(raw)


def test_memory_vector_config_plaintext_falls_back_when_vault_missing(tmp_path):
    """If the SecretVault import fails (or fails to open), the
    plaintext key is silently dropped and we keep whatever
    ``api_key_ref`` was supplied. The router's downstream resolver
    is the ultimate authority on whether the key is usable, so this
    just guarantees we don't leak the secret into the YAML."""

    cfg = _config(tmp_path)

    out = memsearch_index.configure(
        cfg,
        embedding={
            "provider": "openai",
            "api_key_ref": "vault://existing-key",
            "api_key_plain": "sk-leaked-if-broken",
        },
    )

    assert out["ok"] is True
    # Either the vault wrote the new ref (preferred) or we kept the
    # operator's existing ref — either way the plaintext does not
    # appear in the configured api_key_ref.
    ref = out["embedding"]["api_key_ref"]
    assert ref.startswith("vault://")
    assert "sk-leaked-if-broken" not in ref


# ---------------------------------------------------------------------------
# Selected-backend Install + Test buttons
# ---------------------------------------------------------------------------
#
# These cover the new /memory/external/install/run and /memory/test routes
# wired up to support the "Install dependency" and "Test recall" buttons
# on the Selected backend settings card. The dashboard renders the raw
# response, so we exercise the route shape (not the UI) and stub real
# subprocess so the test stays hermetic.


def test_memory_test_returns_all_three_backends_when_disabled(tmp_path):
    """Smoke-only: probe defaults to "memory test", all three backends
    report status (builtin ok, memsearch/agentmemory disabled). This is
    the shape the dashboard renders inside the testResults panel."""

    cfg = _config(tmp_path)
    client = SimpleNamespace(config=cfg)
    route_map = {(method, path): handler for method, path, handler in routes_memory.routes()}

    out = route_map[("POST", "/memory/test")](client, {})

    assert out["ok"] is True
    assert out["query"] == "memory test"
    backends = {b["backend"]: b for b in out["backends"]}
    assert set(backends.keys()) == {"builtin", "memsearch", "agentmemory"}
    # builtin is always reachable; entries default to 0 in a clean tmp_path.
    assert backends["builtin"]["ok"] is True
    # memsearch is disabled by default.
    assert backends["memsearch"]["ok"] is False
    # agentmemory is not selected → enabled flag reports False.
    assert backends["agentmemory"]["ok"] is False
    assert backends["agentmemory"].get("enabled") is False


def test_memory_test_accepts_custom_query(tmp_path):
    cfg = _config(tmp_path)
    client = SimpleNamespace(config=cfg)
    route_map = {(method, path): handler for method, path, handler in routes_memory.routes()}

    out = route_map[("POST", "/memory/test")](client, {"query": "  bullish breakout  ", "limit": 2})

    assert out["ok"] is True
    assert out["query"] == "bullish breakout"


def test_external_install_run_reports_missing_npm(tmp_path, monkeypatch):
    """When npm is not on PATH the route surfaces a structured error so
    the dashboard can render a helpful "Install Node.js first" hint
    instead of a generic 500."""

    cfg = _config(tmp_path)
    client = SimpleNamespace(config=cfg)
    route_map = {(method, path): handler for method, path, handler in routes_memory.routes()}

    from nerya.memory import agentmemory_provider as ampkg

    monkeypatch.setattr(ampkg.shutil, "which", lambda _name: None)

    out = route_map[("POST", "/memory/external/install/run")](client, {})

    assert out["ok"] is False
    assert out["error"] == "npm_missing"
    assert "Node.js" in out["detail"]


def test_agentmemory_prefetch_handles_compact_smart_search(tmp_path, monkeypatch):
    """Regression: agentmemory v0.9.x smart-search returns
    ``{obsId, title, score, ...}`` by default (mode=compact). The
    provider previously only extracted text from
    ``content/text/summary/context/narrative`` and dropped every row.
    Lock in the title+obsId fallback so a real running agentmemory
    server actually returns recall hits through Nerya."""

    cfg = _config(tmp_path)
    cfg.data.setdefault("memory", {})["external"] = {
        "enabled": True,
        "provider": "agentmemory",
        "agentmemory": {"base_url": "http://127.0.0.1:3111"},
    }

    from nerya.memory.agentmemory_provider import AgentMemoryProvider

    provider = AgentMemoryProvider(cfg)

    def fake_request(method, path, *, json=None, params=None):  # noqa: ANN001
        assert method == "POST"
        assert path == "/agentmemory/smart-search"
        return {
            "mode": "compact",
            "results": [
                {
                    "obsId": "mem_abc",
                    "score": 0.97,
                    "sessionId": "memory",
                    "title": "Nerya backtests BTCUSDT momentum strategies",
                    "type": "decision",
                },
                {
                    "obsId": "mem_def",
                    "score": 0.42,
                    "sessionId": "memory",
                    "title": "Operator prefers concise Chinese replies",
                    "type": "decision",
                },
            ],
        }

    monkeypatch.setattr(provider, "_request", fake_request)

    chunks = provider.prefetch("nerya", limit=5)

    assert len(chunks) == 2
    assert chunks[0].text.startswith("Nerya backtests")
    assert chunks[0].score == pytest.approx(0.97)
    assert chunks[0].source == "mem_abc"
    assert chunks[1].source == "mem_def"


def test_external_install_run_invokes_subprocess_when_npm_present(tmp_path, monkeypatch):
    """Happy path: npm is on PATH → we invoke ``npm install -g <pkg>``
    and surface returncode + stdout/stderr tails to the dashboard."""

    cfg = _config(tmp_path)
    client = SimpleNamespace(config=cfg)
    route_map = {(method, path): handler for method, path, handler in routes_memory.routes()}

    from nerya.memory import agentmemory_provider as ampkg

    monkeypatch.setattr(ampkg.shutil, "which", lambda _name: "C:\\tools\\npm.CMD")

    captured: dict[str, object] = {}

    class _Result:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = "added 137 packages in 12s\n"
            self.stderr = ""

    def fake_run(cmd, **kwargs):  # noqa: ANN001 — kwargs are subprocess's
        captured["cmd"] = list(cmd)
        captured["cwd"] = kwargs.get("cwd")
        captured["timeout"] = kwargs.get("timeout")
        return _Result()

    monkeypatch.setattr(ampkg.subprocess, "run", fake_run)

    out = route_map[("POST", "/memory/external/install/run")](client, {})

    assert out["ok"] is True
    assert out["returncode"] == 0
    assert "added 137 packages" in (out["stdout_tail"] or "")
    # Mirror memsearch's install: invoked via the resolved executable,
    # not a shell — guards against shell-injection from install_command.
    assert captured["cmd"][0] == "C:\\tools\\npm.CMD"
    assert captured["cmd"][1:3] == ["install", "-g"]
    # Parsed package name should come from the default install_command
    # (``npx @agentmemory/agentmemory``).
    assert captured["cmd"][3] == "@agentmemory/agentmemory"
    # Bounded by a 5-minute timeout to keep the dashboard responsive.
    assert captured["timeout"] == 300
