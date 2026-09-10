"""Harness — the composition and extension skeleton of Nerya.

Nerya's runtime is assembled from independent extension surfaces
(tools, LLM providers, connectors, skills, team templates, MCP). This
package owns the *skeleton* those surfaces plug into:

* :mod:`nerya.harness.events` — :class:`WaterfallBus`, the
  interceptable pipeline event bus (``tools/pre-execute``,
  ``tools/post-execute``, …).
* :mod:`nerya.harness.extensions` — the unified plugin ABI:
  :class:`Plugin` protocol, :class:`PluginContext` contribution
  façade, :class:`ExtensionHost` with dependency-ordered activation,
  registration-as-effect disposers, and fail-closed plugin isolation.
* :mod:`nerya.harness.loader` — workspace plugin discovery
  (``<workspace>/plugins/<id>/plugin.py``).
* :mod:`nerya.harness.cancellation` — cooperative cancel tokens and
  the steer inbox the kernel registers on every long-running turn.
* :mod:`nerya.harness.result_store` — the overflow spool that keeps
  large tool results out of the rolling LLM context.

Design brief and migration notes: ``docs/extensibility-upgrade.md``.
The legacy planner / output-parser / ``TurnHarness`` / ``ToolRunner``
stack that used to live here is gone — see :mod:`nerya.agent.loop`
for the canonical turn loop.
"""

from .cancellation import (
    CancelledError,
    CancelToken,
    SteerInbox,
    maybe,
    register_steer_inbox,
    register_token,
    signal_cancel,
    signal_steer,
    unregister_steer_inbox,
    unregister_token,
)
from .events import WaterfallBus
from .extensions import ExtensionHost, Plugin, PluginContext, PluginError
from .loader import ModulePlugin, PluginLoadError, discover_plugin_files, load_plugin

__all__ = [
    # events / extensions / loader
    "WaterfallBus",
    "ExtensionHost",
    "Plugin",
    "PluginContext",
    "PluginError",
    "ModulePlugin",
    "PluginLoadError",
    "discover_plugin_files",
    "load_plugin",
    # cancellation
    "CancelledError",
    "CancelToken",
    "SteerInbox",
    "maybe",
    "register_steer_inbox",
    "register_token",
    "signal_cancel",
    "signal_steer",
    "unregister_steer_inbox",
    "unregister_token",
]
