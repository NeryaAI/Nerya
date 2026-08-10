"""FastMCP stdio wiring for :class:`NeryaTools`.

Loaded lazily so the rest of Nerya keeps working without the ``mcp`` package.

alongside the legacy hand-coded :class:`NeryaTools` registry,
the server can register a dynamically-generated tool surface built from
the live skill manifest registry (``DynamicMCPRegistry``). Both surfaces
coexist by default; operators can disable one or the other via the
``mcp.dynamic_tools.{enabled, include_legacy}`` config block.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .dynamic_tools import (
    DynamicMCPRegistry,
    MCPPolicy,
    MCPTool,
    policy_from_config,
)
from .registry_bridge import (
    NativeMCPPolicy,
    MCPNativeTool,
    build_native_mcp_registry,
    policy_from_config as native_policy_from_config,
)
from .tools import NeryaTools

_MCP_INSTALL_HINT = (
    "Nerya MCP server requires the 'mcp' package. Install with:\n"
    f"    {sys.executable} -m pip install 'mcp>=1.0'"
)


def _import_fastmcp():
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore

        return FastMCP
    except ImportError as e:  # pragma: no cover - optional dep
        raise ImportError(_MCP_INSTALL_HINT) from e


def create_server(
    tools: NeryaTools | None = None,
    *,
    workspace: str | Path | None = None,
    dynamic_policy: MCPPolicy | None = None,
    include_legacy: bool | None = None,
    include_dynamic: bool | None = None,
    include_native: bool | None = None,
    native_policy: NativeMCPPolicy | None = None,
):
    """Build a FastMCP instance with every Nerya tool registered.

    registers two layers:

    * **legacy** static :class:`NeryaTools` registry (back-compat, kept
      so existing MCP clients keep working);
    * **dynamic** tools generated from the live skill manifest
      registry, filtered by ``mcp.dynamic_tools`` policy or the
      ``dynamic_policy`` override.

    Both layers can be turned on/off independently via config or the
    function arguments. The tool callables return dicts; we wrap them to
    JSON strings so the MCP client sees a single ``text`` content block
    per call.
    """

    FastMCP = _import_fastmcp()
    tools = tools or NeryaTools.boot(workspace)

    cfg_data = (getattr(tools.client.config, "data", None) or {}) or {}
    mcp_cfg = (cfg_data.get("mcp") or {}) if isinstance(cfg_data, dict) else {}
    dyn_cfg = (mcp_cfg.get("dynamic_tools") or {}) if isinstance(mcp_cfg, dict) else {}

    if include_legacy is None:
        include_legacy = bool(mcp_cfg.get("include_legacy", True))
    if include_dynamic is None:
        include_dynamic = bool(dyn_cfg.get("enabled", True))
    native_cfg = (mcp_cfg.get("native_tools") or {}) if isinstance(mcp_cfg, dict) else {}
    if include_native is None:
        include_native = bool(native_cfg.get("enabled", True))

    instructions = (
        mcp_cfg.get("instructions") if isinstance(mcp_cfg.get("instructions"), str)
        else None
    ) or (
        "Nerya operator surface. Tools are generated from the live skill "
        "manifest registry; mutating actions are gated by the configured "
        "operator preset (see /runtime/operator_presets). Legacy "
        "trading-focused tools (``nerya_*``) remain available for "
        "back-compat."
    )

    mcp = FastMCP("nerya", instructions=instructions)

    if include_legacy:
        for entry in tools.registry():
            _register_tool(mcp, entry["name"], entry["description"], entry["fn"])

    if include_dynamic:
        registry = DynamicMCPRegistry.build(
            tools.client,
            policy=dynamic_policy or policy_from_config(tools.client.config),
        )
        for tool in registry.tools:
            _register_dynamic_tool(mcp, tool)
        try:
            setattr(mcp, "_nerya_dynamic_registry", registry)
        except Exception:
            pass

    if include_native:
        try:
            from ..tools import ToolRegistry
            from ..tools.native import build_native_tool_deps, register_native_tools
            native_registry = ToolRegistry()
            deps = build_native_tool_deps(
                workspace_root=Path(tools.client.config.paths.root),
                skill_roots=_default_skill_roots(tools),
                paths=tools.client.config.paths,
                config=tools.client.config,
                skills=tools.client.skills,
            )
            register_native_tools(native_registry, deps)
            native_view, native_executor = build_native_mcp_registry(
                registry=native_registry,
                config=tools.client.config,
                policy=native_policy or native_policy_from_config(
                    tools.client.config
                ),
            )
            # Native delegation handlers close over ``deps``. Keep the same
            # executor that backs the MCP bridge on that bundle so any child
            # native call re-enters the parent validation/permission pipeline.
            deps.executor = native_executor
            for tool in native_view.tools:
                _register_native_tool(mcp, tool)
            try:
                setattr(mcp, "_nerya_native_registry", native_view)
            except Exception:
                pass
        except Exception:
            # The native bridge must never block server boot.
            pass

    return mcp


def _default_skill_roots(tools: NeryaTools) -> list[Path]:
    roots: list[Path] = []
    try:
        installed = Path(tools.client.config.paths.skills_installed)
        if installed.exists():
            roots.append(installed)
    except Exception:
        pass
    try:
        from .. import skills as _skills_pkg
        builtin = Path(_skills_pkg.__file__).parent / "builtin"
        if builtin.exists():
            roots.append(builtin)
    except Exception:
        pass
    return roots


def build_dynamic_registry(
    tools: NeryaTools | None = None,
    *,
    workspace: str | Path | None = None,
    policy: MCPPolicy | None = None,
) -> DynamicMCPRegistry:
    """Compute the dynamic MCP registry without instantiating FastMCP.

    Useful for the ``/runtime/capability_matrix`` view and for tests that
    don't want the optional ``mcp`` package as a hard dependency.
    """

    nerya = tools or NeryaTools.boot(workspace)
    return DynamicMCPRegistry.build(
        nerya.client,
        policy=policy or policy_from_config(nerya.client.config),
    )


def _register_tool(mcp, name: str, description: str, fn) -> None:
    """Wrap a :class:`NeryaTools` method as an MCP tool.

    We preserve the callable's signature by deferring to ``fn(**kwargs)`` —
    FastMCP inspects the wrapper signature, so we explicitly propagate it.
    """
    import functools
    import inspect

    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def _tool(**kwargs: Any) -> str:
        result = fn(**kwargs)
        return json.dumps(result, default=str, ensure_ascii=False, indent=2)

    _tool.__signature__ = sig  # type: ignore[attr-defined]
    _tool.__name__ = name
    _tool.__doc__ = description or fn.__doc__ or ""
    mcp.tool(name=name, description=_tool.__doc__)(_tool)


def _register_native_tool(mcp, tool: MCPNativeTool) -> None:
    """Wrap a native :class:`MCPNativeTool` for FastMCP.

    Dispatch goes through :class:`NativeToolExecutor` so the executor's
    validate / permission / hook pipeline applies even when the call
    arrives over MCP. The schema is forwarded to the FastMCP layer so
    clients can render parameter forms.
    """
    import functools

    fn = tool.fn

    @functools.wraps(fn or (lambda **_: None))
    def _tool(**kwargs: Any) -> str:
        if fn is None:
            return json.dumps({"error": {
                "code": "no_handler",
                "message": f"native tool {tool.name} has no callable",
            }})
        result = fn(**kwargs)
        return json.dumps(result, default=str, ensure_ascii=False, indent=2)

    _tool.__name__ = tool.name
    _tool.__doc__ = tool.description or tool.tool_name
    try:
        mcp.tool(name=tool.name, description=_tool.__doc__)(_tool)
    except Exception:
        pass


def _register_dynamic_tool(mcp, tool: MCPTool) -> None:
    """Wrap a :class:`MCPTool` (manifest-driven) as a FastMCP tool.

    Dynamic tools accept arbitrary keyword payloads (`**kwargs`) — the
    real schema enforcement happens inside the SkillRuntime via
    ``validate_payload(input_schema)``, so we do not need FastMCP to
    introspect the schema. We do still pass the schema to FastMCP via
    the ``inputSchema`` annotation so MCP clients can render forms.
    """
    import functools

    fn = tool.fn

    @functools.wraps(fn or (lambda **_: None))
    def _tool(**kwargs: Any) -> str:
        if fn is None:
            return json.dumps({"error": {
                "code": "no_handler",
                "message": f"dynamic tool {tool.name} has no callable",
            }})
        result = fn(**kwargs)
        return json.dumps(result, default=str, ensure_ascii=False, indent=2)

    _tool.__name__ = tool.name
    _tool.__doc__ = tool.description or f"{tool.skill_id}.{tool.action}"
    try:
        mcp.tool(name=tool.name, description=_tool.__doc__)(_tool)
    except Exception:  # pragma: no cover - defensive: never break server boot
        pass


def serve(workspace: str | Path | None = None,
          *, verbose: bool = False) -> None:
    """Run the Nerya MCP server on stdio. Blocks until the client disconnects."""
    import logging

    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    tools = NeryaTools.boot(workspace)
    mcp = create_server(tools)
    mcp.run()
