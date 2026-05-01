#!/usr/bin/env python3
"""Smoke-test the Nerya dashboard UI.

Three cheap checks that catch 90%% of regressions without a full ``next build``:

1. **Structure** — every route folder we expect under ``dashboard/app`` exists
   and has a ``page.tsx``. The sidebar nav and the dashboard's quick-actions
   links are cross-referenced against the filesystem so dead links fail loudly.
2. **Palette** — the tailwind config ships the violet/neon palette that the UI
   uses; prevents accidental reverts to the old green theme.
3. **Typecheck** — runs ``npx tsc --noEmit`` in the dashboard directory.

Optional extra: pass ``--live`` to also boot ``next dev`` on a free port and
HTTP-probe every route. We avoid ``next build`` on purpose (per project rule:
the user compiles, not us).

Usage (from repo root, with the dashboard installed)::

    python scripts/test_dashboard_ui.py                 # structure + typecheck
    python scripts/test_dashboard_ui.py --live          # + dev-server probe
    python scripts/test_dashboard_ui.py --skip-tsc      # just structure
"""
from __future__ import annotations

import argparse
import http.client
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
DASH = REPO_ROOT / "dashboard"
APP = DASH / "app"

EXPECTED_ROUTES = [
    "dashboard",
    "chat",
    "agents",
    "subagents",
    "skills",
    "triggers",
    "scripts",
    "portfolio",
    "orders",
    "strategies",
    "strategy-history",
    "messages",
    "memory",
    "evolution",
    "security",
    "settings",
]

PALETTE_MARKERS = [
    # violet primary
    r"#8b5cf6",
    r"#b48bff",
    # neon mint accent (PnL up, RUNNING)
    r"#10d993",
    # ink surface scale
    r"ink:\s*\{",
    # brand scale
    r"brand:\s*\{",
    # accent scale
    r"accent:\s*\{",
]


class Check:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.ok: list[str] = []

    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        print(f"  [FAIL] {msg}")

    def good(self, msg: str) -> None:
        self.ok.append(msg)
        print(f"  [OK]   {msg}")

    def summary(self) -> int:
        print()
        print("=" * 60)
        print(f"passed: {len(self.ok)}, failed: {len(self.failures)}")
        if self.failures:
            print("FAILED checks:")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        return 0


def _check_structure(c: Check) -> None:
    print("== structure ==")
    required_files = [
        APP / "layout.tsx",
        APP / "globals.css",
        DASH / "components" / "Sidebar.tsx",
        DASH / "components" / "TopHeader.tsx",
        DASH / "components" / "Page.tsx",
        DASH / "components" / "Sparkline.tsx",
        DASH / "components" / "icons.tsx",
        DASH / "components" / "chat" / "ChatView.tsx",
        DASH / "components" / "chat" / "ChatMessage.tsx",
        DASH / "components" / "chat" / "ChatInput.tsx",
        DASH / "components" / "chat" / "ChatSidebar.tsx",
        DASH / "components" / "chat" / "TurnBlocks.tsx",
        DASH / "lib" / "chat.ts",
        DASH / "lib" / "nav.ts",
        DASH / "lib" / "api.ts",
        DASH / "lib" / "client.ts",
        DASH / "lib" / "clientApi.ts",
        DASH / "lib" / "settings.ts",
        DASH / "components" / "CandleChart.tsx",
        DASH / "tailwind.config.ts",
    ]
    for f in required_files:
        if f.exists():
            c.good(f"file present: {f.relative_to(REPO_ROOT)}")
        else:
            c.fail(f"missing file: {f.relative_to(REPO_ROOT)}")

    for route in EXPECTED_ROUTES:
        page = APP / route / "page.tsx"
        if page.exists():
            c.good(f"route /{route} has page.tsx")
        else:
            c.fail(f"route /{route} missing page.tsx ({page})")

    # Sidebar nav links must cover all expected routes.
    nav_text = (DASH / "lib" / "nav.ts").read_text(encoding="utf-8")
    for route in EXPECTED_ROUTES:
        if f'"/{route}"' in nav_text:
            c.good(f"nav links /{route}")
        else:
            c.fail(f"nav missing link /{route}")


def _check_palette(c: Check) -> None:
    print("\n== palette ==")
    tw = (DASH / "tailwind.config.ts").read_text(encoding="utf-8")
    for pat in PALETTE_MARKERS:
        if re.search(pat, tw):
            c.good(f"tailwind has {pat!r}")
        else:
            c.fail(f"tailwind missing palette marker {pat!r}")

    css = (DASH / "app" / "globals.css").read_text(encoding="utf-8")
    for token in ("mode-toggle-wrap", "sidebar-item", "icon-btn", "pill-ok", "pill-brand"):
        if token in css:
            c.good(f"globals.css defines .{token}")
        else:
            c.fail(f"globals.css missing .{token}")


def _check_real_data_wiring(c: Check) -> None:
    """Make sure mock KPI numbers are out of the main dashboard, and the
    real-data endpoints / settings store are wired up."""
    print("\n== real data wiring ==")

    settings_src = (DASH / "lib" / "settings.ts").read_text(encoding="utf-8")
    for token in ("useUiSettings", "DEFAULT_SETTINGS", "kline", "venue", "interval",
                  "localStorage"):
        if token in settings_src:
            c.good(f"settings.ts references {token}")
        else:
            c.fail(f"settings.ts missing {token}")

    client_src = (DASH / "lib" / "clientApi.ts").read_text(encoding="utf-8")
    for token in ("/portfolio/summary", "/portfolio/equity_curve",
                  "/strategy/list", "/trading/recent_trades",
                  "/market/candles", "/market/venues"):
        if token in client_src:
            c.good(f"clientApi.ts calls {token}")
        else:
            c.fail(f"clientApi.ts missing call {token}")

    dashboard_src = (DASH / "app" / "dashboard" / "page.tsx").read_text(encoding="utf-8")
    for token in ("useUiSettings", "CandleChart", "clientApi.marketCandles",
                  "clientApi.portfolioSummary", "clientApi.strategyList",
                  "clientApi.recentTrades"):
        if token in dashboard_src:
            c.good(f"dashboard page uses {token}")
        else:
            c.fail(f"dashboard page missing {token}")

    # Previous mock KPI literals should no longer be present.
    for legacy in ("128932.24", "mockStrategies", "mockPositions", "mockTrades"):
        if legacy in dashboard_src:
            c.fail(f"dashboard page still has mock literal '{legacy}'")
        else:
            c.good(f"dashboard page no longer uses '{legacy}'")

    settings_page = (DASH / "app" / "settings" / "page.tsx").read_text(encoding="utf-8")
    if "useUiSettings" in settings_page and "kline" in settings_page:
        c.good("settings page binds to useUiSettings (k-line controls)")
    else:
        c.fail("settings page does not bind to useUiSettings / kline")

    api_dir = REPO_ROOT / "nerya" / "api"
    for module_name in ("routes_portfolio", "routes_market"):
        p = api_dir / f"{module_name}.py"
        if p.exists():
            c.good(f"backend module present: {p.relative_to(REPO_ROOT)}")
        else:
            c.fail(f"backend module missing: {p.relative_to(REPO_ROOT)}")

    local_server = (api_dir / "local_server.py").read_text(encoding="utf-8")
    for module_name in ("routes_portfolio", "routes_market"):
        if module_name in local_server:
            c.good(f"local_server.py registers {module_name}")
        else:
            c.fail(f"local_server.py does not import {module_name}")


def _check_typecheck(c: Check) -> None:
    print("\n== tsc --noEmit ==")
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        c.fail("npx not found on PATH; install Node.js to enable typecheck")
        return
    try:
        out = subprocess.run(
            [npx, "--no-install", "tsc", "--noEmit"],
            cwd=DASH,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        c.fail("tsc timed out after 180s")
        return
    if out.returncode == 0:
        c.good("npx tsc --noEmit clean")
    else:
        snippet = (out.stdout + out.stderr).strip().splitlines()[:40]
        c.fail("tsc reported errors:\n    " + "\n    ".join(snippet))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _probe(host: str, port: int, path: str, timeout: float = 3.0) -> tuple[int, str]:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read(2048).decode(errors="replace")
        return resp.status, body
    finally:
        conn.close()


def _check_dev_server(c: Check, probe_routes: Iterable[str]) -> None:
    print("\n== dev server probe ==")
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        c.fail("npx not found; cannot start next dev")
        return
    port = _free_port()
    env = os.environ.copy()
    env["NEXT_TELEMETRY_DISABLED"] = "1"
    proc = subprocess.Popen(
        [npx, "--no-install", "next", "dev", "-p", str(port), "-H", "127.0.0.1"],
        cwd=DASH,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # wait for "Ready" or "started server" in the stdout, or for the port.
        deadline = time.time() + 60
        ready = False
        while time.time() < deadline:
            try:
                status, _ = _probe("127.0.0.1", port, "/dashboard")
                ready = status < 500
                if ready:
                    break
            except Exception:
                pass
            time.sleep(1)
        if not ready:
            c.fail("next dev did not become ready within 60s")
            return
        c.good(f"next dev up on 127.0.0.1:{port}")
        for r in probe_routes:
            try:
                status, body = _probe("127.0.0.1", port, f"/{r}")
            except Exception as exc:
                c.fail(f"/{r} probe failed: {exc}")
                continue
            if status != 200:
                c.fail(f"/{r} returned HTTP {status}")
                continue
            # Require that NERYA shows up somewhere (sidebar logo is always there).
            if "NERYA" not in body and "Nerya" not in body:
                c.fail(f"/{r} body did not mention NERYA")
                continue
            c.good(f"/{r} HTTP 200 + NERYA present")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-tsc", action="store_true", help="skip tsc --noEmit")
    ap.add_argument("--live", action="store_true", help="boot next dev and probe routes")
    args = ap.parse_args()

    c = Check()
    _check_structure(c)
    _check_palette(c)
    _check_real_data_wiring(c)
    if not args.skip_tsc:
        _check_typecheck(c)
    if args.live:
        _check_dev_server(c, EXPECTED_ROUTES)
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
