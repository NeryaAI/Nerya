"""Example workspace plugin: tool-call audit log + a custom tool.

Drop-in demonstration of the Nerya plugin ABI (``nerya.harness``).
Copy this directory to ``<workspace>/plugins/example_audit_log/`` to
activate it; delete the directory (or list it under
``plugins.disabled`` in ``nerya.yml``) to remove it. Nothing else in
Nerya needs editing — that is the point.

What it shows
-------------
* ``ctx.on_waterfall("tools/post-execute", ...)`` — observe (and
  optionally transform) every tool result after execution.
* ``ctx.register_tool(...)`` — contribute a first-class native tool
  that lands on the same ``ToolRegistry`` as ``read_file`` /
  ``run_shell``, with full risk/permission metadata.
* registration-as-effect teardown — the returned disposers revert
  both contributions if the plugin is ever torn down.
"""

from __future__ import annotations

from typing import Any, Callable

from nerya.harness import Plugin, PluginContext
from nerya.tools.types import (
    PermissionScope,
    RiskLevel,
    ToolDescriptor,
    ToolResult,
)

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "description": "Text to echo back (max 500 chars are kept).",
        }
    },
    "required": ["message"],
}


class ExampleAuditLogPlugin(Plugin):
    name = "example_audit_log"
    requires = ("paths",)

    def setup(self, ctx: PluginContext) -> Callable[[], None] | None:
        paths = ctx.get_service("paths")

        # 1) Observe every completed tool call, journal it, never crash.
        def _audit(payload: dict[str, Any], next: Callable[[], Any]) -> Any:
            try:
                record = {
                    "tool": payload.get("tool", ""),
                    "call_id": payload.get("call_id", ""),
                    "is_error": bool(payload.get("is_error", False)),
                }
                journal = paths.journal("plugin_audit")
                journal.parent.mkdir(parents=True, exist_ok=True)
                with open(journal, "a", encoding="utf-8") as fh:
                    # Plain append; production plugins should use
                    # nerya.core.jsonl for atomicity.
                    import json

                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception:
                pass  # audit is best-effort by contract
            return next()  # always delegate — other listeners must run

        dispose_audit = ctx.on_waterfall("tools/post-execute", _audit)

        # 2) Contribute a native tool through the standard descriptor shape.
        def _echo(call: Any) -> ToolResult:
            message = str((call.arguments or {}).get("message", ""))
            return ToolResult.from_text(
                tool_use_id=call.id,
                name="user_echo",
                text=f"echo: {message[:500]}",
                semantic_success=True,
            )

        dispose_tool = ctx.register_tool(
            ToolDescriptor(
                name="user_echo",
                description=(
                    "Example plugin tool. Echoes a message back. "
                    "Demonstrates workspace plugin tool contributions."
                ),
                input_schema=_INPUT_SCHEMA,
                handler=_echo,
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.NONE,
                read_only=True,
                is_concurrency_safe=True,
                tags=("plugin", "example"),
            )
        )

        def _teardown() -> None:
            dispose_audit()
            dispose_tool()

        return _teardown


PLUGIN = ExampleAuditLogPlugin()
