"""Rasterize a single SVG to PNG via headless Chromium.

Used to produce a PNG fallback of ``branding/feature-grid.svg`` so that
GitHub README renders it consistently even when ``<img src="*.svg">`` is
stripped of advanced features.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="src", required=True)
    parser.add_argument("--out", dest="dst", required=True)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    args = parser.parse_args()

    src = Path(args.src).resolve()
    dst = Path(args.dst).resolve()
    if not src.exists():
        print(f"[fatal] missing {src}", file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print(f"[fatal] playwright not installed: {exc}", file=sys.stderr)
        return 3

    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>body{margin:0;padding:0;background:transparent}</style></head>"
        "<body>" + src.read_text(encoding="utf-8") + "</body></html>"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=2,
        )
        page = ctx.new_page()
        page.set_content(html, wait_until="domcontentloaded")
        page.wait_for_timeout(400)
        dst.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(dst),
            clip={"x": 0, "y": 0, "width": args.width, "height": args.height},
        )
        browser.close()

    print(f"[ok] {src} -> {dst} ({dst.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
