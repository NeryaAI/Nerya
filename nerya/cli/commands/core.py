"""Core lifecycle commands: ``init``, ``run``/``serve``, ``dashboard``,
``doctor``, ``service install/uninstall/status``.

These form the minimal surface any installer or runbook depends on, so
keep them self-contained and side-effect-free at import time.
"""

from __future__ import annotations

import os
import signal
import sys

from .._common import _add_ws, _client, _print
from ...workspace.manager import WorkspaceManager


def cmd_init(args) -> int:
    if getattr(args, "profile", None) and args.workspace is None:
        from ...core.paths import _resolve_home
        target = _resolve_home() / args.profile
    else:
        target = args.workspace
    mgr = WorkspaceManager.init(target)
    print(f"[nerya] initialized workspace at {mgr.paths.root}")
    # Post-init nudge: encourage the operator to run the unified
    # onboarding wizard. Cheaply skipped when --no-hint is passed by
    # scripts/CI that just want a quiet ``init``.
    if not getattr(args, "no_hint", False):
        print()
        print("[nerya] next step: run the setup wizard")
        print("        nerya setup          # asks TUI or Web")
        print("        nerya setup --tui    # terminal wizard")
        print("        nerya setup --web    # opens dashboard /setup")
    return 0


def cmd_profile_list(args) -> int:
    """List profiles under ``$NERYA_HOME``."""
    from ...core.paths import _resolve_home, list_profiles
    home = _resolve_home()
    rows: list[dict] = []
    for name in list_profiles(home):
        rows.append({
            "name": name,
            "root": str((home / name).resolve()),
            "active": (
                (args.profile or "")
                == name
                or (
                    not args.profile
                    and home / name == home / "default"
                )
            ),
        })
    _print({"home": str(home), "profiles": rows})
    return 0


def cmd_profile_current(args) -> int:
    """Print the workspace path the current invocation would use."""
    from ...core.paths import resolve_workspace
    paths = resolve_workspace(args.workspace, profile=getattr(args, "profile", None))
    _print({"root": str(paths.root)})
    return 0


def cmd_profile_init(args) -> int:
    """Initialise a brand-new profile under ``$NERYA_HOME``."""
    from ...core.paths import _resolve_home
    if not args.name or not args.name.strip():
        print("[nerya] --name is required", file=sys.stderr)
        return 2
    home = _resolve_home()
    target = home / args.name.strip()
    mgr = WorkspaceManager.init(target)
    print(f"[nerya] initialized profile '{args.name}' at {mgr.paths.root}")
    return 0


def cmd_run(args) -> int:
    """Boot the local Nerya service.

    Default behaviour is "everything" so startup brings up the backend,
    frontend, and configured gateways together:

    - API server on the configured host/port,
    - configured gateways (Telegram long-poll, etc.) auto-attached by
      :func:`nerya.api.local_server.serve` /
      :func:`nerya.api.routes_gateway.launch_configured_gateways_on_start`,
    - the bundled Next.js dashboard spawned as a subprocess (when
      ``npm`` and ``dashboard/package.json`` are both available).

    ``--no-dashboard`` opts out (useful for headless deployments and
    CI). ``--with-dashboard`` / ``--all`` are kept as no-op aliases for
    backward compat — they used to be required to enable the dashboard.
    """
    from ...api.local_server import serve
    from ...core.dashboard import dashboard_port as configured_dashboard_port
    client = _client(args.workspace, getattr(args, "profile", None))

    no_dashboard = bool(getattr(args, "no_dashboard", False))
    spawn_dashboard = not no_dashboard
    dashboard_proc = None
    if spawn_dashboard:
        requested_dashboard_port = getattr(args, "dashboard_port", None)
        dash_port = requested_dashboard_port or configured_dashboard_port(client.config)
        dashboard_proc = _spawn_dashboard(
            dash_port,
            api_host=args.host,
            api_port=args.port,
        )

    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _handle_termination)
    try:
        serve(client.config, host=args.host, port=args.port)
    finally:
        if dashboard_proc is not None:
            _stop_dashboard(dashboard_proc)
        signal.signal(signal.SIGTERM, previous_sigterm)
    return 0


def _handle_termination(signum, _frame) -> None:
    raise SystemExit(128 + int(signum))


def _stop_dashboard(process) -> None:
    """Stop the dashboard and every child it spawned."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except Exception:
            pass


def _spawn_dashboard(port: int, *, api_host: str = "127.0.0.1", api_port: int = 18317):
    """Spawn the bundled Next.js dashboard.

    Returns the ``subprocess.Popen`` (or ``None`` if the dashboard
    folder / npm is not available — the API stays up regardless).

    The child is launched in its own process group on POSIX so a
    Ctrl-C on the parent terminates it cleanly. On Windows we rely on
    the standard ``terminate()`` path.

    Both ``NERYA_API`` (used by ``app/api/proxy/[...path]/route.ts`` and
    ``lib/api.ts`` server-side) and ``NEXT_PUBLIC_NERYA_API_BASE`` (legacy
    public env, kept for compatibility) are exported so the dashboard
    proxies hit the API server we just booted — not a stale default.
    """
    import os
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node") or shutil.which("node.exe")
    if node is None:
        print("[nerya] dashboard skipped: node not on PATH", file=sys.stderr)
        return None

    here = Path(__file__).resolve().parents[3]
    dashboard_dir = here / "dashboard"
    next_cli = here / "node_modules" / "next" / "dist" / "bin" / "next"
    if not (dashboard_dir / "package.json").exists() or not next_cli.exists():
        print(f"[nerya] dashboard skipped: {dashboard_dir} not found", file=sys.stderr)
        return None

    # 0.0.0.0 isn't a routable host from the dashboard process; rewrite to
    # loopback so the proxy can actually reach the API.
    safe_host = "127.0.0.1" if api_host in ("0.0.0.0", "::", "") else api_host
    api_base = f"http://{safe_host}:{api_port}"

    env = dict(os.environ)
    env["NERYA_API"] = api_base
    env["NEXT_PUBLIC_NERYA_API_BASE"] = api_base
    env["PORT"] = str(port)
    print(f"[nerya] dashboard NERYA_API={api_base}")

    creationflags = 0
    preexec_fn = None
    if os.name == "posix":
        preexec_fn = os.setsid  # type: ignore[attr-defined]
    elif os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    print(f"[nerya] launching dashboard: next dev (port={port}) at {dashboard_dir}")
    try:
        return subprocess.Popen(
            [node, str(next_cli), "dev"],
            cwd=str(dashboard_dir),
            env=env,
            stdout=None, stderr=None,
            creationflags=creationflags,
            preexec_fn=preexec_fn,
        )
    except Exception as exc:
        print(f"[nerya] dashboard failed to launch: {exc}", file=sys.stderr)
        return None


def cmd_dashboard(args) -> int:
    import os
    import webbrowser

    url = args.url or os.environ.get("NERYA_DASHBOARD_URL") or "http://localhost:3000"
    print(f"[nerya] dashboard: {url}")
    print("[nerya] run the dashboard with:  cd dashboard && npm install && npm run dev")
    print("[nerya] run the api with:        nerya serve --port 18317")
    if not getattr(args, "no_open", False):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return 0


def cmd_doctor(args) -> int:
    """operator-grade diagnostic surface.

    Walks the global :class:`~nerya.ops.diagnostics.DiagnosticRegistry`
    and aggregates results into a structured report. ``--json`` emits
    the raw machine-readable payload; default output is a multi-line
    human-readable render. Exit code: 0 if no errors, 1 otherwise
    (warnings do not fail the check — pair with ``--strict`` to fail
    on warnings too).
    """

    from ...ops import diagnostics as diag

    try:
        client = _client(args.workspace, getattr(args, "profile", None))
    except Exception:
        client = None

    only = getattr(args, "only", None) or None
    skip = getattr(args, "skip", None) or None
    only_ids = [s.strip() for s in only.split(",") if s.strip()] if only else None
    skip_ids = [s.strip() for s in skip.split(",") if s.strip()] if skip else None

    report = diag.run_diagnostics(client, only=only_ids, skip=skip_ids)

    if getattr(args, "json", False):
        _print(report.asdict())
    else:
        print(diag.render_doctor(report))

    if not report.ok:
        return 1
    if getattr(args, "strict", False) and report.has_warnings:
        return 1
    return 0


def cmd_status(args) -> int:
    """concise one-line-per-issue operator status.

    Same registry as ``nerya doctor`` but renders only the non-OK
    rows. Useful for shell prompts, status bars, and CI early-out.
    """

    from ...ops import diagnostics as diag

    try:
        client = _client(args.workspace, getattr(args, "profile", None))
    except Exception:
        client = None

    report = diag.run_diagnostics(client)
    if getattr(args, "json", False):
        _print({
            "ok": report.ok,
            "summary": dict(report.summary),
            "issues": [d.asdict() for d in report.diagnoses
                       if d.severity != "ok"],
        })
    else:
        print(diag.render_status(report))
    return 0 if report.ok else 1


def cmd_preflight(args) -> int:
    """Run ``nerya.ops.preflight`` and pretty-print the report.

    Operators use this before promoting between ``prod_paper``,
    ``canary_live`` and ``full_live`` — it validates runtime keys,
    connectors, TA-Lib, and mock/live conflicts up-front.
    """
    from ...ops.preflight import run_preflight
    client = _client(args.workspace, getattr(args, "profile", None))
    report = run_preflight(client.config, mode=args.mode)
    _print(report.asdict())
    return 0 if report.ok() else 1


def cmd_certify(args) -> int:
    """Run the certification gate for ``A``/``B``/``C``.

    This is the gate: preflight plus artifact-based evidence
    (paper cycle, pinned version, rollback target, kill switch,
    experimental capability review).
    """
    from ...ops.certification import run_gate
    client = _client(args.workspace, getattr(args, "profile", None))
    gate = args.gate.upper()
    report = run_gate(client.config, gate)  # type: ignore[arg-type]
    _print(report.asdict())
    return 0 if report.ok() else 1


def cmd_service_install(args) -> int:
    from ...install import service as svc
    return svc.install(workspace=args.workspace, port=args.port, force=args.force)


def cmd_service_uninstall(args) -> int:
    from ...install import service as svc
    return svc.uninstall()


def cmd_service_status(args) -> int:
    from ...install import service as svc
    _print(svc.status())
    return 0


def register(sub) -> None:
    p = sub.add_parser("init")
    _add_ws(p)
    p.add_argument(
        "--no-hint",
        action="store_true",
        dest="no_hint",
        help="Skip the post-init suggestion to run `nerya setup`.",
    )
    p.set_defaults(func=cmd_init)

    # ``run`` and ``serve`` are aliases — installer docs use ``serve``.
    for name in ("run", "serve"):
        p = sub.add_parser(name)
        _add_ws(p)
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=18317)
        # compatibility:
        # ``nerya run`` is the "boot everything" command — service +
        # dashboard + configured gateways (Telegram long-poll auto-
        # attached by local_server.serve). Headless deployments opt
        # out via ``--no-dashboard``. ``--with-dashboard`` / ``--all``
        # are kept as no-op compatibility aliases (they used to be the
        # opt-IN flag before this became the default).
        p.add_argument("--no-dashboard", action="store_true",
                       dest="no_dashboard",
                       help="Skip spawning the Next.js dashboard "
                            "(headless / CI mode).")
        p.add_argument("--with-dashboard", action="store_true",
                       dest="with_dashboard",
                       help="(deprecated) Dashboard now spawns by "
                            "default; this flag is a no-op kept for "
                            "scripts that still pass it.")
        p.add_argument("--dashboard-port", type=int, default=None,
                       dest="dashboard_port",
                       help="Port for the dashboard (default: dashboard.port or 18380).")
        p.add_argument("--all", action="store_true",
                       help="(deprecated) ``run`` is now ``all`` by "
                            "default. Kept for backward compatibility.")
        p.set_defaults(func=cmd_run)

    p = sub.add_parser("dashboard")
    _add_ws(p)
    p.add_argument("--url", default=None)
    p.add_argument("--no-open", action="store_true", dest="no_open")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("doctor")
    _add_ws(p)
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON instead of text")
    p.add_argument("--strict", action="store_true",
                   help="exit non-zero if any warnings are present")
    p.add_argument("--only", default=None,
                   help="comma-separated check ids to run (default: all)")
    p.add_argument("--skip", default=None,
                   help="comma-separated check ids to skip")
    p.set_defaults(func=cmd_doctor)

    # concise operator status (subset of doctor output).
    p = sub.add_parser("status")
    _add_ws(p)
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("preflight")
    _add_ws(p)
    p.add_argument("--mode", default="prod_paper",
                   choices=("local_dev", "prod_paper",
                            "canary_live", "full_live"))
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("certify")
    _add_ws(p)
    p.add_argument("--gate", default="A", choices=("A", "B", "C", "a", "b", "c"))
    p.set_defaults(func=cmd_certify)

    svc = sub.add_parser("service").add_subparsers(dest="svccmd", required=True)
    p = svc.add_parser("install"); _add_ws(p)
    p.add_argument("--port", type=int, default=18317)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_service_install)
    p = svc.add_parser("uninstall"); _add_ws(p)
    p.set_defaults(func=cmd_service_uninstall)
    p = svc.add_parser("status"); _add_ws(p)
    p.set_defaults(func=cmd_service_status)

    # runtime profile selector.
    prof = sub.add_parser("profile").add_subparsers(dest="profcmd", required=True)
    p = prof.add_parser("list"); _add_ws(p)
    p.set_defaults(func=cmd_profile_list)
    p = prof.add_parser("current"); _add_ws(p)
    p.set_defaults(func=cmd_profile_current)
    p = prof.add_parser("init"); _add_ws(p)
    p.add_argument("--name", required=True,
                   help="New profile name (becomes $NERYA_HOME/<name>).")
    p.set_defaults(func=cmd_profile_init)


__all__ = [
    "cmd_init", "cmd_run", "cmd_dashboard", "cmd_doctor", "cmd_status",
    "cmd_preflight", "cmd_certify",
    "cmd_service_install", "cmd_service_uninstall", "cmd_service_status",
    "cmd_profile_list", "cmd_profile_current", "cmd_profile_init",
    "register",
]
