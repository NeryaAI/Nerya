# Workspace plugins

Drop-in extension units for the Nerya runtime — see
`nerya/harness/loader.py` and `docs/extensibility-upgrade.md`.

Each subdirectory with a `plugin.py` is loaded at kernel boot:

- `plugin.py` must expose `PLUGIN = <Plugin instance>` (preferred) or a
  module-level `setup(ctx)` function.
- The plugin is activated as `user:<dirname>` — it can never shadow a
  builtin capability.
- `setup(ctx)` may contribute tools, LLM providers, team templates, and
  waterfall event listeners via `ctx.register_*` / `ctx.on_waterfall`.
  Every contribution returns a disposer; teardown is automatic.
- Failures are journaled to `journals/plugins.jsonl` and never abort
  boot.

Disable all plugins with `plugins.enabled: false` in `nerya.yml`, or a
single one via `plugins.disabled: [<dirname>]`.

`example_audit_log/` is a working reference plugin.
