"""Extension skeleton tests — waterfall bus, plugin host, loader, wiring.

Covers the seams introduced by the extensibility upgrade
(``docs/extensibility-upgrade.md``):

* :mod:`nerya.harness.events` — chain order, short-circuit, dispose.
* :mod:`nerya.harness.extensions` — deferred activation, fail-closed
  plugins, registration-as-effect teardown, tool pipeline bridges.
* :mod:`nerya.harness.loader` — workspace plugin shapes and errors.
* :mod:`nerya.llm.adapters` — custom provider contribution port.
* :mod:`nerya.core.config` — unknown-key warnings.
* :mod:`nerya.api.local_server` — route auto-discovery seam.
* kernel integration — a workspace plugin lands on the live registry.
"""

from __future__ import annotations

import logging
import types
from copy import deepcopy

import pytest

from nerya.agent.kernel import AgentKernel
from nerya.core.config import DEFAULT_CONFIG, Config, load_config
from nerya.core.paths import WorkspacePaths
from nerya.harness.events import WaterfallBus
from nerya.harness.extensions import ExtensionHost, PluginContext
from nerya.harness.loader import build_host, discover_plugin_files, load_plugin
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import (
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolDescriptor,
    ToolResult,
)

pytestmark = pytest.mark.smoke


# --------------------------------------------------------------- waterfall bus


def test_waterfall_chain_order_and_delegation() -> None:
    bus = WaterfallBus()
    seen: list[str] = []
    bus.on("t", lambda p, next: seen.append("a") or next())
    bus.on("t", lambda p, next: seen.append("b") or next())
    out = bus.waterfall("t", {"v": 1})
    assert seen == ["a", "b"]
    assert out == {"v": 1}


def test_waterfall_short_circuit_skips_rest() -> None:
    bus = WaterfallBus()
    seen: list[str] = []
    bus.on("t", lambda p, next: "first")  # no delegation
    bus.on("t", lambda p, next: seen.append("second") or next())
    assert bus.waterfall("t", "x") == "first"
    assert seen == []


def test_waterfall_prepend_and_dispose() -> None:
    bus = WaterfallBus()
    seen: list[str] = []
    dispose_first = bus.on("t", lambda p, next: seen.append("regular") or next())
    bus.on("t", lambda p, next: seen.append("vip") or next(), prepend=True)
    bus.waterfall("t", None)
    assert seen == ["vip", "regular"]
    dispose_first()
    seen.clear()
    bus.waterfall("t", None)
    assert seen == ["vip"]


def test_waterfall_fallback_runs_past_last_listener() -> None:
    bus = WaterfallBus()
    assert bus.waterfall("t", 1, fallback=lambda v: v + 10) == 11
    bus.on("t", lambda p, next: next())
    assert bus.waterfall("t", 1, fallback=lambda v: v + 10) == 11


def test_emit_contains_listener_errors() -> None:
    bus = WaterfallBus()
    received: list[dict] = []

    def bad(_payload: dict) -> None:
        raise RuntimeError("boom")

    dispose_bad = bus.on_event("obs", bad)
    dispose = bus.on_event("obs", received.append)
    bus.emit("obs", a=1)
    assert received == [{"a": 1}]  # bad listener did not break dispatch
    assert bus.has_listeners("obs")
    dispose()
    dispose_bad()
    assert not bus.has_listeners("obs")


# --------------------------------------------------------------- plugin host


class _Plugin:
    def __init__(self, name: str, requires=(), setup_fn=None):
        self.name = name
        self.requires = tuple(requires)
        self._setup_fn = setup_fn

    def setup(self, ctx: PluginContext):
        return self._setup_fn(ctx) if self._setup_fn else None


def _descriptor(name: str) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description="test tool",
        input_schema={"type": "object", "properties": {}},
        handler=lambda call: ToolResult.from_text(
            tool_use_id=call.id, name=name, text="ok"
        ),
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.NONE,
        read_only=True,
        is_concurrency_safe=True,
    )


def test_host_requires_missing_skips_plugin() -> None:
    host = ExtensionHost()
    assert not host.use(_Plugin("p1", requires=("nope",)))
    assert any("missing services" in str(e) for e in host.errors)
    assert host.active_plugins == []


def test_host_duplicate_name_rejected() -> None:
    host = ExtensionHost()
    assert host.use(_Plugin("p1"))
    assert not host.use(_Plugin("p1"))
    assert any("already active" in str(e) for e in host.errors)


def test_host_setup_failure_isolated_and_rolled_back() -> None:
    host = ExtensionHost()

    def setup(ctx: PluginContext):
        ctx.on_waterfall("t", lambda p, next: next())
        ctx.register_llm_provider("acme_broken", lambda: None)
        raise RuntimeError("plugin bug")

    assert not host.use(_Plugin("p1", setup_fn=setup))
    assert any("setup failed" in str(e) for e in host.errors)
    # Contributions made before the raise were rolled back.
    assert not host.bus.has_listeners("t")
    from nerya.llm.adapters import custom_providers

    assert "acme_broken" not in custom_providers()


def test_host_attach_tools_and_collision_rejected() -> None:
    host = ExtensionHost()
    registry = ToolRegistry()
    registry.register(_descriptor("native_tool"))

    def setup(ctx: PluginContext):
        ctx.register_tool(_descriptor("plugin_tool"))
        ctx.register_tool(_descriptor("native_tool"))  # collision

    host.use(_Plugin("p1", setup_fn=setup))
    assert host.attach_tools(registry) == 1
    assert registry.get("plugin_tool") is not None
    assert any("already registered" in str(e) for e in host.errors)


def test_host_attach_rejects_non_descriptor() -> None:
    host = ExtensionHost()
    registry = ToolRegistry()

    def setup(ctx: PluginContext):
        # Callable handler passes the duck check, but the object is not
        # a ToolDescriptor — the isinstance guard must catch it.
        ctx.register_tool(
            types.SimpleNamespace(name="x", handler=lambda call: None)
        )

    host.use(_Plugin("p1", setup_fn=setup))
    assert host.attach_tools(registry) == 0
    assert any("expected ToolDescriptor" in str(e) for e in host.errors)


def test_host_teardown_reverts_everything() -> None:
    host = ExtensionHost()
    registry = ToolRegistry()
    torn_down: list[str] = []

    def setup(ctx: PluginContext):
        ctx.register_tool(_descriptor("plugin_tool"))
        ctx.on_waterfall("t", lambda p, next: next())
        return lambda: torn_down.append("p1")

    host.use(_Plugin("p1", setup_fn=setup))
    host.attach_tools(registry)
    assert registry.get("plugin_tool") is not None

    host.teardown()
    assert torn_down == ["p1"]
    with pytest.raises(KeyError):
        registry.get("plugin_tool")
    assert not host.bus.has_listeners("t")
    assert host.active_plugins == []


# --------------------------------------------------------------- tool bridges


def test_tool_pre_hook_bridge_payload() -> None:
    host = ExtensionHost()
    payloads: list[dict] = []
    host.bus.on(
        "tools/pre-execute",
        lambda p, next: payloads.append(dict(p)) or next(),
    )
    decision = types.SimpleNamespace(kind=types.SimpleNamespace(value="allow"))
    descriptor = types.SimpleNamespace(risk=types.SimpleNamespace(value="read"),
                                       namespace="native")
    host.tool_pre_hook()(
        ToolCall(id="c1", name="read_file", arguments={"path": "x"}),
        descriptor,
        decision,
    )
    assert payloads == [{
        "tool": "read_file",
        "call_id": "c1",
        "arguments": {"path": "x"},
        "risk": "read",
        "namespace": "native",
        "permission_decision": "allow",
    }]


def test_tool_pre_hook_bridge_contains_listener_errors() -> None:
    host = ExtensionHost()
    host.bus.on("tools/pre-execute", lambda p, next: (_ for _ in ()).throw(RuntimeError("x")))
    # Must not raise.
    host.tool_pre_hook()(
        ToolCall(id="c1", name="read_file"),
        types.SimpleNamespace(risk=None, namespace=""),
        types.SimpleNamespace(kind=None),
    )


def test_tool_post_hook_bridge_mutation_and_replacement() -> None:
    host = ExtensionHost()

    def mutator(payload, next):
        payload["result"].metadata["touched"] = True
        return next()

    def replacer(payload, next):
        out = dict(payload)
        out["result"] = ToolResult.from_text(
            tool_use_id="c1", name="grep", text="REPLACED"
        )
        return out  # short-circuit past anyone else

    after_replacer: list[str] = []
    host.bus.on("tools/post-execute", mutator)
    bus_dispose = host.bus.on(
        "tools/post-execute",
        lambda p, next: after_replacer.append("ran") or next(),
        prepend=True,
    )
    # prepend order: replacer -> mutator; replacer short-circuits mutator.
    host.bus.on("tools/post-execute", replacer, prepend=True)
    bus_dispose()  # drop the observer so order is replacer -> mutator

    result = ToolResult.from_text(tool_use_id="c1", name="grep", text="original")
    host.tool_post_hook()(ToolCall(id="c1", name="grep"), result)

    # Replacement was copied back field-by-field onto the live result.
    assert result.text() == "REPLACED"
    assert "touched" not in result.metadata  # mutator never ran past replacer
    assert after_replacer == []


# --------------------------------------------------------------- loader

_SHAPE_A = '''
from nerya.harness import Plugin, PluginContext


class P(Plugin):
    name = "ignored"
    requires = ()

    def setup(self, ctx: PluginContext):
        return None


PLUGIN = P()
'''

_SHAPE_B = """
def setup(ctx):
    return None
"""

_BROKEN = "def oops("
_NO_SHAPE = "X = 1\n"


def test_loader_plugin_instance_shape_gets_user_namespace(tmp_path) -> None:
    plugin_dir = tmp_path / "alpha"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text(_SHAPE_A)
    assert [pid for pid, _ in discover_plugin_files(tmp_path)] == ["alpha"]
    plugin, error = load_plugin("alpha", plugin_dir / "plugin.py")
    assert error is None and plugin is not None
    assert plugin.name == "user:alpha"


def test_loader_module_setup_shape(tmp_path) -> None:
    plugin_dir = tmp_path / "beta"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text(_SHAPE_B)
    plugin, error = load_plugin("beta", plugin_dir / "plugin.py")
    assert error is None and plugin is not None
    assert plugin.name == "user:beta"
    assert callable(plugin.setup)


def test_loader_reports_broken_and_shapeless_plugins(tmp_path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "plugin.py").write_text(_BROKEN)
    _, error = load_plugin("broken", broken / "plugin.py")
    assert error is not None and "import failed" in error.reason

    shapeless = tmp_path / "shapeless"
    shapeless.mkdir()
    (shapeless / "plugin.py").write_text(_NO_SHAPE)
    _, error = load_plugin("shapeless", shapeless / "plugin.py")
    assert error is not None and "neither PLUGIN nor setup" in error.reason


def test_build_host_activates_good_and_reports_bad(tmp_path) -> None:
    good = tmp_path / "good"
    good.mkdir()
    (good / "plugin.py").write_text(_SHAPE_B)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "plugin.py").write_text(_BROKEN)
    (tmp_path / "README.md").write_text("ignored")

    host, errors = build_host(tmp_path)
    assert host.active_plugins == ["user:good"]
    assert [e.plugin_id for e in errors] == ["bad"]

    host2, _ = build_host(tmp_path, disabled=["good"])
    assert host2.active_plugins == []


# --------------------------------------------------------------- kernel wiring

_KERNEL_PLUGIN = '''
from nerya.harness import Plugin, PluginContext
from nerya.tools.types import (
    PermissionScope, RiskLevel, ToolDescriptor, ToolResult,
)

SCHEMA = {"type": "object", "properties": {"message": {"type": "string"}}}


class P(Plugin):
    name = "kernel_demo"
    requires = ("paths",)

    def setup(self, ctx: PluginContext):
        ctx.on_waterfall("tools/post-execute", lambda p, next: next())

        def handler(call):
            return ToolResult.from_text(
                tool_use_id=call.id,
                name="user_demo_tool",
                text="hello from plugin",
            )

        ctx.register_tool(ToolDescriptor(
            name="user_demo_tool",
            description="demo tool from a workspace plugin",
            input_schema=SCHEMA,
            handler=handler,
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NONE,
        ))
        return None


PLUGIN = P()
'''


def test_kernel_loads_workspace_plugin(tmp_path) -> None:
    plugin_dir = tmp_path / "plugins" / "kernel_demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(_KERNEL_PLUGIN)

    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]

    registry = kernel.tool_registry  # triggers _ensure_registry
    assert registry.get("user_demo_tool") is not None

    host = kernel.ext_host
    assert host is not None
    assert "user:kernel_demo" in host.active_plugins
    # The plugin's waterfall listener makes the kernel wire the bridge.
    assert host.bus.has_listeners("tools/post-execute")


def test_kernel_plugins_disabled_via_config(tmp_path, monkeypatch) -> None:
    plugin_dir = tmp_path / "plugins" / "kernel_demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(_KERNEL_PLUGIN)

    data = deepcopy(DEFAULT_CONFIG)
    data["plugins"]["enabled"] = False
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    registry = kernel.tool_registry
    with pytest.raises(KeyError):
        registry.get("user_demo_tool")
    assert kernel.ext_host is None


# --------------------------------------------------------------- llm providers


def test_register_custom_provider_flows_into_builtin_providers() -> None:
    from nerya.llm.adapters import (
        builtin_providers,
        custom_providers,
        register_custom_provider,
    )

    dispose = register_custom_provider("acme_test", lambda transport: "ACME")
    dispose_zero = register_custom_provider("zero_test", lambda: "ZERO")
    try:
        providers = builtin_providers()
        assert providers["acme_test"] == "ACME"
        assert providers["zero_test"] == "ZERO"  # zero-arg factory tolerated
        assert "acme_test" in custom_providers()
        with pytest.raises(ValueError):
            register_custom_provider("acme_test", lambda: "DUP")
        register_custom_provider("acme_test", lambda: "REPLACED", replace=True)
        assert builtin_providers()["acme_test"] == "REPLACED"
    finally:
        dispose()
        dispose_zero()
    assert "acme_test" not in builtin_providers()
    assert "zero_test" not in builtin_providers()


# --------------------------------------------------------------- config warnings


def test_config_warns_on_unknown_keys(tmp_path, caplog, monkeypatch) -> None:
    monkeypatch.delenv("NERYA_CONFIG_QUIET", raising=False)
    (tmp_path / "nerya.yml").write_text(
        "not_a_section: {x: 1}\nagent:\n  not_a_key: true\n"
    )
    with caplog.at_level(logging.WARNING, logger="nerya.core.config"):
        load_config(workspace=tmp_path)
    assert "unknown top-level key 'not_a_section'" in caplog.text
    assert "unknown agent.not_a_key key" in caplog.text


def test_config_quiet_env_silences_warnings(tmp_path, caplog, monkeypatch) -> None:
    monkeypatch.setenv("NERYA_CONFIG_QUIET", "1")
    (tmp_path / "nerya.yml").write_text("not_a_section: {x: 1}\n")
    with caplog.at_level(logging.WARNING, logger="nerya.core.config"):
        load_config(workspace=tmp_path)
    assert "unknown top-level key" not in caplog.text


# --------------------------------------------------------------- route discovery


def test_route_collect_accepts_extra_modules(monkeypatch) -> None:
    import nerya.api.local_server as ls

    def handler(client, params):  # pragma: no cover - registration only
        return {"ok": True}

    fake = types.SimpleNamespace(
        __name__="fake_routes_test",
        routes=lambda: [("GET", "/__plugin_test__", handler)],
    )
    monkeypatch.setattr(ls, "_ROUTES", [])
    ls._collect_routes(extra_modules=(fake,))
    assert any(
        path == "/__plugin_test__" for _, path, _ in ls._ROUTES
    )
    # Second collect is a no-op once routes exist.
    before = len(ls._ROUTES)
    ls._collect_routes()
    assert len(ls._ROUTES) == before
