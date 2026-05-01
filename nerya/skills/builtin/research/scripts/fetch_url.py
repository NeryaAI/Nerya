"""Fetch a URL and return the body, optionally stripped to readable text.

Standalone CLI usage::

    python -m nerya.skills.builtin.research.scripts.fetch_url \\
        --json '{"url": "https://example.com", "strip_html": true}'

Output schema::

    {
      "ok": bool,
      "status": int,
      "url": str,
      "title": str,
      "content_type": str,
      "bytes": int,
      "truncated": bool,
      "elapsed_ms": int,
      "text": str
    }
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from html.parser import HTMLParser
from typing import Any

from ._http import DEFAULT_TIMEOUT, HARD_FETCH_BYTES, http_get


_DEFAULT_FETCH_BYTES = 200_000


class _TextExtractor(HTMLParser):
    """Minimal HTML → plain-text stripper.

    Skips ``<script>`` / ``<style>`` content, collapses whitespace,
    inserts double newlines on block boundaries so the output stays
    legible in the agent's tool result.
    """

    _SKIP = {"script", "style", "noscript", "svg"}
    _BLOCK = {
        "p", "div", "section", "article", "li", "tr", "br", "hr", "h1",
        "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "ul", "ol",
        "table", "thead", "tbody", "tfoot",
    }

    def __init__(self) -> None:
        super().__init__()
        self._buf: list[str] = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in self._BLOCK and self._buf and not self._buf[-1].endswith("\n"):
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag in self._BLOCK and self._buf and not self._buf[-1].endswith("\n"):
            self._buf.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip() + " "
            return
        self._buf.append(data)

    def text(self) -> str:
        joined = "".join(self._buf)
        normalized = re.sub(r"[ \t]+", " ", joined)
        normalized = re.sub(r"\n[ \t]+", "\n", normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip()


def run(
    *,
    url: str,
    strip_html: bool = True,
    max_bytes: int = _DEFAULT_FETCH_BYTES,
    timeout_s: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    if not url:
        return {"ok": False, "error": "url is required"}
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"ok": False, "error": "url must be http(s)"}

    max_bytes = max(1024, min(int(max_bytes), HARD_FETCH_BYTES))
    started = time.monotonic()
    try:
        status, headers, body = http_get(url, timeout=timeout_s)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    truncated = len(body) > max_bytes
    body = body[:max_bytes]
    content_type = (headers.get("content-type") or "").lower()
    text = ""
    title = ""
    if "text/html" in content_type or url.endswith((".html", ".htm")):
        decoded = body.decode("utf-8", errors="replace")
        if strip_html:
            extractor = _TextExtractor()
            try:
                extractor.feed(decoded)
            except Exception:
                pass
            text = extractor.text()
            title = extractor.title.strip()
        else:
            text = decoded
    else:
        text = body.decode("utf-8", errors="replace")

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "ok": 200 <= status < 400,
        "status": status,
        "url": url,
        "title": title,
        "content_type": content_type,
        "bytes": len(body),
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
        "text": text,
    }


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    if args.payload_json:
        return json.loads(args.payload_json) or {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", dest="payload_json", default=None)
    parser.add_argument("--payload-file", dest="payload_file", default=None)
    parser.add_argument("--url", dest="url", default=None)
    args = parser.parse_args()

    payload = _load_payload(args)
    url = args.url or payload.get("url")
    if not url:
        sys.stderr.write("url is required\n")
        raise SystemExit(2)

    try:
        result = run(
            url=url,
            strip_html=bool(payload.get("strip_html", True)),
            max_bytes=int(payload.get("max_bytes") or _DEFAULT_FETCH_BYTES),
            timeout_s=float(payload.get("timeout_s") or DEFAULT_TIMEOUT),
        )
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
