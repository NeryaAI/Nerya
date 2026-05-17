"""Scrapling fetch adapter — last-resort scraper for blocked / JS-heavy pages.

Wraps https://github.com/D4Vinci/Scrapling so the rest of the research
pipeline can call it through a uniform ``fetch(url) -> dict`` interface.

Scrapling provides three relevant fetcher tiers (priority order):

1. ``StealthyFetcher`` — Camoufox-based (headless Firefox with hardened
   anti-bot defenses). Best for Cloudflare / Akamai / etc.
2. ``DynamicFetcher`` — Playwright Chromium for JS-rendered pages.
3. ``Fetcher`` — plain HTTP (cheap fallback).

The agent should call this only AFTER direct fetch + Jina Reader have
both failed or returned low-quality content (the ``fetch_url`` script
handles that orchestration).

Install (user runs, not Nerya runtime)::

    pip install "scrapling[fetchers]"
    scrapling install      # downloads Camoufox & Playwright browsers

If Scrapling is not installed, all calls return
``{"ok": False, "error": "scrapling not installed", ...}`` so the
orchestrator can degrade gracefully.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_SCRAPLING_INSTALL_HINT = (
    "scrapling not installed — run "
    "`pip install \"scrapling[fetchers]\" && scrapling install` to enable "
    "stealth scraping fallback"
)


def _normalize_markdown(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _extract_text(page) -> tuple[str, str]:
    """Pull a (title, text/markdown) pair from a Scrapling response page."""
    try:
        title = (page.css_first("title::text") or "").strip()
    except Exception:
        title = ""
    text = ""

    # Try markdownify on the rendered HTML for prettier output.
    raw_html = ""
    try:
        raw_html = page.body or ""
    except Exception:
        try:
            raw_html = page.html or ""
        except Exception:
            raw_html = ""

    if raw_html:
        try:
            from markdownify import markdownify as md  # type: ignore
            cleaned = re.sub(
                r"<(script|style|noscript|svg|head)\b[^>]*>.*?</\1>",
                "",
                raw_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            text = md(cleaned, heading_style="ATX")
        except Exception:
            text = ""

    if not text:
        # Fall back to Scrapling's own text extraction.
        try:
            text = page.get_all_text(separator="\n", strip=True) or ""
        except Exception:
            try:
                text = (page.text or "").strip()
            except Exception:
                text = ""

    return title, _normalize_markdown(text)


@dataclass
class ScraplingResult:
    ok: bool
    status: int = 0
    url: str = ""
    title: str = ""
    markdown: str = ""
    bytes: int = 0
    fetch_method: str = ""
    fallback_errors: list[str] | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "url": self.url,
            "title": self.title,
            "markdown": self.markdown,
            "text": self.markdown,
            "bytes": self.bytes,
            "fetch_method": self.fetch_method,
            "fallback_errors": self.fallback_errors or [],
            **({"error": self.error} if self.error else {}),
        }


def fetch(
    *,
    url: str,
    timeout_s: float = 30.0,
    headless: bool = True,
    prefer: str = "auto",
    google_search: bool = True,
    network_idle: bool = True,
) -> ScraplingResult:
    """Fetch ``url`` through Scrapling with sensible defaults.

    ``prefer``:
        - ``"stealth"`` — Camoufox / StealthyFetcher only
        - ``"dynamic"`` — Playwright DynamicFetcher only
        - ``"plain"`` — vanilla Fetcher only
        - ``"auto"`` — try stealth → dynamic → plain (default)
    """

    fallback_errors: list[str] = []

    try:
        from scrapling.fetchers import (  # type: ignore[import-not-found]
            StealthyFetcher, DynamicFetcher, Fetcher,
        )
    except Exception as exc:  # noqa: BLE001
        return ScraplingResult(
            ok=False,
            url=url,
            fetch_method="scrapling_unavailable",
            error=f"{_SCRAPLING_INSTALL_HINT} ({type(exc).__name__}: {exc})",
        )

    chain: list[tuple[str, Any]] = []
    if prefer in ("auto", "stealth"):
        chain.append(("stealth", StealthyFetcher))
    if prefer in ("auto", "dynamic"):
        chain.append(("dynamic", DynamicFetcher))
    if prefer in ("auto", "plain"):
        chain.append(("plain", Fetcher))
    if not chain:
        return ScraplingResult(
            ok=False, url=url,
            fetch_method="scrapling_unsupported_mode",
            error=f"unsupported prefer={prefer!r}",
        )

    for label, fetcher_cls in chain:
        try:
            kwargs: dict[str, Any] = {}
            if label == "stealth":
                kwargs.update(dict(
                    headless=headless,
                    network_idle=network_idle,
                    google_search=google_search,
                    timeout=int(timeout_s * 1000),
                ))
            elif label == "dynamic":
                kwargs.update(dict(
                    headless=headless,
                    network_idle=network_idle,
                    timeout=int(timeout_s * 1000),
                ))
            else:
                kwargs.update(dict(timeout=timeout_s))

            page = fetcher_cls.fetch(url, **kwargs)
        except Exception as exc:  # noqa: BLE001
            fallback_errors.append(f"scrapling_{label}: {type(exc).__name__}: {exc}")
            continue

        try:
            status = int(getattr(page, "status", 0) or 0)
        except Exception:
            status = 0

        if status and status >= 400:
            fallback_errors.append(f"scrapling_{label}: HTTP {status}")
            continue

        title, markdown = _extract_text(page)
        if not markdown.strip():
            fallback_errors.append(f"scrapling_{label}: empty content")
            continue

        try:
            byte_count = len((page.body or "").encode("utf-8"))
        except Exception:
            byte_count = len(markdown.encode("utf-8"))

        return ScraplingResult(
            ok=True,
            status=status or 200,
            url=url,
            title=title,
            markdown=markdown,
            bytes=byte_count,
            fetch_method=f"scrapling_{label}",
            fallback_errors=fallback_errors,
        )

    return ScraplingResult(
        ok=False,
        url=url,
        fetch_method="scrapling_chain_exhausted",
        fallback_errors=fallback_errors,
        error="all scrapling tiers failed",
    )


__all__ = ["fetch", "ScraplingResult"]
