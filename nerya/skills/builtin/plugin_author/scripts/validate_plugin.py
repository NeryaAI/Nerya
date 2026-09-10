"""Full load-and-activate diagnostic for an installed workspace plugin.

Unlike ``propose_plugin`` (which never executes the draft), this
script imports the plugin and runs its ``setup()`` on a scratch
:class:`~nerya.harness.extensions.ExtensionHost` to surface exactly
what boot would do: load errors, activation errors, and the tool
contributions that would land on the registry. Operator-triggered
diagnostics only — the same code runs at kernel boot anyway.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .....core.config import load_config
from .....skills.manifest import cli_main


def run(_ctx=None, **payload: Any) -> dict[str, Any]:
    from .....harness.extensions import ExtensionHost
    from .....harness.loader import load_plugin
    from .....tools.registry import ToolRegistry

    workspace = payload.pop("workspace", None)
    plugin_id = str(payload.pop("plugin_id", "") or "").strip()
    if not plugin_id:
        return {"ok": False, "errors": ["plugin_id is required"]}

    config = load_config(Path(workspace).expanduser() if workspace else None)
    paths = config.paths
    plugin_py = paths.plugins / plugin_id / "plugin.py"
    if not plugin_py.is_file():
        return {
            "ok": False,
            "errors": [f"no plugin at plugins/{plugin_id}/plugin.py"],
        }

    plugin, load_error = load_plugin(plugin_id, plugin_py)
    if load_error is not None or plugin is None:
        return {
            "ok": False,
            "errors": [load_error.reason if load_error else "load failed"],
        }

    host = ExtensionHost(services={"paths": paths, "config": config})
    activated = host.use(plugin)
    registry = ToolRegistry()
    tools_registered = host.attach_tools(registry) if activated else 0
    errors = [str(e) for e in host.errors]
    return {
        "ok": activated and not errors,
        "plugin": getattr(plugin, "name", plugin_id),
        "activated": activated,
        "tools_registered": tools_registered,
        "tool_names": sorted(
            d.name for d in registry.list_tools()
        ),
        "errors": errors,
        "waterfall_listeners": [
            name
            for name in ("tools/pre-execute", "tools/post-execute")
            if host.bus.has_listeners(name)
        ],
    }


if __name__ == "__main__":
    cli_main(run)
