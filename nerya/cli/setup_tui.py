"""Rich-powered TUI for ``nerya setup``.

Walks the operator through the same domains the dashboard's
``/setup`` wizard does — password, LLM model, gateway, memory, browser,
account, search — using only ``rich`` (already a runtime dep). No new
external packages, no curses, no Textual.

Design rules
------------

1. **Every domain except LLM has a safe default.** A user that just
   wants to "next, next, finish" gets the bare-minimum-working install
   with the LLM provider as the only required step.
2. **No HTTP server required.** We talk to the in-process
   :class:`nerya.sdk.InternalClient` and the matching ``ops`` modules
   (``nerya.api.auth``, ``nerya.llm.ops``, …) so the service does not
   need to be running for ``nerya setup --tui`` to work.
3. **Graceful non-TTY fallback.** If stdin/stdout is not a TTY we still
   render a summary and apply the requested defaults — useful for
   ``nerya setup --tui --yes`` in containers / CI smoke tests.
4. **Idempotent re-runs.** Every step shows the *current* value first
   and treats "press Enter to keep" as the no-op path.

The module exposes a single public entry point — :func:`run` — invoked
by :mod:`nerya.cli.commands.setup`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from ..sdk import InternalClient


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


@dataclass
class StepOutcome:
    """One row in the final summary table."""

    name: str
    status: str  # "ok" | "skipped" | "warn" | "error"
    detail: str = ""


@dataclass
class WizardResult:
    """Cumulative result of every step the operator walked through."""

    steps: list[StepOutcome] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.steps.append(StepOutcome(name=name, status=status, detail=detail))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run(
    client: InternalClient,
    *,
    console: Console | None = None,
    accept_defaults: bool = False,
    quick: bool = False,
) -> WizardResult:
    """Run the setup wizard against ``client``.

    ``accept_defaults=True`` is the non-interactive escape hatch:
    every prompt resolves to its default answer, every confirm
    resolves to "No, skip this step". The wizard still prints a
    full summary and persists the defaults that don't require user
    input (currently: none — all defaults are no-ops).

    ``quick=True`` cuts the wizard down to the only required step
    (LLM provider + model). Every other domain stays at its safe
    default and is recorded as "skipped" in the summary. This is the
    80% path for casual users — `nerya quickstart` and the bundled
    installers default to it.
    """

    console = console or Console()
    interactive = (
        not accept_defaults
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )

    _print_banner(console, quick=quick)

    result = WizardResult()
    ctx = _Ctx(client=client, console=console, interactive=interactive,
               accept_defaults=accept_defaults, result=result)

    if quick:
        # Single-question fast path. We still run the LLM step (the
        # only required step) end-to-end so the resulting workspace
        # is actually usable, and we record everything else as
        # "skipped — default" so the summary is honest.
        _step_llm(ctx)
        for name, detail in (
            ("Password", "skipped — local-only mode (default)"),
            ("Gateway", "skipped — off (default)"),
            ("Memory", "ok — builtin notebook (default)"),
            ("Browser", "skipped — off (default)"),
            ("Account", "ok — paper trading (default)"),
            ("Search", "skipped — off (default)"),
        ):
            ctx.result.add(name, "skipped" if detail.startswith("skipped") else "ok",
                           detail)
    else:
        # Order matters: password gates everything else for remote users; the
        # LLM model is the only "required" step; the rest are optional with
        # sensible defaults.
        _step_password(ctx)
        _step_llm(ctx)
        _step_gateway(ctx)
        _step_memory(ctx)
        _step_browser(ctx)
        _step_account(ctx)
        _step_search(ctx)

    _print_summary(console, result)
    _print_next_steps(console, client, quick=quick)
    return result


# ---------------------------------------------------------------------------
# Context + helpers
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    client: InternalClient
    console: Console
    interactive: bool
    accept_defaults: bool
    result: WizardResult


def _print_banner(console: Console, *, quick: bool = False) -> None:
    console.print()
    if quick:
        body = (
            "[bold cyan]Nerya quick setup[/bold cyan]\n"
            "One question only: pick an LLM provider + model. "
            "Everything else stays at safe defaults.\n"
            "Run [cyan]nerya setup[/cyan] (no flag) anytime for the "
            "full 7-step wizard."
        )
    else:
        body = (
            "[bold cyan]Nerya setup[/bold cyan]\n"
            "Configure your runtime. Press Enter at any prompt to keep the "
            "current / default value.\n"
            "Use Ctrl-C to abort — partially saved sections stay saved."
        )
    console.print(Panel.fit(body, border_style="cyan"))
    console.print()


def _print_summary(console: Console, result: WizardResult) -> None:
    table = Table(
        title="\nSetup summary",
        show_lines=False,
        header_style="bold",
        title_justify="left",
    )
    table.add_column("Step", style="cyan", no_wrap=True)
    table.add_column("Status")
    table.add_column("Detail", style="dim")

    tone = {
        "ok": "[green]ok[/green]",
        "skipped": "[yellow]skipped[/yellow]",
        "warn": "[yellow]warn[/yellow]",
        "error": "[red]error[/red]",
    }
    for row in result.steps:
        table.add_row(row.name, tone.get(row.status, row.status), row.detail)

    console.print(table)


def _print_next_steps(console: Console, client: InternalClient,
                      *, quick: bool = False) -> None:
    """Render the post-setup readiness + the "what now" command list."""

    try:
        from ..api import routes_operator as _ops

        # `_readiness_handler` expects ``(client, _query)`` and returns
        # the same envelope the HTTP endpoint serves.
        envelope = _ops._readiness_handler(client, {})  # type: ignore[attr-defined]
        data = (envelope or {}).get("data") or {}
        checks = data.get("checks") or []
        blocking = data.get("blocking") or []
        if checks:
            console.print()
            console.print("[bold]Readiness checks[/bold]")
            for chk in checks:
                tone = {
                    "ok": "[green]●[/green]",
                    "warn": "[yellow]●[/yellow]",
                    "blocked": "[red]●[/red]",
                }.get(chk.get("status"), "[dim]●[/dim]")
                console.print(f"  {tone} {chk.get('name')}: {chk.get('summary', '')}")
            if blocking:
                console.print(
                    f"\n[red]{len(blocking)} blocking item(s) remain. "
                    f"Re-run [bold]nerya setup[/bold] any time.[/red]"
                )
    except Exception:
        # Readiness is informational — don't fail the wizard if it raises.
        pass

    console.print()
    console.print("[bold]Next:[/bold]")
    console.print("  • Start everything:      [cyan]nerya quickstart[/cyan]")
    console.print("  • Or run server only:    [cyan]nerya serve[/cyan]")
    console.print("  • Open the dashboard:    [cyan]nerya dashboard[/cyan]")
    console.print("  • Run diagnostics:       [cyan]nerya doctor[/cyan]")
    if quick:
        console.print(
            "  • Tune defaults later:   [cyan]nerya setup[/cyan] "
            "(7-step wizard)"
        )
    console.print()


def _ask_yes(ctx: _Ctx, prompt: str, *, default: bool) -> bool:
    """``Confirm.ask`` with a non-interactive fallback to ``default``."""
    if not ctx.interactive:
        return default
    try:
        return Confirm.ask(prompt, default=default, console=ctx.console)
    except (EOFError, KeyboardInterrupt):
        raise


def _ask(
    ctx: _Ctx,
    prompt: str,
    *,
    default: str | None = None,
    choices: list[str] | None = None,
    password: bool = False,
) -> str:
    """``Prompt.ask`` with a non-interactive fallback to ``default``."""
    if not ctx.interactive:
        return default or ""
    try:
        return Prompt.ask(
            prompt,
            default=default,
            choices=choices,
            password=password,
            console=ctx.console,
            show_default=not password,
        )
    except (EOFError, KeyboardInterrupt):
        raise


def _safe(callable_: Callable[[], Any], *, on_error: str = "") -> Any:
    """Call ``callable_`` and squash any exception to ``on_error``.

    The wizard does best-effort discovery — if a sub-API is missing,
    misconfigured, or the workspace is too fresh to have the relevant
    YAML files yet, we skip the prefill instead of crashing.
    """
    try:
        return callable_()
    except Exception:
        return on_error


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _step_password(ctx: _Ctx) -> None:
    """Admin password — protects non-loopback dashboard access."""

    ctx.console.rule("[bold]1/7 · Admin password", style="cyan")

    from ..api import auth as auth_mod

    is_set = _safe(lambda: auth_mod.has_admin_password(ctx.client.config),
                   on_error=False)
    if is_set:
        ctx.console.print(
            "  Status: [green]configured[/green]. The dashboard "
            "requires login for non-loopback access."
        )
        if not _ask_yes(ctx, "  Rotate the password?", default=False):
            ctx.result.add("Password", "ok", "already configured")
            ctx.console.print()
            return
        current = _ask(ctx, "  Current password", password=True)
        if not auth_mod.verify_admin_password(ctx.client.config, current):
            ctx.console.print("  [red]Current password did not verify — skipping.[/red]")
            ctx.result.add("Password", "warn", "rotation aborted")
            ctx.console.print()
            return
    else:
        ctx.console.print(
            "  Status: [yellow]not set[/yellow]. The dashboard runs in "
            "[cyan]local-only[/cyan] mode (loopback only) until a "
            "password is configured."
        )
        if not _ask_yes(ctx, "  Set an admin password now?", default=True):
            ctx.result.add("Password", "skipped", "local-only mode")
            ctx.console.print()
            return

    while True:
        new_pw = _ask(ctx, "  New password (min 8 chars)", password=True)
        confirm = _ask(ctx, "  Confirm new password", password=True)
        if not new_pw:
            ctx.result.add("Password", "skipped", "no value entered")
            ctx.console.print()
            return
        if new_pw != confirm:
            ctx.console.print("  [red]Passwords do not match — try again.[/red]")
            continue
        try:
            auth_mod.set_admin_password(ctx.client.config, new_pw)
        except ValueError as exc:
            ctx.console.print(f"  [red]Rejected: {exc}[/red]")
            continue
        ctx.console.print("  [green]Password saved.[/green]")
        ctx.result.add("Password", "ok", "saved")
        ctx.console.print()
        return


def _step_llm(ctx: _Ctx) -> None:
    """LLM provider / tier assignment — the only blocking step."""

    ctx.console.rule("[bold]2/7 · LLM model", style="cyan")

    from ..llm import ops as llm_ops

    config = ctx.client.config
    readiness = _safe(lambda: llm_ops.provider_readiness(config),
                      on_error={"tiers": []})
    config_view = _safe(lambda: llm_ops.llm_config(config),
                        on_error={"tiers": []})

    tiers = config_view.get("tiers") or []
    ready_tiers = [t for t in tiers if (t.get("provider") and t.get("model"))]
    if ready_tiers:
        ctx.console.print("  Currently configured tiers:")
        for row in ready_tiers:
            ctx.console.print(
                f"    • [cyan]{row.get('tier')}[/cyan] → "
                f"{row.get('provider')}:{row.get('model')}"
            )
        if not _ask_yes(ctx, "  Adjust any tier now?", default=False):
            ctx.result.add("LLM model", "ok",
                           f"{len(ready_tiers)} tier(s) configured")
            ctx.console.print()
            return

    # Build the provider catalogue list (ids only).
    ready_providers: list[str] = []
    for row in (readiness or {}).get("tiers") or []:
        # `tiers` here actually carries the per-tier readiness summary
        # — providers ship inside .ready_providers
        pass
    provider_rows = (readiness or {}).get("providers") or []
    if not isinstance(provider_rows, list):
        provider_rows = []
    for row in provider_rows:
        pid = str(row.get("provider") or "").strip()
        if pid and row.get("ready"):
            ready_providers.append(pid)

    if not provider_rows:
        # Fallback to the static list bundled with the adapters package.
        try:
            from ..llm.adapters import builtin_providers
            provider_rows = [{"provider": p, "ready": False}
                             for p in builtin_providers()]
        except Exception:
            provider_rows = []

    # Pick a provider.
    all_provider_ids = [str(row.get("provider") or "") for row in provider_rows
                        if row.get("provider")]
    default_provider = (
        ready_providers[0] if ready_providers
        else (all_provider_ids[0] if all_provider_ids else "openai")
    )
    if not ctx.interactive:
        ctx.result.add("LLM model", "skipped", "non-interactive run")
        ctx.console.print(
            "  [yellow]Non-interactive — LLM tiers must be configured "
            "manually (see `nerya/cli` docs or the dashboard).[/yellow]"
        )
        ctx.console.print()
        return

    ctx.console.print("  Available providers:")
    for row in provider_rows[:20]:
        mark = "[green]✓[/green]" if row.get("ready") else "[dim]·[/dim]"
        ctx.console.print(f"    {mark} {row.get('provider')}")

    provider = _ask(
        ctx,
        "  Provider for the [cyan]medium[/cyan] tier",
        default=default_provider,
    ).strip().lower()
    if not provider:
        ctx.result.add("LLM model", "skipped", "no provider chosen")
        ctx.console.print()
        return

    # Pick a model. We don't ship an exhaustive list per-provider here —
    # the dashboard's catalog does that. Instead we accept free-form
    # input with a couple of sensible defaults baked in.
    default_model = _suggest_model(provider)
    model = _ask(ctx, "  Model", default=default_model).strip()
    if not model:
        ctx.result.add("LLM model", "skipped", "no model chosen")
        ctx.console.print()
        return

    # Optional API key — we never log it, and immediately stash it as a
    # vault reference via the existing `llm_config_set` plumbing.
    api_key = _ask(
        ctx,
        f"  API key for [cyan]{provider}[/cyan] (press Enter to skip)",
        default="",
        password=True,
    )

    providers_payload = []
    if api_key:
        providers_payload.append({
            "provider": provider,
            "provider_key": api_key,
        })

    try:
        llm_ops.llm_config_set(
            ctx.client.config,
            default_tier="medium",
            tiers=[{"tier": "medium", "provider": provider, "model": model}],
            providers=providers_payload or None,
        )
    except ValueError as exc:
        ctx.console.print(f"  [red]Rejected: {exc}[/red]")
        ctx.result.add("LLM model", "error", str(exc))
        ctx.console.print()
        return

    ctx.console.print(
        f"  [green]Saved:[/green] medium tier → {provider}:{model}"
        + (" (key stored in vault)" if api_key else "")
    )
    ctx.result.add("LLM model", "ok", f"medium → {provider}:{model}")
    ctx.console.print()


def _suggest_model(provider: str) -> str:
    """Best-effort default model for the most common providers."""
    suggestions = {
        "openai": "gpt-4o-mini",
        "anthropic": "claude-3-5-haiku-latest",
        "gemini": "gemini-2.5-flash",
        "deepseek": "deepseek-chat",
        "moonshot": "kimi-k2-instruct",
        "xai": "grok-2-mini",
        "openrouter": "anthropic/claude-3.5-haiku",
        "ollama": "qwen2.5:7b",
    }
    return suggestions.get(provider, "")


def _step_gateway(ctx: _Ctx) -> None:
    """Messaging gateway — default off.

    Today's interactive path supports **Telegram** end-to-end (bot token
    + chat ID); other platforms (Discord, Slack, Lark, WhatsApp, …)
    have more involved OAuth / webhook ceremonies that are friendlier
    in the web wizard, so the TUI offers a one-click deep-link to the
    web step for those.
    """

    ctx.console.rule("[bold]3/7 · Messaging gateway", style="cyan")
    ctx.console.print(
        "  Gateways relay agent output to Telegram, Discord, Slack, "
        "Lark, etc. Default: [cyan]off[/cyan]."
    )

    if not _ask_yes(ctx, "  Configure a gateway now?", default=False):
        ctx.result.add("Gateway", "skipped", "default: off")
        ctx.console.print()
        return

    platform = _ask(
        ctx,
        "  Platform",
        default="telegram",
        choices=["telegram", "other"],
    )

    if platform != "telegram":
        ctx.console.print(
            "  Non-Telegram platforms need OAuth / webhook setup — the "
            "web wizard at [cyan]/setup[/cyan] (Gateway step) is faster.\n"
            "  Run [cyan]nerya setup --web[/cyan] to switch."
        )
        ctx.result.add("Gateway", "warn",
                       f"{platform} deferred to web")
        ctx.console.print()
        return

    bot_token = _ask(
        ctx,
        "  Telegram bot token (from @BotFather)",
        default="",
        password=True,
    )
    if not bot_token:
        ctx.console.print(
            "  [yellow]No bot token entered — skipping.[/yellow]\n"
            "  Get one from @BotFather on Telegram, then re-run "
            "[cyan]nerya setup --tui[/cyan]."
        )
        ctx.result.add("Gateway", "skipped", "no bot token")
        ctx.console.print()
        return

    chat_id = _ask(
        ctx,
        "  Chat ID (numeric, from @userinfobot — leave blank to accept "
        "any chat)",
        default="",
    )

    try:
        from ..api import routes_gateway as gw

        payload: dict[str, Any] = {
            "channel": "telegram",
            "platform": "telegram",
            "enabled": True,
            "mode": "polling",
            "polling": True,
            "secrets": {"bot_token": bot_token},
        }
        if chat_id.strip():
            payload["chat_id"] = chat_id.strip()

        # Build a minimal "client" view that gateway_config_upsert needs
        # — it only reads ``client.config``.
        result = gw.gateway_config_upsert(ctx.client, payload)  # type: ignore[arg-type]
        if not result.get("ok", True):
            ctx.console.print(
                f"  [red]Save failed:[/red] {result.get('error')}"
            )
            ctx.result.add("Gateway", "error",
                           str(result.get("error") or "save failed"))
        else:
            ctx.console.print(
                "  [green]Saved:[/green] Telegram channel configured. "
                "The polling worker will attach on next "
                "[cyan]nerya serve[/cyan]."
            )
            ctx.result.add("Gateway", "ok", "telegram (polling)")
    except Exception as exc:  # pragma: no cover — defensive
        ctx.console.print(f"  [red]Save failed: {exc}[/red]")
        ctx.result.add("Gateway", "error", str(exc))
    ctx.console.print()


def _step_memory(ctx: _Ctx) -> None:
    """Memory backend — default ``builtin`` (notebook)."""

    ctx.console.rule("[bold]4/7 · Memory backend", style="cyan")

    current = _safe(
        lambda: ctx.client.config.get("memory.backend", "builtin"),
        on_error="builtin",
    )
    ctx.console.print(
        f"  Current backend: [cyan]{current}[/cyan]. "
        "Builtin keeps a curated AGENT.md + OPERATOR.md notebook with "
        "char-bounded entries."
    )

    if not _ask_yes(ctx, "  Change the memory backend?", default=False):
        ctx.result.add("Memory", "ok", f"{current} (default)")
        ctx.console.print()
        return

    backend = _ask(
        ctx,
        "  Backend",
        default=str(current),
        choices=["builtin", "memsearch", "agentmemory"],
    )

    try:
        _persist_yaml(ctx.client.config, {"memory": {"backend": backend}})
        ctx.console.print(f"  [green]Saved:[/green] memory.backend = {backend}")
        ctx.result.add("Memory", "ok", backend)
    except Exception as exc:  # pragma: no cover — defensive
        ctx.console.print(f"  [red]Save failed: {exc}[/red]")
        ctx.result.add("Memory", "error", str(exc))
    ctx.console.print()


def _step_browser(ctx: _Ctx) -> None:
    """Headless browser engine — default off."""

    ctx.console.rule("[bold]5/7 · Headless browser", style="cyan")
    ctx.console.print(
        "  The browser engine powers web-scraping skills "
        "(Playwright + Chromium). Default: [cyan]off[/cyan]."
    )

    if not _ask_yes(ctx, "  Enable the headless browser engine?", default=False):
        ctx.result.add("Browser", "skipped", "default: off")
        ctx.console.print()
        return

    ctx.console.print(
        "  Install instructions live at "
        "[cyan]/browsers[/cyan] in the dashboard.\n"
        "  Run [cyan]nerya doctor --only browsers[/cyan] to verify."
    )
    ctx.result.add("Browser", "warn", "manual install required")
    ctx.console.print()


def _step_account(ctx: _Ctx) -> None:
    """Trading account — default paper."""

    ctx.console.rule("[bold]6/7 · Trading account", style="cyan")
    ctx.console.print(
        "  Paper trading is on by default. Live trading requires both a "
        "configured account and [cyan]runtime.live_trading_enabled[/cyan] "
        "= true in nerya.yml (gated by the approval gate)."
    )

    if not _ask_yes(ctx, "  Stay on paper trading?", default=True):
        ctx.console.print(
            "  Configure live accounts at [cyan]/accounts[/cyan] in the "
            "dashboard — credentials use the vault."
        )
        ctx.result.add("Account", "warn", "live mode deferred to /accounts")
    else:
        ctx.result.add("Account", "ok", "paper (default)")
    ctx.console.print()


def _step_search(ctx: _Ctx) -> None:
    """Web-search engine — default off.

    Today's interactive path supports the seven engines exposed by
    ``/search/engines/config``: ``duckduckgo`` (keyless), ``searxng``
    (keyless, base-URL only), and the five keyed engines ``brave``,
    ``tavily``, ``serper``, ``exa``, ``perplexity``. The wizard
    accepts comma-separated keys (multi-key rotation, same shape as the
    dashboard's row form).
    """

    ctx.console.rule("[bold]7/7 · Web search", style="cyan")
    ctx.console.print(
        "  Web-search engines power the research skills. "
        "Default: [cyan]off[/cyan] — DuckDuckGo can be enabled "
        "without a key."
    )

    if not _ask_yes(ctx, "  Configure a search engine now?", default=False):
        ctx.result.add("Search", "skipped", "default: off")
        ctx.console.print()
        return

    engine = _ask(
        ctx,
        "  Engine",
        default="duckduckgo",
        choices=[
            "duckduckgo", "searxng", "brave", "tavily", "serper",
            "exa", "perplexity",
        ],
    )

    payload: dict[str, Any] = {"engines": [engine]}
    keyless = engine in {"duckduckgo"}
    needs_base_url = engine == "searxng"

    if needs_base_url:
        base_url = _ask(
            ctx,
            "  SearXNG base URL",
            default="http://127.0.0.1:8888",
        )
        payload["base_urls"] = {engine: base_url}

    if not keyless and not needs_base_url:
        keys = _ask(
            ctx,
            f"  API key(s) for {engine} (comma-separated for rotation)",
            default="",
            password=True,
        )
        if not keys.strip():
            ctx.console.print(
                f"  [yellow]No key entered — skipping {engine}.[/yellow]"
            )
            ctx.result.add("Search", "skipped", f"{engine}: no key")
            ctx.console.print()
            return
        payload["keys"] = {engine: keys}

    try:
        from ..api import routes_search

        # Call the route handler directly. ``routes_search.routes()``
        # returns a list of ``(method, path, handler)`` tuples; we pick
        # the POST /search/engines/config handler.
        for method, path, handler in routes_search.routes():
            if method == "POST" and path == "/search/engines/config":
                result = handler(ctx.client, payload)
                break
        else:  # pragma: no cover — defensive
            raise RuntimeError("search config route not found")

        # The handler returns a status snapshot, never an `ok=False`
        # for the happy path. Inspect engine status to confirm.
        engines_summary = (result or {}).get("engines") or []
        row = next((r for r in engines_summary if r.get("name") == engine), None)
        if row and row.get("ready"):
            ctx.console.print(
                f"  [green]Saved:[/green] {engine} enabled and ready."
            )
            ctx.result.add("Search", "ok", engine)
        else:
            ctx.console.print(
                f"  [yellow]Saved[/yellow] but {engine} is not marked "
                "ready — run [cyan]nerya doctor --only search[/cyan]."
            )
            ctx.result.add("Search", "warn", f"{engine} not ready")
    except Exception as exc:  # pragma: no cover
        ctx.console.print(f"  [red]Save failed: {exc}[/red]")
        ctx.result.add("Search", "error", str(exc))
    ctx.console.print()


# ---------------------------------------------------------------------------
# Persistence helper
# ---------------------------------------------------------------------------


def _persist_yaml(config: Any, patch: dict[str, Any]) -> None:
    """Merge ``patch`` into the workspace YAML config and save.

    Mirrors the small ``setdefault``/``update`` walk used by other ops
    modules — kept here so the TUI doesn't need to import every single
    domain's ops module just to flip a single boolean.
    """
    from ..core import yaml_io

    existing = yaml_io.load(config.paths.config, default={}) or {}
    if not isinstance(existing, dict):
        existing = {}

    def _deep_update(target: dict, src: dict) -> None:
        for key, value in src.items():
            if (
                isinstance(value, dict)
                and isinstance(target.get(key), dict)
            ):
                _deep_update(target[key], value)
            else:
                target[key] = value

    _deep_update(existing, patch)
    yaml_io.dump(config.paths.config, existing)


__all__ = ["run", "WizardResult", "StepOutcome"]
