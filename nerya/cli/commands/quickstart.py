"""``nerya quickstart`` — one-command "I just installed, get me going".

For a brand-new user (or anyone re-trying after a workspace wipe) this
command:

1. Initialises the workspace if it doesn't exist yet.
2. Probes the dashboard port; spawns ``nerya serve`` in the background
   if nothing's listening, then waits up to ~30 s for it to be ready.
3. Runs the **quick** wizard (``nerya setup --quick``). With no flag
   this asks the operator interactively whether to use the TUI or
   the web view; with ``--tui`` / ``--web`` it skips the picker.
4. Opens the dashboard once setup completes (unless ``--no-open`` or
   we're already in the web wizard).

The goal is "type `nerya quickstart`, answer one question (LLM
provider + key), have a working runtime in <60 seconds".
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from .._common import _add_ws

_DEFAULT_API_PORT = 18317
_DEFAULT_DASHBOARD_PORT = 18380


def _port_is_open(host: str, port: int, *, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_port(
    host: str, port: int, *, timeout_s: float = 30.0, label: str = "service",
) -> bool:
    """Poll the TCP port until it accepts a connection or we time out."""
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


def _workspace_exists(workspace: str | None, profile: str | None) -> bool:
    """Best-effort check that ``nerya.yml`` exists at the resolved
    workspace root. We don't want to overwrite a partial workspace."""
    try:
        from ...core.paths import resolve_workspace
    except Exception:
        return True  # be conservative — assume it exists
    try:
        paths = resolve_workspace(workspace, profile=profile)
        return paths.config.exists()
    except Exception:
        return False


def _spawn_service(api_port: int, dashboard_port: int) -> subprocess.Popen | None:
    """Background-spawn ``nerya serve`` (which also boots the
    dashboard). Returns the subprocess handle, or ``None`` on failure.
    """
    import shutil

    nerya_bin = shutil.which("nerya") or shutil.which("nerya.cmd")
    if nerya_bin:
        cmd = [
            nerya_bin, "serve",
            "--port", str(api_port),
            "--dashboard-port", str(dashboard_port),
        ]
    else:
        # In-tree fallback (development install / packaged shim missing).
        cmd = [
            sys.executable, "-m", "nerya.cli.app", "serve",
            "--port", str(api_port),
            "--dashboard-port", str(dashboard_port),
        ]

    creationflags = 0
    preexec_fn = None
    if os.name == "posix":
        preexec_fn = os.setsid  # type: ignore[attr-defined]
    elif os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        return subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            preexec_fn=preexec_fn,
        )
    except Exception as exc:
        print(f"[nerya] failed to spawn service: {exc}", file=sys.stderr)
        return None


def cmd_quickstart(args) -> int:
    """One-shot: workspace + service + quick setup + dashboard."""
    workspace = getattr(args, "workspace", None)
    profile = getattr(args, "profile", None)
    api_port = int(getattr(args, "api_port", _DEFAULT_API_PORT) or _DEFAULT_API_PORT)
    dash_port = int(
        getattr(args, "dashboard_port", _DEFAULT_DASHBOARD_PORT)
        or _DEFAULT_DASHBOARD_PORT
    )
    no_open = bool(getattr(args, "no_open", False))
    no_service = bool(getattr(args, "no_service", False))
    mode = (getattr(args, "mode", None) or "tui").strip().lower()
    if mode not in ("tui", "web"):
        mode = "tui"

    # 1. Workspace.
    if not _workspace_exists(workspace, profile):
        print("[nerya] step 1/4: initialising workspace")
        from ...workspace.manager import WorkspaceManager
        target = workspace
        if profile and not workspace:
            from ...core.paths import _resolve_home
            target = str(_resolve_home() / profile)
        mgr = WorkspaceManager.init(target)
        print(f"        workspace at {mgr.paths.root}")
    else:
        print("[nerya] step 1/4: workspace already initialised")

    # 2. Service (only when running in web mode — TUI mode is in-process).
    spawned: subprocess.Popen | None = None
    if not no_service and mode == "web":
        if _port_is_open("127.0.0.1", dash_port):
            print(f"[nerya] step 2/4: dashboard already running on :{dash_port}")
        else:
            print(f"[nerya] step 2/4: starting Nerya service on :{api_port} + dashboard on :{dash_port}")
            spawned = _spawn_service(api_port, dash_port)
            if spawned is None:
                return 1
            ok = _wait_for_port(
                "127.0.0.1", dash_port, timeout_s=45.0, label="dashboard"
            )
            if not ok:
                print(
                    "[nerya] dashboard didn't come up in 45s — continuing "
                    "anyway. Tail logs with `nerya status`.",
                    file=sys.stderr,
                )
    else:
        print("[nerya] step 2/4: skipping service (TUI mode)")

    # 3. Quick setup (cuts to a single LLM question).
    print("[nerya] step 3/4: running quick setup")
    from . import setup as setup_cmd

    # Build a synthetic args object — we just need the same fields the
    # ``setup`` argparser exposes. Reusing the real parser would force
    # us to wrap ``argparse`` again; this is simpler and tested.
    class _ShimArgs:
        pass

    shim = _ShimArgs()
    shim.workspace = workspace
    shim.profile = profile
    shim.tui = mode == "tui"
    shim.web = mode == "web"
    shim.print_url = False
    shim.yes = False
    shim.url = getattr(args, "url", None)
    shim.dashboard_port = dash_port
    shim.api_port = api_port
    shim.no_open = no_open
    # Don't double-spawn the service — we already did that above.
    shim.start_server = False
    shim.quick = True
    rc = setup_cmd.cmd_setup(shim)
    if rc != 0:
        return rc

    # 4. Open the dashboard (web mode opens the wizard tab itself).
    if mode == "tui" and not no_open and not no_service:
        # Even in TUI mode we offer to open the running dashboard so the
        # user lands on the live runtime view, not just a CLI prompt.
        if _port_is_open("127.0.0.1", dash_port):
            url = f"http://localhost:{dash_port}/"
            print(f"[nerya] step 4/4: opening dashboard at {url}")
            try:
                webbrowser.open(url)
            except Exception as exc:
                print(f"[nerya] couldn't auto-open: {exc}", file=sys.stderr)
        else:
            print(
                "[nerya] step 4/4: dashboard not running. Start it with "
                "[cyan]nerya serve[/cyan]."
            )
    else:
        print("[nerya] step 4/4: done")

    return 0


def register(sub) -> None:
    p = sub.add_parser(
        "quickstart",
        help="One-command path: workspace + service + quick setup + "
             "dashboard. Recommended for new users.",
        description=(
            "Run everything a new user needs in one shot: initialise the "
            "workspace, start the Nerya service (if web mode), run the "
            "quick (LLM-only) setup wizard, and open the dashboard. "
            "Typing `nerya quickstart` immediately after install should "
            "produce a working runtime in <60 seconds (answer one "
            "question: pick a provider + paste an API key)."
        ),
    )
    _add_ws(p)
    p.add_argument(
        "--mode",
        choices=["tui", "web"],
        default="tui",
        help="How to run the wizard. tui = terminal (default, fastest), "
             "web = open the dashboard /setup page in your browser.",
    )
    p.add_argument(
        "--api-port",
        type=int,
        default=_DEFAULT_API_PORT,
        dest="api_port",
        help="API port (default: 18317).",
    )
    p.add_argument(
        "--dashboard-port",
        type=int,
        default=_DEFAULT_DASHBOARD_PORT,
        dest="dashboard_port",
        help="Dashboard port (default: 18380).",
    )
    p.add_argument(
        "--no-service",
        action="store_true",
        dest="no_service",
        help="Don't spawn `nerya serve` automatically (you'll start it "
             "yourself).",
    )
    p.add_argument(
        "--no-open",
        action="store_true",
        dest="no_open",
        help="Don't open the browser at the end.",
    )
    p.add_argument(
        "--url",
        default=None,
        help="Override the dashboard URL (e.g. for tunnels).",
    )
    p.set_defaults(func=cmd_quickstart)


__all__ = ["cmd_quickstart", "register"]
