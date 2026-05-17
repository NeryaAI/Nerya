"""stdio MCP client transport.

Spawns an MCP server as a subprocess and exchanges JSON-RPC frames
over its stdin/stdout. This is the path for free local MCP servers
that ship as CLI commands (yfinance-mcp, fred-mcp-server, fmp-mcp,
finviz-free-mcp, mcp_polygon, …).

Implements :class:`nerya.mcp.session_adapter.MCPClient` so it can be
wrapped by :class:`MCPSessionAdapter` exactly the same as the HTTP
transport.

Authentication for stdio servers happens via environment variables —
the bootstrap resolves ``vault://`` refs once and passes them through
``env`` here. We never log env vars to the transcript.

Subprocess lifecycle:

* :meth:`__init__` records the command but does NOT spawn — keep the
  cost of "instantiate to inspect" zero;
* the first ``list_tools`` / ``list_resources`` / ``call_tool`` triggers
  :meth:`_ensure_started` which spawns the process, runs the MCP
  ``initialize`` handshake, and stores the negotiated protocol version;
* :meth:`reconnect` terminates the subprocess and clears the cache;
  the next call re-spawns;
* :meth:`close` is called by the bootstrap on shutdown for clean
  teardown (so the OS doesn't accumulate zombie children if Nerya is
  long-lived).

Failures spawning the process or talking JSON-RPC are wrapped as
:class:`StdioTransportError` so the adapter can map them to typed
:class:`ToolError`. Session-level expiry from the remote end (e.g.
the server crashes mid-call) raises :class:`MCPSessionExpiredError`
so the adapter's existing single-retry path kicks in.
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from ..session_adapter import MCPSessionExpiredError


_LOG = logging.getLogger(__name__)


class StdioTransportError(Exception):
    """Raised on transport-level failures (process spawn, JSON-RPC framing)."""


# ---------------------------------------------------------------------------
# JSON-RPC helpers (mirror http.py — kept local to avoid coupling)
# ---------------------------------------------------------------------------


def _next_id() -> str:
    return f"nerya-{uuid.uuid4().hex[:12]}"


def _build_rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": method,
        "params": params,
    }


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@dataclass
class StdioMCPClient:
    """One subprocess MCP server.

    Args:
        server_id: registry-facing id (used in ``mcp__<server_id>__<tool>``).
        command:   ``argv`` list to spawn (e.g. ``["uvx", "yahoo-finance-mcp"]``).
                   The first element is resolved against ``$PATH`` like any
                   normal subprocess — the bootstrap is responsible for
                   verifying ``uvx`` / ``npx`` exists if the operator config
                   relies on it.
        env:       Extra environment variables to set on the subprocess.
                   These layer ON TOP of the parent env so PATH stays usable;
                   pass secret values resolved from the vault here.
        cwd:       Working directory for the subprocess. Defaults to the
                   current process cwd.
        startup_timeout: Max seconds to wait for the MCP ``initialize``
                   response before giving up.
        read_timeout: Per-call max seconds to wait for the JSON-RPC reply.
                   stdio servers can hang on misbehaving inputs; this caps
                   the worst-case agent-loop stall.
    """

    server_id: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    startup_timeout: float = 30.0
    read_timeout: float = 60.0

    # Test injection point — replaces subprocess.Popen for the test
    # harness fake. Production callers leave it None.
    _spawn: Any = None

    # Process state — populated lazily by ``_ensure_started``.
    _proc: Optional[subprocess.Popen] = field(default=None, init=False, repr=False)
    _stdout_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False,
    )
    _send_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False,
    )
    _initialized: bool = field(default=False, init=False, repr=False)

    # ------------------------------------------------------------------
    # MCPClient Protocol
    # ------------------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        envelope = self._rpc("tools/list", {})
        result = envelope.get("result") or {}
        tools = result.get("tools") or []
        return list(tools) if isinstance(tools, list) else []

    def list_resources(self) -> list[dict[str, Any]]:
        try:
            envelope = self._rpc("resources/list", {})
        except StdioTransportError:
            return []
        result = envelope.get("result") or {}
        resources = result.get("resources") or []
        return list(resources) if isinstance(resources, list) else []

    def list_skills(self) -> list[dict[str, Any]]:
        try:
            envelope = self._rpc("skills/list", {})
        except StdioTransportError:
            return []
        result = envelope.get("result") or {}
        skills = result.get("skills") or []
        return list(skills) if isinstance(skills, list) else []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        envelope = self._rpc("tools/call", {"name": name, "arguments": arguments})
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise StdioTransportError(
                f"server {self.server_id!r} returned non-dict result for tools/call"
            )
        return result

    def reconnect(self) -> None:
        self.close()
        # Next call will re-spawn via _ensure_started.

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Terminate the subprocess if running. Safe to call repeatedly."""

        proc = self._proc
        self._proc = None
        self._initialized = False
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5.0)
        except Exception:
            try:
                proc.kill()
            except Exception:  # pragma: no cover - best-effort
                pass

    def __enter__(self) -> "StdioMCPClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _spawn_proc(self) -> subprocess.Popen:
        if self._spawn is not None:
            return self._spawn(self.command, env=self.env, cwd=self.cwd)

        if not self.command:
            raise StdioTransportError(
                f"server {self.server_id!r}: command list is empty"
            )

        # Layer the operator-supplied env on top of the parent process env
        # so PATH/HOME/PWD stay usable. Vault-resolved secrets land via
        # ``self.env`` and never appear in the parent env.
        full_env = dict(os.environ)
        full_env.update(self.env or {})

        try:
            proc = subprocess.Popen(  # nosec - operator-controlled command
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=full_env,
                cwd=self.cwd,
                bufsize=0,  # unbuffered: we frame JSON-RPC ourselves
                close_fds=(sys.platform != "win32"),
            )
        except FileNotFoundError as exc:
            raise StdioTransportError(
                f"server {self.server_id!r}: command not found: {self.command[0]!r}"
            ) from exc
        except OSError as exc:
            raise StdioTransportError(
                f"server {self.server_id!r}: spawn failed: {exc}"
            ) from exc
        return proc

    def _ensure_started(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc

        if self._proc is not None:
            # Process exited unexpectedly — surface as session expiry.
            self._proc = None
            self._initialized = False

        proc = self._spawn_proc()
        self._proc = proc

        # MCP ``initialize`` handshake. Even servers that don't strictly
        # require it answer it; servers that DO require it will refuse all
        # tools/list calls until we send it.
        deadline = time.time() + self.startup_timeout
        init_payload = {
            "jsonrpc": "2.0",
            "id": _next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "Nerya", "version": "1.0"},
                "capabilities": {},
            },
        }
        try:
            self._send_frame(init_payload)
        except Exception as exc:
            self.close()
            raise StdioTransportError(
                f"server {self.server_id!r}: initialize send failed: {exc}"
            ) from exc

        try:
            envelope = self._read_frame(deadline)
        except StdioTransportError:
            self.close()
            raise

        if not isinstance(envelope, dict) or "result" not in envelope:
            err_msg = ""
            if isinstance(envelope, dict):
                err_msg = str(envelope.get("error") or envelope)
            self.close()
            raise StdioTransportError(
                f"server {self.server_id!r}: initialize handshake failed: {err_msg}"
            )

        # Some servers require a ``notifications/initialized`` follow-up;
        # send it unconditionally — servers that don't care will ignore.
        try:
            self._send_frame(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                }
            )
        except Exception:
            _LOG.debug(
                "stdio server %s: initialized notification dropped (non-fatal)",
                self.server_id,
            )

        self._initialized = True
        return proc

    def _send_frame(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise StdioTransportError(
                f"server {self.server_id!r}: stdin not available"
            )

        # MCP stdio framing = raw JSON line + ``\n``. We do NOT use the
        # Content-Length header form (that's the LSP / "stdio (HTTP-style)"
        # variant). MCP servers in the wild overwhelmingly use newline
        # framing; should we hit a Content-Length-only server we'll add
        # an alt frame mode in a follow-up.
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._send_lock:
            try:
                proc.stdin.write(line.encode("utf-8"))
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise MCPSessionExpiredError(
                    f"server {self.server_id!r}: stdin pipe broken: {exc}"
                ) from exc

    def _read_frame(self, deadline: float) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise StdioTransportError(
                f"server {self.server_id!r}: stdout not available"
            )

        # Single reader thread per call — MCP servers reply in order so we
        # don't need a multiplexer.
        with self._stdout_lock:
            while True:
                if time.time() > deadline:
                    raise StdioTransportError(
                        f"server {self.server_id!r}: timed out waiting for response"
                    )
                if proc.poll() is not None:
                    raise MCPSessionExpiredError(
                        f"server {self.server_id!r}: subprocess exited "
                        f"(rc={proc.returncode}) before responding"
                    )
                try:
                    line = proc.stdout.readline()
                except Exception as exc:  # pragma: no cover - rare
                    raise StdioTransportError(
                        f"server {self.server_id!r}: stdout read failed: {exc}"
                    ) from exc
                if not line:
                    # EOF — process exited.
                    raise MCPSessionExpiredError(
                        f"server {self.server_id!r}: subprocess closed stdout"
                    )
                stripped = line.strip()
                if not stripped:
                    continue  # blank line — keep reading
                try:
                    return json.loads(stripped.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Some servers print non-JSON warnings/banners on stdout
                    # before they're fully booted. Skip and keep reading.
                    _LOG.debug(
                        "stdio server %s: skipping non-JSON line: %r",
                        self.server_id, stripped[:120],
                    )
                    continue

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._ensure_started()
        rpc = _build_rpc(method, params)
        try:
            self._send_frame(rpc)
        except MCPSessionExpiredError:
            raise
        except Exception as exc:
            raise StdioTransportError(
                f"server {self.server_id!r} send failed for {method}: {exc}"
            ) from exc

        deadline = time.time() + self.read_timeout
        envelope = self._read_frame(deadline)

        if "error" in envelope and "result" not in envelope:
            err = envelope["error"]
            if isinstance(err, dict):
                code = err.get("code")
                msg = err.get("message") or "MCP error"
                if code == -32001 or "session" in str(msg).lower():
                    raise MCPSessionExpiredError(
                        f"server {self.server_id!r} reported session expiry: {msg}"
                    )
                raise StdioTransportError(
                    f"server {self.server_id!r} JSON-RPC error {code}: {msg}"
                )
        return envelope


__all__ = ["StdioMCPClient", "StdioTransportError"]
