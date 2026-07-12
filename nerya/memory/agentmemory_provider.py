"""Optional agentmemory external provider.

agentmemory is a JavaScript service/MCP package, so Nerya deliberately keeps
it out of Python runtime dependencies. Operators opt in by starting the
agentmemory server themselves and enabling this provider in ``nerya.yml``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..core import yaml_io
from ..core.config import Config
from .provider import (
    MemoryProvider,
    MemoryProviderInfo,
    MemoryRecallChunk,
    MemoryToolDef,
    MemoryToolResult,
)


__all__ = [
    "AgentMemoryProvider",
    "agentmemory_config",
    "agentmemory_install_instructions",
    "agentmemory_install_run",
    "configure_agentmemory",
    "external_memory_config",
    "selected_external_provider",
]


_DEFAULT_BASE_URL = "http://127.0.0.1:3111"
_DEFAULT_VIEWER_URL = "http://127.0.0.1:3113"
_DEFAULT_INSTALL_COMMAND = "npx @agentmemory/agentmemory"
_DEFAULT_MCP_COMMAND = "npx -y @agentmemory/mcp"


@dataclass(frozen=True)
class AgentMemorySettings:
    enabled: bool
    provider: str
    base_url: str
    secret_ref: str
    secret_env: str
    project: str
    session_id: str
    context_budget: int
    timeout_s: float
    install_command: str
    mcp_command: str
    viewer_url: str


_INFO = MemoryProviderInfo(
    id="agentmemory",
    name="agentmemory",
    family="external",
    description=(
        "Optional local agentmemory server integration over REST/MCP. "
        "Start it yourself with npx; Nerya does not install or bundle it."
    ),
    requires_api_key=False,
    env_key="AGENTMEMORY_SECRET",
    cost_hint="optional npm package; no Python dependency unless operator installs it",
    install_command=_DEFAULT_INSTALL_COMMAND,
    install_alternatives=(_DEFAULT_MCP_COMMAND,),
    docs_url="https://github.com/rohitg00/agentmemory",
)


def _external_raw(config: Config) -> dict[str, Any]:
    raw = config.get("memory.external", {}) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def agentmemory_config(config: Config) -> AgentMemorySettings:
    external = _external_raw(config)
    raw = external.get("agentmemory") or {}
    if not isinstance(raw, dict):
        raw = {}
    provider = str(external.get("provider") or "").strip().lower()
    try:
        budget = int(raw.get("context_budget") or 2000)
    except (TypeError, ValueError):
        budget = 2000
    try:
        timeout_s = float(raw.get("timeout_s") or 1.5)
    except (TypeError, ValueError):
        timeout_s = 1.5
    root_name = Path(config.paths.root).name
    base_url = str(raw.get("base_url") or _DEFAULT_BASE_URL).strip() or _DEFAULT_BASE_URL
    viewer_url = str(raw.get("viewer_url") or _DEFAULT_VIEWER_URL).strip() or _DEFAULT_VIEWER_URL
    return AgentMemorySettings(
        enabled=bool(external.get("enabled", False)),
        provider=provider,
        base_url=base_url.rstrip("/"),
        secret_ref=str(raw.get("secret_ref") or "").strip(),
        secret_env=str(raw.get("secret_env") or "AGENTMEMORY_SECRET").strip(),
        project=str(raw.get("project") or root_name).strip() or root_name,
        session_id=str(raw.get("session_id") or "").strip(),
        context_budget=max(1, budget),
        timeout_s=max(0.1, min(timeout_s, 30.0)),
        install_command=str(raw.get("install_command") or _DEFAULT_INSTALL_COMMAND),
        mcp_command=str(raw.get("mcp_command") or _DEFAULT_MCP_COMMAND),
        viewer_url=viewer_url,
    )


def selected_external_provider(config: Config) -> str:
    settings = agentmemory_config(config)
    if not settings.enabled:
        return ""
    return settings.provider


def external_memory_config(config: Config) -> dict[str, Any]:
    settings = agentmemory_config(config)
    return {
        "enabled": settings.enabled,
        "provider": settings.provider,
        "available_providers": ["agentmemory"],
        "agentmemory": {
            "base_url": settings.base_url,
            "secret_ref": settings.secret_ref,
            "secret_env": settings.secret_env,
            "project": settings.project,
            "session_id": settings.session_id,
            "context_budget": settings.context_budget,
            "timeout_s": settings.timeout_s,
            "install_command": settings.install_command,
            "mcp_command": settings.mcp_command,
            "viewer_url": settings.viewer_url,
            "docs_url": _INFO.docs_url,
        },
    }


def configure_agentmemory(
    config: Config,
    *,
    enabled: bool | None = None,
    provider: str | None = None,
    agentmemory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist external memory provider config without installing anything."""

    existing = yaml_io.load(config.paths.config, default={}) or {}
    if not isinstance(existing, dict):
        existing = {}
    memory = existing.setdefault("memory", {})
    if not isinstance(memory, dict):
        memory = {}
        existing["memory"] = memory
    external = memory.setdefault("external", {})
    if not isinstance(external, dict):
        external = {}
        memory["external"] = external
    current = external.setdefault("agentmemory", {})
    if not isinstance(current, dict):
        current = {}
        external["agentmemory"] = current

    if enabled is not None:
        external["enabled"] = bool(enabled)
        if enabled:
            vector = memory.setdefault("vector_search", {})
            if isinstance(vector, dict):
                vector["enabled"] = False
                vector["watch_enabled"] = False
    if provider is not None:
        normalized = str(provider or "").strip().lower()
        if normalized not in {"", "agentmemory"}:
            raise ValueError(f"unsupported memory provider {provider!r}")
        external["provider"] = normalized
    if isinstance(agentmemory, dict):
        allowed = {
            "base_url",
            "secret_ref",
            "secret_env",
            "project",
            "session_id",
            "context_budget",
            "timeout_s",
            "install_command",
            "mcp_command",
            "viewer_url",
        }
        for key, value in agentmemory.items():
            if key not in allowed or value is None:
                continue
            if key == "context_budget":
                try:
                    current[key] = max(1, int(value))
                except (TypeError, ValueError):
                    continue
            elif key == "timeout_s":
                try:
                    current[key] = max(0.1, min(float(value), 30.0))
                except (TypeError, ValueError):
                    continue
            else:
                current[key] = str(value).strip()

    external.setdefault("enabled", False)
    external.setdefault("provider", "")
    current.setdefault("base_url", _DEFAULT_BASE_URL)
    current.setdefault("secret_ref", "")
    current.setdefault("secret_env", "AGENTMEMORY_SECRET")
    current.setdefault("project", Path(config.paths.root).name)
    current.setdefault("session_id", "")
    current.setdefault("context_budget", 2000)
    current.setdefault("timeout_s", 1.5)
    current.setdefault("install_command", _DEFAULT_INSTALL_COMMAND)
    current.setdefault("mcp_command", _DEFAULT_MCP_COMMAND)
    current.setdefault("viewer_url", _DEFAULT_VIEWER_URL)

    yaml_io.dump(config.paths.config, existing)
    config.data.setdefault("memory", {})
    if not isinstance(config.data["memory"], dict):
        config.data["memory"] = {}
    config.data["memory"]["external"] = external
    if enabled:
        vector_cfg = memory.get("vector_search")
        if isinstance(vector_cfg, dict):
            config.data["memory"]["vector_search"] = vector_cfg
    return external_memory_config(config)


def agentmemory_install_instructions(config: Config) -> dict[str, Any]:
    settings = agentmemory_config(config)
    return {
        "ok": True,
        "manual": True,
        "provider": "agentmemory",
        "dependency_available": AgentMemoryProvider(config).is_available(),
        "commands": [
            settings.install_command,
            settings.mcp_command,
        ],
        "health_url": f"{settings.base_url}/agentmemory/health",
        "viewer_url": settings.viewer_url,
        "docs_url": _INFO.docs_url,
        "note": (
            "Nerya does not install agentmemory automatically. Start the "
            "server in a separate terminal, then enable memory.external.provider."
        ),
    }


def agentmemory_install_run(config: Config) -> dict[str, Any]:
    """Actually run `npm install -g <package>` for agentmemory.

    The default install_command is ``npx @agentmemory/agentmemory`` which
    fetches+caches the package on the fly; for a "real" install the
    operator typically wants the global npm bin so the next ``npx`` is
    instant. We mirror memsearch's ``install_dependency`` (pip install)
    by running the install through subprocess and returning stdout/err
    + the resulting reachability state. Failures surface to the
    dashboard via ``ok=False`` + ``stderr_tail``.

    Safety: we resolve the npm executable explicitly via ``shutil.which``
    so we never invoke an unknown shell; we run inside the workspace
    root; and we never echo secrets — the install command is the only
    argument.
    """

    settings = agentmemory_config(config)
    # Parse the configured install_command and replace `npx <pkg>` with
    # `npm install -g <pkg>` so the artifact lives on disk afterwards.
    raw = (settings.install_command or "").strip()
    parts = [p for p in raw.split() if p]
    if not parts:
        return {
            "ok": False,
            "manual": False,
            "error": "no_install_command",
            "detail": "memory.external.agentmemory.install_command is empty.",
        }
    pkg: str
    if parts[0] == "npx":
        # Drop leading flags like -y, --yes
        pkg_parts = [p for p in parts[1:] if not p.startswith("-")]
        pkg = pkg_parts[0] if pkg_parts else ""
    elif parts[0] == "npm":
        pkg_parts = [p for p in parts if not p.startswith("-") and p not in {"npm", "install", "i", "-g", "--global"}]
        pkg = pkg_parts[0] if pkg_parts else ""
    else:
        pkg = parts[-1]
    if not pkg:
        return {
            "ok": False,
            "manual": False,
            "error": "unparsable_install_command",
            "detail": f"Could not extract a package name from {raw!r}.",
        }
    npm = shutil.which("npm")
    if npm is None:
        return {
            "ok": False,
            "manual": False,
            "error": "npm_missing",
            "detail": "npm executable not found on PATH. Install Node.js first.",
        }
    cmd = [npm, "install", "-g", pkg]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(config.paths.root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "manual": False,
            "error": "install_timeout",
            "detail": "npm install exceeded 300s.",
            "cmd": cmd,
            "stdout_tail": (exc.stdout or "")[-4000:] if exc.stdout else "",
            "stderr_tail": (exc.stderr or "")[-4000:] if exc.stderr else "",
        }
    # Probe dependency_available after install — for agentmemory this
    # really means "is the local server running and reachable" since the
    # install is a pkg-only operation. The dashboard surfaces both
    # signals.
    available = AgentMemoryProvider(config).is_available()
    return {
        "ok": proc.returncode == 0,
        "manual": False,
        "cmd": cmd,
        "package": pkg,
        "returncode": proc.returncode,
        "stdout_tail": (proc.stdout or "")[-4000:],
        "stderr_tail": (proc.stderr or "")[-4000:],
        "dependency_available": available,
        "note": (
            "npm package installed globally. Start the agentmemory server "
            f"with `{settings.install_command}` in a separate terminal, "
            "then click Test recall."
        ),
    }


class AgentMemoryProvider(MemoryProvider):
    """REST adapter for a separately installed agentmemory server."""

    info = _INFO

    def __init__(self, config: Config) -> None:
        self.config = config
        self.settings = agentmemory_config(config)
        self._last_error = ""

    def is_available(self) -> bool:
        if not (self.settings.enabled and self.settings.provider == "agentmemory"):
            return False
        try:
            self._request("GET", "/agentmemory/health")
            self._last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            return False

    def initialize(self) -> None:
        if not self.is_available():
            raise RuntimeError(self._last_error or "agentmemory server unavailable")

    def system_prompt_block(self) -> str:
        session_id = self.settings.session_id or self._default_session_id()
        payload = {
            "sessionId": session_id,
            "project": self.settings.project,
            "budget": self.settings.context_budget,
        }
        try:
            result = self._request("POST", "/agentmemory/context", json=payload)
        except Exception:
            return ""
        context = str(result.get("context") or result.get("body") or "").strip()
        if not context:
            return ""
        return "External agentmemory recall:\n" + context

    def prefetch(self, query: str, *, limit: int = 5) -> list[MemoryRecallChunk]:
        if not query.strip():
            return []
        try:
            result = self._request(
                "POST",
                "/agentmemory/smart-search",
                json={
                    "query": query,
                    "limit": int(limit or 5),
                    "sessionId": self.settings.session_id,
                    "project": self.settings.project,
                },
            )
        except Exception:
            return []
        rows = self._extract_rows(result)
        chunks: list[MemoryRecallChunk] = []
        for row in rows[:limit]:
            # agentmemory v0.9.x ``smart-search`` returns ``mode:"compact"``
            # by default — rows look like ``{obsId, score, sessionId,
            # timestamp, title, type}``. Older 0.7.x responses used
            # ``content``/``text``/``summary``. Fall back through every
            # known shape so the provider keeps working across upstream
            # changes without callers having to know the wire format.
            text = str(
                row.get("content")
                or row.get("text")
                or row.get("summary")
                or row.get("context")
                or row.get("narrative")
                or row.get("title")
                or row.get("body")
                or "",
            ).strip()
            if not text:
                continue
            try:
                score = float(row.get("score") or row.get("relevance") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            chunks.append(
                MemoryRecallChunk(
                    text=text,
                    score=score,
                    source=str(
                        row.get("id")
                        or row.get("obsId")
                        or row.get("source")
                        or "agentmemory"
                    ),
                    metadata={
                        k: v for k, v in row.items() if k not in {"content", "text", "title"}
                    },
                )
            )
        return chunks

    def sync_turn(self, *, turn: dict[str, Any]) -> None:
        if not bool(self.config.get("memory.external.agentmemory.sync_turns", False)):
            return
        content = str(turn.get("content") or "").strip()
        if not content:
            return
        try:
            self._request(
                "POST",
                "/agentmemory/remember",
                json={
                    "content": content[:8000],
                    "type": "nerya_turn",
                    "concepts": ["nerya", "turn"],
                },
            )
        except Exception:
            return

    def get_tool_schemas(self) -> list[MemoryToolDef]:
        return [
            MemoryToolDef(
                name="agentmemory",
                description="Search or write to the optional external agentmemory store.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["search", "remember", "context"],
                        },
                        "query": {"type": "string"},
                        "content": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "default": 5},
                    },
                    "required": ["action"],
                },
            )
        ]

    def handle_tool_call(
        self, name: str, arguments: dict[str, Any]
    ) -> MemoryToolResult:
        if name != "agentmemory":
            return MemoryToolResult(ok=False, error=f"agentmemory: unknown tool {name!r}")
        action = str(arguments.get("action") or "").strip().lower()
        if action == "search":
            query = str(arguments.get("query") or "")
            chunks = self.prefetch(query, limit=int(arguments.get("limit") or 5))
            return MemoryToolResult(
                ok=True,
                content="\n\n".join(c.text for c in chunks),
                extra={"count": len(chunks), "query": query},
            )
        if action == "remember":
            content = str(arguments.get("content") or "").strip()
            if not content:
                return MemoryToolResult(ok=False, error="agentmemory: content required")
            try:
                out = self._request(
                    "POST",
                    "/agentmemory/remember",
                    json={"content": content, "type": "nerya_manual"},
                )
            except Exception as exc:  # noqa: BLE001
                return MemoryToolResult(ok=False, error=str(exc))
            return MemoryToolResult(ok=True, content="saved", extra=out)
        if action == "context":
            return MemoryToolResult(ok=True, content=self.system_prompt_block())
        return MemoryToolResult(ok=False, error=f"agentmemory: unknown action {action!r}")

    def _default_session_id(self) -> str:
        return f"nerya:{Path(self.config.paths.root).name}"

    def _headers(self) -> dict[str, str]:
        secret = self._resolve_secret()
        return {"Authorization": f"Bearer {secret}"} if secret else {}

    def _resolve_secret(self) -> str:
        ref = self.settings.secret_ref
        if ref.startswith("vault://"):
            try:
                from ..security.secrets import SecretVault

                vault = SecretVault.open(self.config.paths.vault_enc)
                return vault.resolve(ref.split("vault://", 1)[-1].strip())
            except Exception:
                return ""
        if ref:
            return ref
        if self.settings.secret_env:
            return os.environ.get(self.settings.secret_env, "")
        return ""

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.settings.base_url}{path}"
        timeout = httpx.Timeout(
            self.settings.timeout_s,
            connect=min(self.settings.timeout_s, 0.5),
        )
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            resp = client.request(
                method.upper(),
                url,
                headers=self._headers(),
                json=json,
                params=params,
            )
        resp.raise_for_status()
        if not resp.content:
            return {}
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}

    @staticmethod
    def _extract_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("results", "memories", "observations", "hits", "items", "data"):
            value = result.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        if isinstance(result.get("result"), dict):
            return AgentMemoryProvider._extract_rows(result["result"])
        return []
