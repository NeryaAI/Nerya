"""Workspace plugin discovery — ``<workspace>/plugins/<id>/plugin.py``.

Operators extend Nerya by dropping a plugin directory next to the
other file-driven extension surfaces (``hooks/``, ``providers/``,
``subagents/``). Same trust model as those: whoever controls the
workspace root controls the runtime, so workspace plugins load
without a separate signature/permission layer — but they can never
shadow builtin capabilities (names are forced into the ``user:``
namespace and tool collisions are rejected at attach time).

Accepted plugin module shapes::

    # shape A — a Plugin instance (preferred)
    from nerya.harness import Plugin, PluginContext

    class MyPlugin(Plugin):
        name = "my_audit"
        requires = ()
        def setup(self, ctx: PluginContext): ...

    PLUGIN = MyPlugin()

    # shape B — a bare module-level ``setup(ctx)`` function
    def setup(ctx): ...

Both are wrapped as ``user:<id>`` where ``<id>`` is the directory
name, so two origins can never collide. Loading failures (syntax
errors, missing PLUGIN, wrong shape) are returned as
:class:`PluginLoadError` entries — the kernel activates the rest and
journals the failure instead of refusing to boot.

Discovery is pure (no imports), activation happens in the kernel via
:func:`build_host`, which keeps this module trivially testable.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "PluginLoadError",
    "ModulePlugin",
    "discover_plugin_files",
    "load_plugin",
]

log = logging.getLogger("nerya.harness.loader")

PLUGIN_FILENAME = "plugin.py"


@dataclass
class PluginLoadError:
    """A plugin directory that exists but could not be loaded."""

    plugin_id: str
    path: Path
    reason: str

    def asdict(self) -> dict[str, str]:
        return {
            "plugin": self.plugin_id,
            "path": str(self.path),
            "reason": self.reason,
        }


class ModulePlugin:
    """Adapter wrapping a bare ``setup(ctx)`` module as a Plugin."""

    def __init__(self, name: str, setup_fn: Callable[[Any], Any]) -> None:
        self.name = name
        self.requires: tuple[str, ...] = ()
        self._setup_fn = setup_fn

    def setup(self, ctx: Any) -> Any:
        return self._setup_fn(ctx)


def discover_plugin_files(plugins_dir: Path) -> list[tuple[str, Path]]:
    """Return ``(plugin_id, plugin.py path)`` pairs, sorted by id.

    Only directories containing ``plugin.py`` count; anything else in
    ``plugins/`` (READMEs, scratch files) is ignored.
    """

    if not plugins_dir.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir():
            continue
        candidate = entry / PLUGIN_FILENAME
        if candidate.is_file():
            found.append((entry.name, candidate))
    return found


def load_plugin(
    plugin_id: str,
    path: Path,
    *,
    namespace: str = "user:",
) -> tuple[Any | None, PluginLoadError | None]:
    """Import ``path`` and extract its plugin object.

    Returns ``(plugin, None)`` on success or ``(None, error)`` on
    failure. The plugin's canonical name always starts with
    ``namespace`` so builtin and workspace origins never collide.
    """

    if not path.is_file():
        return None, PluginLoadError(plugin_id, path, "plugin.py missing")

    module_name = f"nerya_user_plugin_{plugin_id}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError("could not create import spec")
        module = importlib.util.module_from_spec(spec)
        # Register before exec so dataclasses / typing in the plugin
        # module can resolve their own module by name. The module stays
        # cached for the process so repeated loads (tests, kernel
        # re-boots) don't materialise distinct classes per call.
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        return None, PluginLoadError(plugin_id, path, f"import failed: {exc}")

    canonical = f"{namespace}{plugin_id}"

    candidate = getattr(module, "PLUGIN", None)
    if candidate is not None:
        if not hasattr(candidate, "setup") or not callable(candidate.setup):
            return None, PluginLoadError(
                plugin_id, path, "PLUGIN must provide a callable setup(ctx)"
            )
        try:
            # Force the canonical namespaced name regardless of what the
            # author wrote, preventing shadowing of builtin plugin ids.
            candidate.name = canonical
        except AttributeError:
            return None, PluginLoadError(
                plugin_id, path, "PLUGIN.name is not writable (use a plain class)"
            )
        if not getattr(candidate, "requires", None):
            candidate.requires = ()
        return candidate, None

    setup_fn = getattr(module, "setup", None)
    if callable(setup_fn):
        return ModulePlugin(canonical, setup_fn), None

    return None, PluginLoadError(
        plugin_id, path, "module exposes neither PLUGIN nor setup(ctx)"
    )


def build_host(
    plugins_dir: Path,
    *,
    services: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    disabled: list[str] | None = None,
) -> tuple[Any, list[PluginLoadError]]:
    """Discover, load, and activate every workspace plugin onto a host.

    Returns ``(host, load_errors)``. Import/shape failures are reported
    in ``load_errors``; activation failures land in ``host.errors``.
    """

    from .extensions import ExtensionHost

    host = ExtensionHost(services=services, config=config)
    skip = {str(item) for item in (disabled or [])}
    load_errors: list[PluginLoadError] = []

    for plugin_id, path in discover_plugin_files(plugins_dir):
        if plugin_id in skip:
            continue
        plugin, error = load_plugin(plugin_id, path)
        if error is not None:
            load_errors.append(error)
            continue
        host.use(plugin)

    return host, load_errors
