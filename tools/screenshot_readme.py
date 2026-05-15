"""Capture dashboard screenshots for the README.

Headless Playwright capture against the locally-running dashboard (`:3001`).
Writes PNGs into ``branding/screenshots/``.

Usage::

    .venv/Scripts/python.exe tools/screenshot_readme.py
    .venv/Scripts/python.exe tools/screenshot_readme.py --locale zh --suffix -zh
    .venv/Scripts/python.exe tools/screenshot_readme.py --pages chat --clean-chat

Skipped if the dashboard is not reachable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib import request

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "branding" / "screenshots"


PAGES = [
    {"name": "home", "path": "/dashboard"},
    {"name": "chat", "path": "/chat"},
    {"name": "setup", "path": "/setup"},
    {"name": "portfolio", "path": "/portfolio"},
    {"name": "skills", "path": "/skills"},
    {"name": "memory", "path": "/memory"},
    {"name": "agents", "path": "/agents"},
    {"name": "gateway", "path": "/gateway"},
]


def _alive(base: str) -> bool:
    try:
        with request.urlopen(base + "/dashboard", timeout=20) as resp:
            return resp.status == 200
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:3001",
                        help="Dashboard base URL (default: http://127.0.0.1:3001)")
    parser.add_argument("--api", default="http://127.0.0.1:18317",
                        help="Nerya API base URL used for the localStorage shim")
    parser.add_argument("--locale", choices=["en", "zh"], default="en",
                        help="UI language to inject into the localStorage settings blob")
    parser.add_argument("--suffix", default="",
                        help="Append this suffix to every output filename, e.g. -zh")
    parser.add_argument("--pages", nargs="*", default=None,
                        help="Subset of page names to capture (default: all)")
    parser.add_argument("--clean-chat", action="store_true",
                        help="On /chat, wipe chat threads + hide sidebar + dismiss history "
                             "so the screenshot only shows the fresh-conversation hero.")
    args = parser.parse_args()

    if not _alive(args.base):
        print(f"[skip] dashboard not reachable at {args.base}; "
              f"start it first via scripts/windows/start-local.ps1", file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print(f"[fatal] playwright not installed: {exc}", file=sys.stderr)
        return 3

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # The dashboard reads its locale from `nerya.ui_settings.v1.language` in
    # localStorage (see Nerya/dashboard/lib/settings.ts). We inject the whole
    # settings blob in dark mode + the chosen locale before the first paint.
    ui_settings = {
        "kline": {"venue": "binance", "symbol": "BTCUSDT", "interval": "1h", "count": 96},
        "refreshSeconds": 30,
        "showVolume": True,
        "chartType": "candlestick",
        "compact": False,
        "timezone": "auto",
        "language": args.locale,
        "marketStream": "standard",
        "darkMode": True,
    }
    settings_blob = json.dumps(ui_settings)

    pages_to_run = PAGES
    if args.pages:
        pages_to_run = [p for p in PAGES if p["name"] in args.pages]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        ctx.add_init_script(
            f"""
            try {{
                window.localStorage.setItem('nerya:api', '{args.api}');
                window.localStorage.setItem('nerya.ui_settings.v1', {settings_blob!r});
            }} catch (_) {{}}
            """
        )

        page = ctx.new_page()

        for spec in pages_to_run:
            url = args.base + spec["path"]
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception:
                # networkidle can hang on live-polling pages; fall back to DOMContentLoaded
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                except Exception as exc2:
                    print(f"[warn] {spec['name']}: navigation failed ({exc2})", file=sys.stderr)
                    continue

            # Next.js dev mode lazy-compiles each route on first hit; the
            # chat page in particular waits for /api/proxy/* to stabilise.
            wait_ms = 8000 if spec["name"] == "chat" else 2500
            page.wait_for_timeout(wait_ms)

            if spec["name"] == "chat" and args.clean_chat:
                # Hide the per-user chat history sidebar so the screenshot shows
                # the canonical "fresh conversation" hero — no leaked personal data.
                page.evaluate(
                    """
                    () => {
                      try {
                        // Remove the left chat-history aside (md:w-64 marker is distinctive).
                        document.querySelectorAll('aside').forEach((el) => {
                          if (el.className && el.className.includes('md:w-64')) {
                            el.remove();
                          }
                        });
                      } catch (_) {}
                    }
                    """
                )
                page.wait_for_timeout(400)

            shot = OUT_DIR / f"dashboard-{spec['name']}{args.suffix}.png"
            try:
                page.screenshot(path=str(shot), full_page=False)
                size = shot.stat().st_size if shot.exists() else 0
                print(f"[ok]   {spec['name']:10s} -> {shot.relative_to(REPO_ROOT)} ({size} bytes)")
            except Exception as exc:
                print(f"[warn] {spec['name']}: screenshot failed ({exc})", file=sys.stderr)

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
