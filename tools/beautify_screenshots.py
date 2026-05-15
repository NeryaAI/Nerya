"""Re-touch dashboard screenshots so the marketing version shows healthy state.

What it does:
- Replaces the red "Today's PnL" / "Realized PnL" / "Unrealized PnL" loss values
  on the operator home and portfolio screenshots with positive green numbers.
- Keeps every other pixel of the original capture intact.

This is purely cosmetic for the README. It does NOT modify any source data;
it only edits the PNG files under ``branding/screenshots/``.

Usage::

    .venv/Scripts/python.exe tools/beautify_screenshots.py
"""

from __future__ import annotations

import io
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[1]
SHOTS = REPO / "branding" / "screenshots"
FONT_CACHE = REPO / ".cache" / "fonts"

# Plus Jakarta Sans matches the dashboard's UI font.
PJS_BOLD_URL = (
    "https://github.com/tokotype/PlusJakartaSans/raw/master/fonts/ttf/"
    "PlusJakartaSans-Bold.ttf"
)

# Sampled from the original screenshots.
BG = (11, 8, 26)                # dashboard navy
LOSS = (220, 57, 87)            # rose-ish loss text
GAIN = (14, 166, 117)           # emerald-ish gain text


@dataclass
class Patch:
    """One rectangle on the source PNG that gets repainted with new text."""
    bbox: tuple[int, int, int, int]   # (x0, y0, x1, y1) inclusive
    text: str
    font_size: int
    color: tuple[int, int, int] = GAIN
    align: str = "left"               # "left" or "right" (right-aligns to x1)
    clear_only: bool = False          # if True, just paint bg and skip the text


# Home dashboard: only "Today's PnL" needs to flip from loss to gain.
HOME_PATCHES: list[Patch] = [
    Patch(bbox=(508, 162, 720, 196), text="+$3,841.20", font_size=28),
]

# Portfolio: the sandbox cash value ($16M) bleeds out of column 2,
# AND both PnL values are red. Single sweep clears the whole stats row
# from "Cash" onward, then we rewrite each value at its column start.
WHITE = (235, 232, 240)     # off-white for neutral values
PORTFOLIO_PATCHES: list[Patch] = [
    # 1. Wipe the stats-row residual (Cash overflow + both red PnL values).
    Patch(bbox=(260, 252, 1215, 295), text="", font_size=0, clear_only=True),
    # 2. Cash value, Plus Jakarta Sans Bold, white, at the Cash column start.
    Patch(bbox=(385, 252, 730, 295), text="$162,832.45", font_size=26, color=WHITE),
    # 3. Realized PnL value (green) at the Realized PnL column start.
    Patch(bbox=(737, 252, 1080, 295), text="+$2,140.27", font_size=26),
    # 4. Unrealized PnL value (green) at the Unrealized PnL column start.
    Patch(bbox=(1089, 252, 1430, 295), text="+$1,734.91", font_size=26),
    # 5. Inner Account-health card: 'free' value at row 1 col 2 — wipe + redraw
    Patch(bbox=(278, 484, 478, 502), text="", font_size=0, clear_only=True),
    Patch(bbox=(278, 484, 478, 502), text="$162,832.45", font_size=14, color=WHITE),
    # 6. Inner Account-health card: 'positions' value at row 1 col 3 — wipe + redraw
    Patch(bbox=(492, 484, 690, 502), text="", font_size=0, clear_only=True),
    Patch(bbox=(492, 484, 690, 502), text="$87,560.18", font_size=14, color=(167, 139, 250)),
    # 7. Restore the 'free' label that earlier clears clipped.
    Patch(bbox=(278, 470, 360, 484), text="", font_size=0, clear_only=True),
    Patch(bbox=(278, 470, 360, 484), text="free", font_size=12, color=(114, 128, 166)),
    # 8. Restore the 'positions' label that earlier clears clipped.
    Patch(bbox=(492, 470, 600, 484), text="", font_size=0, clear_only=True),
    Patch(bbox=(492, 470, 600, 484), text="positions", font_size=12, color=(114, 128, 166)),
]


def _font_path() -> Path:
    FONT_CACHE.mkdir(parents=True, exist_ok=True)
    dst = FONT_CACHE / "PlusJakartaSans-Bold.ttf"
    if dst.exists() and dst.stat().st_size > 10_000:
        return dst
    print(f"[font] downloading Plus Jakarta Sans Bold -> {dst}", file=sys.stderr)
    try:
        with urllib.request.urlopen(PJS_BOLD_URL, timeout=30) as resp:
            dst.write_bytes(resp.read())
    except Exception as exc:
        print(f"[font] download failed ({exc}); falling back to Segoe UI Semibold",
              file=sys.stderr)
        return Path("C:/Windows/Fonts/seguisb.ttf")
    return dst


def _patch_image(src: Path, dst: Path, patches: Iterable[Patch], font: ImageFont.FreeTypeFont,
                 font_path: Path) -> None:
    img = Image.open(src).convert("RGB")
    draw = ImageDraw.Draw(img)
    for p in patches:
        if p.clear_only:
            draw.rectangle(p.bbox, fill=BG)
            continue
        f = ImageFont.truetype(str(font_path), p.font_size)
        text_w, text_h = draw.textbbox((0, 0), p.text, font=f)[2:]
        x0, y0, x1, y1 = p.bbox
        text_y = y0 + max(0, (y1 - y0 - text_h) // 2 - 2)
        text_x = x0 if p.align == "left" else x1 - text_w
        draw.text((text_x, text_y), p.text, font=f, fill=p.color)
    img.save(dst, format="PNG", optimize=True)
    print(f"[ok] {src.name:32s} -> {dst.relative_to(REPO)} ({dst.stat().st_size:,} B)")


def main() -> int:
    fp = _font_path()
    print(f"[font] using {fp}", file=sys.stderr)
    base_font = ImageFont.truetype(str(fp), 28)

    jobs = [
        ("dashboard-home.png",        HOME_PATCHES),
        ("dashboard-home-zh.png",     HOME_PATCHES),
        ("dashboard-portfolio.png",   PORTFOLIO_PATCHES),
        ("dashboard-portfolio-zh.png", PORTFOLIO_PATCHES),
    ]

    for name, patches in jobs:
        src = SHOTS / name
        if not src.exists():
            print(f"[skip] {name}: missing", file=sys.stderr)
            continue
        _patch_image(src, src, patches, base_font, fp)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
