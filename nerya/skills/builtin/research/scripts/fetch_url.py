"""Fetch a URL and return readable markdown/text with safe fallbacks.

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
      "fetch_method": str,
      "fallback_errors": [str, ...],
      "elapsed_ms": int,
      "markdown": str,
      "text": str
    }
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
import sys
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import os
from pathlib import Path

from nerya.security.web_safety import evaluate_url

from ._http import DEFAULT_TIMEOUT, HARD_FETCH_BYTES, http_get
from . import _scrapling


def _workspace_root() -> Path:
    """Mirror the ``NERYA_WORKSPACE`` resolution used by ``_engine_config``."""
    workspace = os.environ.get("NERYA_WORKSPACE")
    if not workspace:
        workspace = str(Path.home() / ".nerya")
    return Path(workspace).expanduser()


_DEFAULT_FETCH_BYTES = 200_000
_MIN_USEFUL_CHARS = 160
_JINA_READER_PREFIX = "https://r.jina.ai/"
_BLOCKER_PATTERNS = (
    "enable javascript",
    "just a moment",
    "checking your browser",
    "captcha",
    "access denied",
    "forbidden",
    "request blocked",
)


@dataclass
class _Extraction:
    text: str
    title: str = ""
    method: str = "plain_text"
    error: str = ""


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


def _normalize_markdown(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _looks_low_quality(text: str, *, min_chars: int) -> bool:
    clean = " ".join((text or "").split())
    if len(clean) < min_chars:
        return True
    lowered = clean[:1200].lower()
    return any(pattern in lowered for pattern in _BLOCKER_PATTERNS)


def _extract_with_trafilatura(html_text: str, url: str) -> _Extraction:
    try:
        import trafilatura  # type: ignore[import-not-found]
    except Exception as exc:
        return _Extraction("", method="trafilatura", error=f"unavailable: {exc}")
    try:
        extracted = trafilatura.extract(
            html_text,
            url=url,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            include_links=True,
            deduplicate=True,
        )
    except Exception as exc:
        return _Extraction("", method="trafilatura", error=f"{type(exc).__name__}: {exc}")
    return _Extraction(_normalize_markdown(extracted or ""), method="trafilatura")


def _extract_with_markdownify(html_text: str) -> _Extraction:
    try:
        from markdownify import markdownify as md  # type: ignore[import-not-found]
    except Exception as exc:
        return _Extraction("", method="markdownify", error=f"unavailable: {exc}")
    try:
        cleaned = re.sub(
            r"<(script|style|noscript|svg|head)\b[^>]*>.*?</\1>",
            "",
            html_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        markdown = md(
            cleaned,
            heading_style="ATX",
            strip=["script", "style", "noscript", "svg", "head"],
        )
    except Exception as exc:
        return _Extraction("", method="markdownify", error=f"{type(exc).__name__}: {exc}")
    return _Extraction(_normalize_markdown(markdown), method="markdownify")


def _extract_with_stdlib(html_text: str) -> _Extraction:
    extractor = _TextExtractor()
    try:
        extractor.feed(html_text)
    except Exception:
        pass
    return _Extraction(
        text=extractor.text(),
        title=extractor.title.strip(),
        method="stdlib_html_text",
    )


def _extract_html(html_text: str, *, url: str, min_content_chars: int) -> _Extraction:
    errors: list[str] = []
    for extractor in (
        lambda: _extract_with_trafilatura(html_text, url),
        lambda: _extract_with_markdownify(html_text),
        lambda: _extract_with_stdlib(html_text),
    ):
        result = extractor()
        if result.error:
            errors.append(f"{result.method}: {result.error}")
        if result.text and not _looks_low_quality(result.text, min_chars=min_content_chars):
            return result
    fallback = _extract_with_stdlib(html_text)
    if errors:
        fallback.error = "; ".join(errors)
    return fallback


def _jina_reader_url(url: str) -> str:
    # Reader's public contract is prefix-based:
    # https://r.jina.ai/https://example.com/page
    return _JINA_READER_PREFIX + url


def _extract_jina_title(markdown: str) -> str:
    for line in (markdown or "").splitlines()[:8]:
        stripped = line.strip()
        if stripped.lower().startswith("title:"):
            return stripped.split(":", 1)[1].strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _fetch_jina_reader(
    *,
    safe_url: str,
    max_bytes: int,
    timeout_s: float,
) -> tuple[dict[str, Any] | None, str | None]:
    reader_url = _jina_reader_url(safe_url)
    decision = evaluate_url(reader_url)
    if not decision.is_allowed():
        return None, f"jina_reader blocked by safety policy: {decision.reason}: {decision.note}"
    try:
        status, headers, body = http_get(
            decision.url,
            timeout=timeout_s,
            extra_headers={"Accept": "text/plain"},
        )
    except Exception as exc:
        return None, f"jina_reader: {type(exc).__name__}: {exc}"
    truncated = len(body) > max_bytes
    body = body[:max_bytes]
    markdown = _normalize_markdown(body.decode("utf-8", errors="replace"))
    if status >= 400:
        return None, f"jina_reader HTTP {status}"
    if not markdown:
        return None, "jina_reader returned empty content"
    return {
        "status": status,
        "url": safe_url,
        "reader_url": decision.url,
        "title": _extract_jina_title(markdown),
        "content_type": (headers.get("content-type") or "text/plain").lower(),
        "bytes": len(body),
        "truncated": truncated,
        "fetch_method": "jina_reader",
        "markdown": markdown,
        "text": markdown,
    }, None


def _try_browser_engine(
    *,
    safe_url: str,
    timeout_s: float,
    min_content_chars: int,
    fallback_errors: list[str],
    safety_dict: dict[str, Any],
    started_at: float,
) -> dict[str, Any] | None:
    """Try the operator-selected headless-browser engine.

    Sits between Jina Reader and Scrapling in the fallback chain. Returns
    ``None`` when no engine is selected, the engine isn't installed, or
    its output looks low-quality.
    """
    try:
        from nerya.integrations import browser_engines as _be
    except Exception as exc:  # noqa: BLE001
        fallback_errors.append(f"browser_engine: import failed: {exc}")
        return None
    try:
        result = _be.fetch(_workspace_root(), url=safe_url,
                           timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001
        fallback_errors.append(f"browser_engine: {type(exc).__name__}: {exc}")
        return None

    if not result.get("ok"):
        err = result.get("error") or ""
        if err and err not in {"no_engine_selected", "engine_not_installed"}:
            fallback_errors.append(
                f"browser:{result.get('name') or '?'}: {err} "
                f"{result.get('detail') or ''}".strip()
            )
        return None

    body = (result.get("markdown") or result.get("text")
            or result.get("html") or "")
    if not body:
        fallback_errors.append(f"browser:{result.get('name')}: empty output")
        return None
    # If the engine returned raw HTML, strip it to plaintext before quality check.
    if result.get("html") and not result.get("markdown"):
        extracted = _extract_html(
            body, url=safe_url, min_content_chars=min_content_chars,
        )
        body = extracted.text or body

    if _looks_low_quality(body, min_chars=min_content_chars):
        fallback_errors.append(
            f"browser:{result.get('name')}: low-quality content "
            f"({len(body)} chars)"
        )
        return None

    return {
        "ok": True,
        "status": 200,
        "url": safe_url,
        "title": "",
        "content_type": "text/markdown",
        "bytes": result.get("bytes") or len(body),
        "truncated": False,
        "fetch_method": result.get("fetch_method") or f"browser:{result.get('name')}",
        "fallback_errors": fallback_errors,
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
        "markdown": body,
        "text": body,
        "safety": safety_dict,
    }


def _try_scrapling(
    *,
    safe_url: str,
    timeout_s: float,
    min_content_chars: int,
    fallback_errors: list[str],
    safety_dict: dict[str, Any],
    started_at: float,
) -> dict[str, Any] | None:
    """Last-resort: try Scrapling (StealthyFetcher → Dynamic → plain).

    Returns a fully-formed response dict on success, ``None`` if Scrapling
    is not installed or every tier failed. Failure reasons are appended
    to ``fallback_errors`` so the caller can surface them.
    """
    try:
        scr = _scrapling.fetch(
            url=safe_url,
            timeout_s=timeout_s,
            prefer="auto",
        )
    except Exception as exc:  # noqa: BLE001
        fallback_errors.append(f"scrapling: {type(exc).__name__}: {exc}")
        return None

    if not scr.ok:
        if scr.error:
            fallback_errors.append(scr.error)
        for err in (scr.fallback_errors or []):
            fallback_errors.append(err)
        return None

    if _looks_low_quality(scr.markdown, min_chars=min_content_chars):
        fallback_errors.append(
            f"{scr.fetch_method}: low-quality content "
            f"({len(scr.markdown)} chars)"
        )
        return None

    return {
        "ok": True,
        "status": scr.status,
        "url": scr.url,
        "title": scr.title,
        "content_type": "text/html",
        "bytes": scr.bytes,
        "truncated": False,
        "fetch_method": scr.fetch_method,
        "fallback_errors": fallback_errors,
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
        "markdown": scr.markdown,
        "text": scr.markdown,
        "safety": safety_dict,
    }


def run(
    *,
    url: str,
    strip_html: bool = True,
    max_bytes: int = _DEFAULT_FETCH_BYTES,
    timeout_s: float = DEFAULT_TIMEOUT,
    use_jina_fallback: bool = True,
    prefer_jina: bool = False,
    use_browser_fallback: bool = True,
    use_scrapling_fallback: bool = True,
    min_content_chars: int = _MIN_USEFUL_CHARS,
) -> dict[str, Any]:
    if not url:
        return {"ok": False, "error": "url is required"}
    safety = evaluate_url(url)
    if not safety.is_allowed():
        return {
            "ok": False,
            "url": url,
            "error": f"{safety.reason}: {safety.note}",
            "safety": safety.to_dict(),
        }

    max_bytes = max(1024, min(int(max_bytes), HARD_FETCH_BYTES))
    min_content_chars = max(0, int(min_content_chars))
    started = time.monotonic()
    fallback_errors: list[str] = []

    if prefer_jina:
        jina, err = _fetch_jina_reader(
            safe_url=safety.url,
            max_bytes=max_bytes,
            timeout_s=timeout_s,
        )
        if jina is not None:
            jina["ok"] = True
            jina["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            jina["fallback_errors"] = fallback_errors
            jina["safety"] = safety.to_dict()
            return jina
        if err:
            fallback_errors.append(err)

    try:
        status, headers, body = http_get(safety.url, timeout=timeout_s)
    except Exception as exc:
        fallback_errors.append(f"direct_fetch: {type(exc).__name__}: {exc}")
        if use_jina_fallback:
            jina, err = _fetch_jina_reader(
                safe_url=safety.url,
                max_bytes=max_bytes,
                timeout_s=timeout_s,
            )
            if jina is not None:
                jina["ok"] = True
                jina["elapsed_ms"] = int((time.monotonic() - started) * 1000)
                jina["fallback_errors"] = fallback_errors
                jina["safety"] = safety.to_dict()
                return jina
            if err:
                fallback_errors.append(err)
        if use_browser_fallback:
            br_result = _try_browser_engine(
                safe_url=safety.url,
                timeout_s=timeout_s,
                min_content_chars=min_content_chars,
                fallback_errors=fallback_errors,
                safety_dict=safety.to_dict(),
                started_at=started,
            )
            if br_result is not None:
                return br_result
        if use_scrapling_fallback:
            scr_result = _try_scrapling(
                safe_url=safety.url,
                timeout_s=timeout_s,
                min_content_chars=min_content_chars,
                fallback_errors=fallback_errors,
                safety_dict=safety.to_dict(),
                started_at=started,
            )
            if scr_result is not None:
                return scr_result
        return {
            "ok": False,
            "url": safety.url,
            "error": f"{type(exc).__name__}: {exc}",
            "fallback_errors": fallback_errors,
            "safety": safety.to_dict(),
        }

    truncated = len(body) > max_bytes
    body = body[:max_bytes]
    content_type = (headers.get("content-type") or "").lower()
    markdown = ""
    title = ""
    fetch_method = "direct_text"
    if "text/html" in content_type or urlparse(safety.url).path.endswith((".html", ".htm")):
        decoded = body.decode("utf-8", errors="replace")
        if strip_html:
            extracted = _extract_html(
                decoded,
                url=safety.url,
                min_content_chars=min_content_chars,
            )
            markdown = extracted.text
            title = extracted.title
            fetch_method = extracted.method
            if extracted.error:
                fallback_errors.append(extracted.error)
        else:
            markdown = decoded
            fetch_method = "direct_html"
    else:
        markdown = body.decode("utf-8", errors="replace")

    should_try_jina = (
        use_jina_fallback
        and strip_html
        and (
            status >= 400
            or (
                ("text/html" in content_type or urlparse(safety.url).path.endswith((".html", ".htm")))
                and _looks_low_quality(markdown, min_chars=min_content_chars)
            )
        )
    )
    if should_try_jina:
        jina, err = _fetch_jina_reader(
            safe_url=safety.url,
            max_bytes=max_bytes,
            timeout_s=timeout_s,
        )
        if jina is not None and not _looks_low_quality(
            jina["markdown"], min_chars=min_content_chars,
        ):
            jina["ok"] = True
            jina["direct_status"] = status
            jina["direct_fetch_method"] = fetch_method
            jina["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            jina["fallback_errors"] = fallback_errors
            jina["safety"] = safety.to_dict()
            return jina
        if err:
            fallback_errors.append(err)

    # Browser engine tier — only kicks in when Jina also failed or returned
    # low-quality content. Cheap to skip if no engine is selected.
    needs_browser = (
        use_browser_fallback
        and strip_html
        and (status >= 400 or _looks_low_quality(markdown, min_chars=min_content_chars))
    )
    if needs_browser:
        br_result = _try_browser_engine(
            safe_url=safety.url,
            timeout_s=timeout_s,
            min_content_chars=min_content_chars,
            fallback_errors=fallback_errors,
            safety_dict=safety.to_dict(),
            started_at=started,
        )
        if br_result is not None:
            br_result["direct_status"] = status
            br_result["direct_fetch_method"] = fetch_method
            return br_result

    # Last-resort fallback: Scrapling (Camoufox / Playwright stealth).
    # Only trigger when direct + Jina both produced nothing usable.
    needs_scrapling = (
        use_scrapling_fallback
        and strip_html
        and (status >= 400 or _looks_low_quality(markdown, min_chars=min_content_chars))
    )
    if needs_scrapling:
        scr_result = _try_scrapling(
            safe_url=safety.url,
            timeout_s=timeout_s,
            min_content_chars=min_content_chars,
            fallback_errors=fallback_errors,
            safety_dict=safety.to_dict(),
            started_at=started,
        )
        if scr_result is not None:
            scr_result["direct_status"] = status
            scr_result["direct_fetch_method"] = fetch_method
            return scr_result

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "ok": 200 <= status < 400,
        "status": status,
        "url": safety.url,
        "title": title,
        "content_type": content_type,
        "bytes": len(body),
        "truncated": truncated,
        "fetch_method": fetch_method,
        "fallback_errors": fallback_errors,
        "elapsed_ms": elapsed_ms,
        "markdown": markdown,
        "text": markdown,
        "safety": safety.to_dict(),
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


def _payload_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    if key not in payload:
        return default
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


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
            strip_html=_payload_bool(payload, "strip_html", True),
            max_bytes=int(payload.get("max_bytes") or _DEFAULT_FETCH_BYTES),
            timeout_s=float(payload.get("timeout_s") or DEFAULT_TIMEOUT),
            use_jina_fallback=_payload_bool(payload, "use_jina_fallback", True),
            prefer_jina=_payload_bool(payload, "prefer_jina", False),
            use_browser_fallback=_payload_bool(payload, "use_browser_fallback", True),
            use_scrapling_fallback=_payload_bool(payload, "use_scrapling_fallback", True),
            min_content_chars=int(payload.get("min_content_chars") or _MIN_USEFUL_CHARS),
        )
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
