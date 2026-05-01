"""Nerya MCP server — expose safe read-only + trigger.emit + proposal tools.

This package is intentionally decoupled from the `mcp` SDK: the real logic
lives in :class:`NeryaTools`, which is pure Python and can be unit-tested
directly. The thin FastMCP wiring in :mod:`nerya.mcp.server` is the only
piece that imports the third-party SDK, and it is imported lazily so tests
and offline workspaces keep working without the extra dependency.
"""

from .tools import NeryaTools, ToolError

__all__ = ["NeryaTools", "ToolError"]
