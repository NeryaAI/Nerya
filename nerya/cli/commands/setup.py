"""``nerya setup`` — unified onboarding entry point.

After ``nerya init`` (or the one-line installer) we offer the operator
two equivalent ways to configure password, LLM model, gateway, memory,
browser, account, and search:

* ``--tui``        — Rich-powered terminal wizard, in-process via
  :class:`nerya.sdk.InternalClient`. No HTTP server required.
* ``--web``        — opens the dashboard's ``/setup`` route, which
  reuses the same Settings cards. Auto-launches the API + dashboard
  pair when ``--start-server`` is passed.
* ``--print-url``  — headless variant of ``--web``: prints the URL
  the wizard lives at and exits.
* ``--ask``        — interactive picker between TUI / Web (default
  when stdout is a TTY).
* ``--yes``        — non-interactive run of the TUI that accepts every
  default. Useful for containers and CI smoke tests.

The wizard never mutates state without prompting (in non-interactive
mode it only applies the empty-default no-ops). Saving each step is
the user's decision in interactive mode, and the no-op path otherwise.
"""

from __future__ import annotations

import os
import socket
import sys
import time
import webbrowser
from typing import Optional

from .._common import _add_ws, _client


_DEFAULT_DASHBOARD_PORT = 18380
_DEFAULT_API_PORT = 18317


def _port_is_open(host: str, port: int, *, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_port(host: str, port: int, *, timeout_s: float = 30.0,
                   label: str = "service") -> bool:
    deadline = time.monotonic() + timeout_s
    sys.stdout.write(f"[nerya] waiting for {label} on {host}:{port} ")
    sys.stdout.flush()
    while time.monotonic() < deadline:
        if _port_is_open(host, port):
            sys.stdout.write(" ready\n")
            sys.stdout.flush()
            return True
        sys.stdout.write(".")
        sys.stdout.flush()
        time.sleep(0.5)
    sys.stdout.write(" timed out\n")
    sys.stdout.flush()
    return False


def _resolve_dashboard_url(args) -> str:
    """Pick the dashboard URL the ``/setup`` route lives at.

    Priority:

    1. Explicit ``--url`` argument.
    2. ``NERYA_SETUP_URL`` env var.
    3. ``NERYA_DASHBOARD_URL`` env var (same env contract as
       ``nerya dashboard``) with ``/setup`` appended.
    4. ``http://localhost:<dashboard-port>/setup`` (default 18380).
    """
    explicit = getattr(args, "url", None)
    if explicit:
        return explicit

    env_setup = os.environ.get("NERYA_SETUP_URL")
    if env_setup:
        return env_setup

    base = os.environ.get("NERYA_DASHBOARD_URL")
    if base:
        return base.rstrip("/") + "/setup"

    port = getattr(args, "dashboard_port", None) or _DEFAULT_DASHBOARD_PORT
    return f"http://localhost:{port}/setup"


def _pick_mode_interactively() -> str:
    """Ask the operator whether to use the TUI or the web wizard.

    Returns ``"tui"`` or ``"web"``. If stdin is not a TTY we default
    to ``"tui"`` (it has the most graceful headless fallback).
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return "tui"

    try:
        from rich.console import Console
        from rich.prompt import Prompt

        console = Console()
        console.print()
        console.print(
            "[bold cyan]Nerya is installed — let's finish configuration.[/bold cyan]"
        )
        console.print(
            "  • [cyan]tui[/cyan]  — interactive wizard right here in the terminal"
        )
        console.print(
            "  • [cyan]web[/cyan]  — opens the dashboard's [bold]/setup[/bold] page in your browser"
        )
        choice = Prompt.ask(
            "How would you like to continue?",
            choices=["tui", "web"],
            default="tui",
            console=console,
        )
        return str(choice).strip().lower() or "tui"
    except Exception:
        # Rich failed — fall back to plain input.
        print()
        print("[nerya] Nerya is installed — let's finish configuration.")
        print("  tui = interactive terminal wizard (default)")
        print("  web = open the dashboard /setup page in your browser")
        try:
            raw = input("Mode [tui]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raw = ""
        return raw if raw in ("tui", "web") else "tui"


def _spawn_service_if_needed(api_port: int, dashboard_port: int) -> None:
    """Best-effort background spawn of ``nerya serve`` + the dashboard.

    Only used by ``--start-server``. We do *not* wait for either child
    to be healthy — the browser tab will retry on its own. Failures are
    logged but never fatal: the user can always start the service
    manually.
    """
    import shutil
    import subprocess

    nerya_bin = shutil.which("nerya") or shutil.which("nerya.cmd")
    if not nerya_bin:
        nerya_bin = sys.executable
        cmd = [nerya_bin, "-m", "nerya.cli.app", "serve",
               "--port", str(api_port),
               "--dashboard-port", str(dashboard_port)]
    else:
        cmd = [nerya_bin, "serve",
               "--port", str(api_port),
               "--dashboard-port", str(dashboard_port)]

    creationflags = 0
    preexec_fn = None
    if os.name == "posix":
        preexec_fn = os.setsid  # type: ignore[attr-defined]
    elif os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            preexec_fn=preexec_fn,
        )
    except Exception as exc:
        print(f"[nerya] failed to spawn service: {exc}", file=sys.stderr)


def cmd_setup(args) -> int:
    """Dispatch to the chosen wizard mode."""

    # Resolve mode.
    mode: Optional[str] = None
    if getattr(args, "tui", False):
        mode = "tui"
    elif getattr(args, "web", False):
        mode = "web"
    elif getattr(args, "print_url", False):
        mode = "print-url"
    elif getattr(args, "yes", False):
        # ``--yes`` implies TUI in non-interactive accept-all mode.
        mode = "tui-yes"
    else:
        mode = _pick_mode_interactively()

    quick = bool(getattr(args, "quick", False))

    if mode in ("tui", "tui-yes"):
        from .. import setup_tui

        client = _client(args.workspace, getattr(args, "profile", None))
        try:
            setup_tui.run(
                client,
                accept_defaults=(mode == "tui-yes"),
                quick=quick,
            )
        except KeyboardInterrupt:
            print("\n[nerya] setup aborted by user", file=sys.stderr)
            return 130
        return 0

    # All web-style modes route through the dashboard /setup page.
    url = _resolve_dashboard_url(args)
    if quick:
        # Tell the web wizard to render single-step mode. The
        # SetupWizard component reads `?mode=quick` from the URL on
        # mount and reduces the stepper to a single LLM card.
        url = url + ("&" if "?" in url else "?") + "mode=quick"

    # Smart auto-spawn: if the user picked --web (or its --print-url
    # variant doesn't need this) and the dashboard port isn't open,
    # spawn the service automatically *unless* the user explicitly opted
    # out via --no-auto-serve. This is the "lowest-friction install"
    # behaviour: typing `nerya setup --web` after a fresh install should
    # Just Work, not greet the user with a "site can't be reached".
    auto_spawn = (
        mode == "web"
        and not getattr(args, "no_auto_serve", False)
        and not getattr(args, "start_server", False)
    )
    dash_port = getattr(args, "dashboard_port", None) or _DEFAULT_DASHBOARD_PORT
    if auto_spawn:
        # Detect the port from the URL when possible so an overridden
        # --url still triggers the right probe.
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if parsed.port:
                dash_port = parsed.port
        except Exception:
            pass

        if not _port_is_open("127.0.0.1", int(dash_port)):
            print(
                f"[nerya] dashboard not running on :{dash_port} — "
                "spawning `nerya serve` automatically (pass "
                "--no-auto-serve to skip)."
            )
            _spawn_service_if_needed(
                getattr(args, "api_port", _DEFAULT_API_PORT) or _DEFAULT_API_PORT,
                int(dash_port),
            )
            _wait_for_port(
                "127.0.0.1", int(dash_port),
                timeout_s=45.0, label="dashboard",
            )

    if getattr(args, "start_server", False):
        api_port = getattr(args, "api_port", _DEFAULT_API_PORT) or _DEFAULT_API_PORT
        dash_port = (
            getattr(args, "dashboard_port", _DEFAULT_DASHBOARD_PORT)
            or _DEFAULT_DASHBOARD_PORT
        )
        print(f"[nerya] starting API on :{api_port} + dashboard on :{dash_port}…")
        _spawn_service_if_needed(api_port, dash_port)

    print(f"[nerya] setup wizard: {url}")

    if mode == "print-url":
        return 0

    if not getattr(args, "no_open", False):
        try:
            webbrowser.open(url)
        except Exception as exc:
            print(f"[nerya] could not open browser automatically: {exc}",
                  file=sys.stderr)
            print(f"[nerya] open this URL manually: {url}", file=sys.stderr)

    return 0


def register(sub) -> None:
    p = sub.add_parser(
        "setup",
        help="Configure password, LLM, gateway, memory, browser, "
             "account, and search via a TUI or web wizard.",
        description=(
            "Run the Nerya onboarding wizard. With no flags the command "
            "asks whether to use the TUI or open the web wizard at the "
            "dashboard's /setup page. Every domain (gateway, memory, "
            "browser, account, search) has a safe default — only the LLM "
            "model is required for a usable runtime."
        ),
    )
    _add_ws(p)

    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--tui",
        action="store_true",
        help="Run the Rich-based terminal wizard.",
    )
    mode.add_argument(
        "--web",
        action="store_true",
        help="Open the dashboard's /setup page in the default browser.",
    )
    mode.add_argument(
        "--print-url",
        action="store_true",
        dest="print_url",
        help="Print the /setup URL and exit (headless / CI).",
    )
    mode.add_argument(
        "--yes",
        action="store_true",
        help="Non-interactive TUI run that accepts every default "
             "(containers / smoke tests).",
    )

    # Web-mode plumbing.
    p.add_argument(
        "--url",
        default=None,
        help="Override the /setup URL (default: "
             "http://localhost:18380/setup).",
    )
    p.add_argument(
        "--dashboard-port",
        type=int,
        default=_DEFAULT_DASHBOARD_PORT,
        dest="dashboard_port",
        help="Dashboard port for the wizard URL (default: 18380).",
    )
    p.add_argument(
        "--api-port",
        type=int,
        default=_DEFAULT_API_PORT,
        dest="api_port",
        help="API port used when --start-server is passed (default: 18317).",
    )
    p.add_argument(
        "--start-server",
        action="store_true",
        dest="start_server",
        help="Spawn `nerya serve` in the background before opening the "
             "browser (handy for fresh installs).",
    )
    p.add_argument(
        "--no-auto-serve",
        action="store_true",
        dest="no_auto_serve",
        help="With --web: do NOT auto-spawn `nerya serve` even when "
             "the dashboard port is unreachable. Default behaviour "
             "spawns the service so a fresh-install user gets the "
             "wizard immediately.",
    )
    p.add_argument(
        "--no-open",
        action="store_true",
        dest="no_open",
        help="With --web: print the URL but do not open the browser.",
    )

    p.add_argument(
        "--quick",
        action="store_true",
        help="One-question setup: pick an LLM provider + model and "
             "accept defaults for the other 6 domains. The 80%% path "
             "for new users.",
    )

    p.set_defaults(func=cmd_setup)


__all__ = ["cmd_setup", "register"]
