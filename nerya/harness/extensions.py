"""Unified plugin skeleton for Nerya — one ABI for every extension surface.

Before this module, contributing a capability meant touching a different
hardcoded assembly point per domain: ``tools/native/bootstrap.py`` for
tools, ``llm/adapters.builtin_providers`` for providers,
``teams/templates.BUILTIN_TEMPLATES`` for team topologies,
``api/local_server._collect_routes`` for HTTP routes. Six extension
surfaces, six shapes, no third-party entry path.

:mod:`nerya.harness.extensions` borrows the composition lessons of
plugin-host agent harnesses (deepseek-harness / Cordis) and restates
them in ~300 lines of dependency-free Python:

* **One plugin protocol.** A plugin is anything with ``name``,
  ``requires`` (service keys it needs before activating) and
  ``setup(ctx)``. ``setup`` receives a :class:`PluginContext` and may
  return a teardown callable.
* **Registration is an effect.** Every contribution method returns a
  disposer that reverts exactly that contribution. Host teardown runs
  disposers in reverse activation order — nothing leaks past the
  plugin's lifetime.
* **Deferred, dependency-ordered activation.** :meth:`ExtensionHost.use`
  skips (and records) plugins whose ``requires`` are unsatisfied, so
  optional integrations degrade instead of crashing boot.
* **Fail-closed plugins, fail-open host.** A plugin that raises in
  ``setup`` is recorded in :attr:`ExtensionHost.errors` and skipped;
  the host — and the agent kernel embedding it — keeps running.

What this is deliberately *not*: a DI container, a fiber runtime, or a
scope-shadowing engine. Nerya's session-level capability variance is
already served by ``SubAgentExecutionPolicy`` and operator presets;
this skeleton only standardises *contribution and teardown*.

Wiring lives in :mod:`nerya.agent.kernel` (tool contributions + tool
pipeline bridges) and :mod:`nerya.harness.loader` (workspace plugin
files). See ``docs/extensibility-upgrade.md`` for the design brief.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Protocol, runtime_checkable

from .events import WaterfallBus

__all__ = [
    "ExtensionHost",
    "Plugin",
    "PluginContext",
    "PluginError",
]

log = logging.getLogger("nerya.harness.extensions")

Teardown = Callable[[], None]


@runtime_checkable
class Plugin(Protocol):
    """The one shape every Nerya extension unit must have.

    ``name`` should be namespaced (``builtin:``-style prefixes for
    shipped code, ``user:`` for workspace plugins loaded by
    :mod:`nerya.harness.loader`) so two sources can never collide.

    ``requires`` lists *service keys* (see
    :meth:`ExtensionHost.provide`) that must be present before the
    plugin activates. Missing requirements skip the plugin with a
    recorded error instead of crashing boot.

    ``setup`` may return a teardown callable; the host combines it
    with the disposers of every contribution made through ``ctx``.
    """

    name: str
    requires: tuple[str, ...]

    def setup(self, ctx: "PluginContext") -> Teardown | None: ...  # pragma: no cover


class PluginError(RuntimeError):
    """A skipped / failed plugin activation (never fatal to the host)."""


class _ToolContribution:
    __slots__ = ("descriptor", "source")

    def __init__(self, descriptor: Any, source: str) -> None:
        self.descriptor = descriptor
        self.source = source


class PluginContext:
    """Per-plugin contribution façade handed to ``Plugin.setup``.

    Every method returns a disposer; the plugin may return its own
    teardown from ``setup`` but does not have to track these — the
    host remembers them and reverts on teardown automatically.
    """

    def __init__(
        self,
        host: "ExtensionHost",
        plugin_name: str,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._host = host
        self.name = plugin_name
        self.config = dict(config or {})
        # Disposers of every contribution made through this context. The
        # host snapshots this list when setup() returns so teardown can
        # revert the plugin even if it returned no teardown of its own.
        self._disposers: list[Teardown] = []

    def _track(self, dispose: Teardown) -> Teardown:
        self._disposers.append(dispose)
        return dispose

    # ------------------------------------------------------ services

    def get_service(self, key: str) -> Any | None:
        """Fetch a host-provided service (``"config"``, ``"paths"``,
        ``"tools"`` after attach, …). ``None`` when absent."""

        return self._host.get_service(key)

    # ------------------------------------------------------ contributions

    def register_tool(self, descriptor: Any) -> Teardown:
        """Contribute a :class:`~nerya.tools.types.ToolDescriptor`.

        The tool lands on the kernel's ``ToolRegistry`` when the host
        attaches (before the first turn runs). Registering a name that
        already exists is recorded as an error and skipped — plugins
        never shadow native or MCP tools.
        """

        return self._track(self._host._contribute_tool(self.name, descriptor))

    def on_waterfall(
        self,
        event: str,
        listener: Callable[..., Any],
        *,
        prepend: bool = False,
    ) -> Teardown:
        """Subscribe to an interceptable pipeline event
        (``tools/pre-execute``, ``tools/post-execute``, …)."""

        return self._track(self._host.bus.on(event, listener, prepend=prepend))

    def on_event(self, event: str, listener: Callable[[dict[str, Any]], None]) -> Teardown:
        """Subscribe to an observation-only event (failures contained)."""

        return self._track(self._host.bus.on_event(event, listener))

    def register_llm_provider(
        self, provider_name: str, factory: Callable[[], Any]
    ) -> Teardown:
        """Contribute an LLM provider factory for ``ModelRouter``.

        Takes effect for gateways constructed after the contribution
        (the process-level custom-provider table is merged by
        ``llm.adapters.builtin_providers`` on each call).
        """

        return self._track(
            self._host._contribute_llm_provider(self.name, provider_name, factory)
        )

    def register_team_template(self, template: Any) -> Teardown:
        """Contribute a :class:`~nerya.teams.models.TeamTemplate`."""

        return self._track(self._host._contribute_team_template(self.name, template))


class ExtensionHost:
    """Owns plugin activation, contributions, and teardown for one kernel."""

    def __init__(
        self,
        *,
        services: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.bus = WaterfallBus()
        self._services: dict[str, Any] = dict(services or {})
        self._config = dict(config or {})
        self._lock = threading.RLock()

        # name -> activation record (insertion ordered)
        self._active: dict[str, dict[str, Any]] = {}
        # pending tool contributions consumed by attach_tools()
        self._pending_tools: list[_ToolContribution] = []
        self._tools_attached = False
        # capability tables surfaced to consumers
        self._llm_factories: dict[str, Callable[[], Any]] = {}
        self._team_templates: dict[str, Any] = {}
        self.errors: list[PluginError] = []

    # ------------------------------------------------------------ services

    def provide(self, key: str, value: Any) -> Teardown:
        """Publish a service key plugins can ``requires``/``get_service``."""

        with self._lock:
            self._services[key] = value

        def _dispose() -> None:
            with self._lock:
                if self._services.get(key) is value:
                    del self._services[key]

        return _dispose

    def get_service(self, key: str) -> Any | None:
        with self._lock:
            return self._services.get(key)

    # ------------------------------------------------------------ activation

    def use(self, plugin: Any, *, config: dict[str, Any] | None = None) -> bool:
        """Activate ``plugin`` now (dependencies must already be provided).

        Returns ``True`` when the plugin activated. A plugin with
        unmet ``requires``, a duplicate name, or a failing ``setup``
        is skipped with a recorded :class:`PluginError` — the host
        stays healthy.
        """

        name = getattr(plugin, "name", "") or ""
        if not isinstance(name, str) or not name:
            self._record(f"plugin {type(plugin).__name__} has no valid name")
            return False
        requires = tuple(getattr(plugin, "requires", ()) or ())

        with self._lock:
            if name in self._active:
                self._record(f"plugin {name!r} already active")
                return False

        missing = [key for key in requires if self.get_service(key) is None]
        if missing:
            self._record(
                f"plugin {name!r} skipped: missing services {', '.join(missing)}"
            )
            return False

        setup = getattr(plugin, "setup", None)
        if not callable(setup):
            self._record(f"plugin {name!r} has no callable setup(); skipped")
            return False

        ctx = PluginContext(self, name, config if config is not None else self._config)
        try:
            user_teardown = setup(ctx)
        except Exception as exc:  # fail-closed for the plugin, not the host
            self._record(f"plugin {name!r} setup failed: {exc}")
            # Roll back whatever the plugin contributed before raising.
            for dispose in reversed(ctx._disposers):
                try:
                    dispose()
                except Exception:
                    log.warning("rollback dispose failed", exc_info=True)
            self._rollback_contributions(name)
            return False
        if user_teardown is not None and not callable(user_teardown):
            user_teardown = None

        with self._lock:
            self._active[name] = {
                "plugin": plugin,
                "teardown": user_teardown,
                "contributions": list(ctx._disposers),
            }
        return True

    @property
    def active_plugins(self) -> list[str]:
        with self._lock:
            return list(self._active.keys())

    # ------------------------------------------------------------ tools

    def attach_tools(self, registry: Any) -> int:
        """Move pending tool contributions onto a live ``ToolRegistry``.

        Returns the number of tools registered. Name collisions with
        already-registered tools are recorded as errors and skipped so
        a plugin can never shadow a native/MCP tool. Idempotent per
        host (subsequent calls only flush contributions registered
        after the first attach).
        """

        count = 0
        with self._lock:
            pending, self._pending_tools = self._pending_tools, []
            self.provide("tools", registry)
            self._tools_attached = True
        for contribution in pending:
            descriptor = contribution.descriptor
            registered_name = getattr(descriptor, "name", "")
            try:
                try:
                    registry.get(registered_name)
                    already_registered = True
                except KeyError:  # ToolNotFoundError subclasses KeyError
                    already_registered = False
                if already_registered:
                    raise ValueError(
                        f"tool {registered_name!r} already registered"
                    )
                self._validate_tool_descriptor(descriptor)
                registry.register(descriptor)
            except Exception as exc:
                self._record(
                    f"plugin {contribution.source!r} tool contribution "
                    f"{registered_name!r} rejected: {exc}"
                )
                continue
            count += 1
        return count

    def _contribute_tool(self, source: str, descriptor: Any) -> Teardown:
        def _dispose() -> None:
            name = getattr(descriptor, "name", "")
            with self._lock:
                self._pending_tools = [
                    c
                    for c in self._pending_tools
                    if c.descriptor is not descriptor
                ]
            # After attach, the disposer must also unregister from the
            # live registry so teardown fully reverts the contribution.
            registry = self.get_service("tools")
            if registry is not None and name:
                try:
                    registry.unregister(name)
                except Exception:
                    pass

        with self._lock:
            self._pending_tools.append(_ToolContribution(descriptor, source))
        return _dispose

    @staticmethod
    def _validate_tool_descriptor(descriptor: Any) -> None:
        if not isinstance(getattr(descriptor, "name", None), str) or not descriptor.name:
            raise ValueError("descriptor.name must be a non-empty string")
        if not callable(getattr(descriptor, "handler", None)):
            raise ValueError("descriptor.handler must be callable")
        try:
            from ..tools.types import ToolDescriptor  # leaf module, no cycles

            if not isinstance(descriptor, ToolDescriptor):
                raise ValueError(
                    f"expected ToolDescriptor, got {type(descriptor).__name__}"
                )
        except ImportError:  # pragma: no cover - only in stripped installs
            pass

    # ------------------------------------------------------------ capability tables

    def llm_provider_factories(self) -> dict[str, Callable[[], Any]]:
        with self._lock:
            return dict(self._llm_factories)

    def team_templates(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._team_templates)

    def _contribute_llm_provider(
        self, source: str, provider_name: str, factory: Callable[[], Any]
    ) -> Teardown:
        if not isinstance(provider_name, str) or not provider_name:
            self._record(f"plugin {source!r} llm provider needs a name")
            return lambda: None
        if not callable(factory):
            self._record(f"plugin {source!r} llm provider factory not callable")
            return lambda: None

        def adapters_dispose() -> None:
            return None

        try:
            # Also land on the process-level table so builtin_providers()
            # picks the contribution up for every future gateway/router.
            from ..llm.adapters import register_custom_provider

            adapters_dispose = register_custom_provider(provider_name, factory)
        except Exception as exc:
            self._record(
                f"plugin {source!r} llm provider {provider_name!r} "
                f"rejected by adapter registry: {exc}"
            )
            return lambda: None

        def _dispose() -> None:
            adapters_dispose()
            with self._lock:
                self._llm_factories.pop(provider_name, None)

        with self._lock:
            self._llm_factories[provider_name] = factory
        return _dispose

    def _contribute_team_template(self, source: str, template: Any) -> Teardown:
        template_id = getattr(template, "id", None)
        if not isinstance(template_id, str) or not template_id:
            self._record(f"plugin {source!r} team template has no id")
            return lambda: None

        def _dispose() -> None:
            with self._lock:
                self._team_templates.pop(template_id, None)

        with self._lock:
            self._team_templates[template_id] = template
        return _dispose

    # ------------------------------------------------------------ tool pipeline bridges

    def tool_pre_hook(self) -> Callable[[Any, Any, Any], None]:
        """Bridge matching :class:`~nerya.tools.executor.NativeToolExecutor`
        pre-hooks onto the ``tools/pre-execute`` waterfall.

        Observation-only in this first revision: the permission engine
        stays the single decision authority for allow/deny. Listener
        failures are contained here — a plugin must never crash the
        executor chokepoint.
        """

        def _pre_hook(call: Any, descriptor: Any, decision: Any) -> None:
            try:
                payload = {
                    "tool": getattr(call, "name", ""),
                    "call_id": getattr(call, "id", ""),
                    "arguments": dict(getattr(call, "arguments", None) or {}),
                    "risk": getattr(getattr(descriptor, "risk", None), "value", ""),
                    "namespace": getattr(descriptor, "namespace", ""),
                    "permission_decision": getattr(
                        getattr(decision, "kind", None), "value", ""
                    ),
                }
                self.bus.waterfall("tools/pre-execute", payload)
            except Exception:
                log.warning("tools/pre-execute bridge failed", exc_info=True)

        return _pre_hook

    def tool_post_hook(self) -> Callable[[Any, Any], None]:
        """Bridge executor post-hooks onto ``tools/post-execute``.

        The payload carries the live ``ToolResult``; listeners may
        mutate it in place (redaction, spill-to-disk, breadcrumbs) or
        return a replacement payload whose ``result`` is copied back
        field-by-field. Bridge failures are contained.
        """

        def _post_hook(call: Any, result: Any) -> None:
            try:
                original = result
                payload = {
                    "tool": getattr(call, "name", ""),
                    "call_id": getattr(call, "id", ""),
                    "result": result,
                    "is_error": bool(getattr(result, "is_error", False)),
                }
                final = self.bus.waterfall("tools/post-execute", payload)
                if final is not payload and isinstance(final, dict):
                    replacement = final.get("result")
                    if replacement is not None and replacement is not original:
                        _copy_result_fields(original, replacement)
            except Exception:
                log.warning("tools/post-execute bridge failed", exc_info=True)

        return _post_hook

    # ------------------------------------------------------------ teardown

    def teardown(self) -> None:
        """Run plugin teardowns in reverse activation order.

        Contribution disposers run before the plugin's own teardown so
        the plugin can still observe a fully-wired world while tearing
        down. Errors are logged, never propagated.
        """

        with self._lock:
            records = list(self._active.values())
            self._active.clear()
        for record in reversed(records):
            for dispose in record["contributions"]:
                try:
                    dispose()
                except Exception:
                    log.warning("contribution dispose failed", exc_info=True)
            teardown = record["teardown"]
            if teardown is not None:
                try:
                    teardown()
                except Exception:
                    log.warning("plugin teardown failed", exc_info=True)

    # ------------------------------------------------------------ internals

    def _record(self, message: str) -> None:
        error = PluginError(message)
        self.errors.append(error)
        log.warning("%s", message)

    def _rollback_contributions(self, plugin_name: str) -> None:
        with self._lock:
            self._pending_tools = [
                c for c in self._pending_tools if c.source != plugin_name
            ]


def _copy_result_fields(target: Any, replacement: Any) -> None:
    """Copy dataclass fields from ``replacement`` onto ``target`` in place.

    ``ToolResult`` is a mutable dataclass and the executor hands the
    same instance to every post-hook, so a waterfall listener that
    produced a *new* result still needs its fields copied back onto
    the object the executor will return.
    """

    import dataclasses

    try:
        for f in dataclasses.fields(replacement):
            setattr(target, f.name, getattr(replacement, f.name))
    except Exception:
        log.warning("post-execute result replacement could not be copied back")
