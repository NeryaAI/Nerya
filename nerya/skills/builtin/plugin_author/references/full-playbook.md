# Plugin Author — Full Playbook

Complete reference for authoring workspace plugins that extend the
Nerya runtime via the `nerya.harness` extension skeleton. Read this
before drafting `plugin.py`. Design brief:
`agent/docs/extensibility-upgrade.md`.

## 1. When a plugin is (and is not) the right surface

| Need | Surface |
|---|---|
| Repeatable playbook the model follows (research steps, checklists) | `evolve_skill_proposal` (skill) |
| Prompt / config / policy tweak | `self_modify` channels |
| New native **tool** the model calls (data lookup, integration action) | plugin (`ctx.register_tool`) |
| Observe/transform **every tool call** (audit, redaction, spill) | plugin (`tools/*` waterfall) |
| New **LLM provider** not in the catalogue | plugin (`ctx.register_llm_provider`) |
| New **team topology** beyond builtin templates | plugin (`ctx.register_team_template`) — or a `teams/templates/*.yml` file when no code is needed |
| New exchange venue | `exchange_author` (providers lane), not a plugin |

Plugins are Python code loaded at kernel boot: choose them when the
capability must run *inside* the agent loop, not as a model-followed
playbook.

## 2. The Plugin ABI

```python
from nerya.harness import Plugin, PluginContext

class MyPlugin(Plugin):
    name = "my_plugin"        # loader forces user:<dirname> anyway
    requires = ("paths",)     # service keys: "config", "paths", "tools"

    def setup(self, ctx: PluginContext):
        ...contribute...
        return optional_teardown_callable   # may return None

PLUGIN = MyPlugin()
```

A bare module-level `def setup(ctx): ...` also works (no class).
Both shapes are wrapped as `user:<id>`; workspace plugins can never
shadow builtin capabilities.

Lifecycle contract:

- `setup()` runs once at kernel boot (or when the host attaches).
  Keep it fast; heavy work belongs in tool handlers or listeners.
- Every `ctx.*` contribution returns a **disposer**; teardown runs
  contributions in reverse automatically. Returning your own teardown
  callable is optional.
- `setup()` raising → the plugin is skipped and recorded in
  `journals/plugins.jsonl`; boot never fails.

## 3. Contribution API (`PluginContext`)

- `ctx.register_tool(descriptor)` — a
  `nerya.tools.types.ToolDescriptor`. Required fields: `name`,
  `description`, `input_schema` (JSON schema), `handler(call) ->
  ToolResult`. Set honest `risk` / `permission_scope` /
  `read_only` / `is_concurrency_safe` — the permission engine and the
  orchestrator trust them. A name colliding with an existing tool is
  rejected at attach (never shadows).
- `ctx.on_waterfall(event, listener, prepend=False)` — interceptable
  pipeline events:
  - `tools/pre-execute` — payload `{tool, call_id, arguments, risk,
    namespace, permission_decision}`; observation-only in this
    revision (deny stays with the permission engine).
  - `tools/post-execute` — payload carries the live `result`
    (`ToolResult`); mutate in place (redaction, metadata, spill) or
    return a replacement payload dict with a new `result` to swap it.
    **Always `next()` unless you are deliberately short-circuiting.**
- `ctx.on_event(event, listener)` — observation-only, failures contained.
- `ctx.register_llm_provider(name, factory)` — `factory(transport)`
  (zero-arg ok) returning a `ProviderCallable`; merged into every
  future `builtin_providers()` call, resolvable from
  `llm.tiers.<t>.provider`.
- `ctx.register_team_template(template)` — a
  `nerya.teams.models.TeamTemplate`; ids colliding with builtins are
  rejected.
- `ctx.get_service(key)` — `"config"` (Config), `"paths"`
  (WorkspacePaths), `"tools"` (ToolRegistry after attach).
- `ctx.config` — the plugin config dict (`plugins.*` from nerya.yml).

## 4. Minimal templates

Tool contribution:

```python
from nerya.harness import Plugin, PluginContext
from nerya.tools.types import (PermissionScope, RiskLevel,
                               ToolDescriptor, ToolResult)

SCHEMA = {"type": "object",
          "properties": {"query": {"type": "string"}},
          "required": ["query"]}

class T(Plugin):
    name = "x"
    requires = ()
    def setup(self, ctx: PluginContext):
        def handler(call):
            return ToolResult.from_text(
                tool_use_id=call.id, name="x_lookup",
                text=f"looked up {call.arguments['query']}",
                semantic_success=True)
        ctx.register_tool(ToolDescriptor(
            name="x_lookup", description="Look up X.",
            input_schema=SCHEMA, handler=handler,
            risk=RiskLevel.READ, permission_scope=PermissionScope.NETWORK,
            read_only=True, is_concurrency_safe=True))

PLUGIN = T()
```

Audit listener:

```python
class A(Plugin):
    name = "audit"
    requires = ("paths",)
    def setup(self, ctx: PluginContext):
        paths = ctx.get_service("paths")
        def audit(payload, next):
            append_line(paths.journal("my_audit"),
                        {"tool": payload["tool"],
                         "ok": not payload["is_error"]})
            return next()
        ctx.on_waterfall("tools/post-execute", audit)

PLUGIN = A()
```

(Use `nerya.core.jsonl.append` for journal writes in real code.)

Working reference: `workspace_template/plugins/example_audit_log/`.

## 5. Proposal flow (mandatory)

Executable code is never written live. Use the skill scripts:

1. `scripts/propose_plugin.py` — payload `{workspace, plugin_id,
   code, summary}`. AST-validates (syntax; module-level `PLUGIN` or
   `setup` exposure; no obvious `exec`/`eval`/network imports at
   module level), then stages `plugin_proposal` under
   `evolution/proposals/<pid>/after/plugins/<id>/plugin.py`, state
   `pending_review`, with test plan + rollback notes. Fails closed:
   invalid code → no proposal.
2. Operator approves → `apply_proposal` copies the file into
   `plugins/<id>/plugin.py`.
3. Next kernel boot loads it; check `journals/plugins.jsonl`
   (`plugins.boot` record lists active plugins, registered tools,
   load/activation errors).
4. `scripts/validate_plugin.py` — payload `{workspace, plugin_id}`:
   full load + activation diagnostic of an installed plugin (runs its
   `setup()` in this process — operator-triggered diagnostics only).

## 6. Disable / rollback / audit

- Disable one plugin: propose `plugins.disabled: [<id>]` in
  `nerya.yml` (core-config channel) or remove the directory via a
  proposal. Takes effect next boot.
- Disable all: `plugins.enabled: false`.
- Audit trail: `journals/plugins.jsonl` (boot records) and
  `journals/evolution.jsonl` (proposal apply), replayable via
  `nerya/observability/trace.py`.
- A plugin that misbehaves is contained: setup/listener/tool failures
  are journaled, never crash the kernel or a turn.

## 7. Validation checklist (attach to every proposal)

- `python -m pytest tests/test_harness_extensions.py` green.
- `validate_plugin` run reports `ok: true` after apply.
- Contributed tool appears in `tool_index` and honours permission
  mode (test in `read_only` preset).
- Waterfall listeners proven to `next()` (chain still reaches the
  audit journal).
- No secrets in code; no network at import time; `setup()` < ~1s.
