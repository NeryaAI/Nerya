"""Read a specific section of an SEC filing (10-K / 10-Q / 8-K).

CLI usage::

    python -m nerya.skills.builtin.sec_filings_skill.scripts.read_section \\
        --json '{"ticker": "AAPL", "form": "10-K",
                  "section": "risk_factors"}'

Implementation: lists filings via :class:`EquitiesClient.filings`, picks
the most recent matching `form`, then fetches its primary document URL
through the existing :mod:`fetch_url` script (which handles
direct-fetch + Jina Reader + Scrapling fallback). Sections are
extracted by markdown heading match against a small regex catalogue
(``risk_factors``, ``mdna``, ``business``, ``financial_statements``,
``controls``, ``legal``, ``cover``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from .....data.equities import EquitiesClient
from ...research.scripts import fetch_url as fetch_url_script


_SECTION_PATTERNS: dict[str, tuple[str, list[str]]] = {
    "risk_factors": (
        "Item 1A — Risk Factors",
        [r"item\s*1a[\.\s]*risk\s+factors", r"risk\s+factors"],
    ),
    "business": (
        "Item 1 — Business",
        [r"item\s*1[\.\s]*business"],
    ),
    "mdna": (
        "Item 7 — Management's Discussion and Analysis",
        [r"item\s*7[\.\s]*management.{0,40}discussion",
         r"management.{0,40}discussion\s+and\s+analysis"],
    ),
    "md_and_a": (
        "Item 7 — Management's Discussion and Analysis",
        [r"item\s*7[\.\s]*management.{0,40}discussion"],
    ),
    "financial_statements": (
        "Item 8 — Financial Statements",
        [r"item\s*8[\.\s]*financial\s+statements"],
    ),
    "controls": (
        "Item 9A — Controls and Procedures",
        [r"item\s*9a[\.\s]*controls\s+and\s+procedures"],
    ),
    "legal": (
        "Item 3 — Legal Proceedings",
        [r"item\s*3[\.\s]*legal\s+proceedings"],
    ),
    "cover": (
        "Filing Cover",
        [r"^.{0,400}", ],
    ),
}


def _select_filing(filings: list[dict[str, Any]], *,
                   form: str | None) -> dict[str, Any] | None:
    if not filings:
        return None
    if form:
        form_norm = form.upper().replace(" ", "")
        for f in filings:
            this_form = (f.get("form") or "").upper().replace(" ", "")
            if this_form == form_norm:
                return f
    return filings[0]


def _extract_section(markdown: str, *,
                     patterns: list[str]) -> tuple[str, str]:
    """Return ``(section_text, matched_heading)``.

    Strategy: find the first line that matches any pattern, then take
    everything until the next ``Item N`` heading (or end of doc).
    """
    if not markdown:
        return "", ""

    lines = markdown.splitlines()
    start_idx = -1
    matched = ""
    for i, line in enumerate(lines):
        clean = line.strip().lower()
        for pat in patterns:
            if re.search(pat, clean, re.IGNORECASE):
                start_idx = i
                matched = line.strip()
                break
        if start_idx >= 0:
            break
    if start_idx < 0:
        return "", ""

    end_idx = len(lines)
    next_item_re = re.compile(r"^\s*item\s+\d+[a-z]?\s*[\.\—:-]", re.IGNORECASE)
    for j in range(start_idx + 1, len(lines)):
        if next_item_re.match(lines[j]):
            end_idx = j
            break

    return "\n".join(lines[start_idx:end_idx]).strip(), matched


def run(
    *,
    ticker: str,
    form: str | None = "10-K",
    section: str = "risk_factors",
    accession_number: str | None = None,
    max_chars: int = 60_000,
) -> dict[str, Any]:
    if not ticker:
        return {"ok": False, "error": "ticker is required"}

    section_key = section.lower().strip()
    if section_key not in _SECTION_PATTERNS:
        return {
            "ok": False,
            "error": f"unsupported section: {section!r}; "
                     f"supported: {sorted(_SECTION_PATTERNS.keys())}",
        }
    label, patterns = _SECTION_PATTERNS[section_key]

    client = EquitiesClient()
    listing = client.filings(ticker, form=form, limit=10)
    listing_env = listing.get("_envelope") or {}
    dependency_guidance = None
    if isinstance(listing_env, dict) and (
        listing_env.get("missing_key")
        or "Financial Datasets API key is not configured" in str(
            listing_env.get("error") or "",
        )
    ):
        dependency_guidance = listing_env.get("setup_guidance")
        return {
            "ok": False,
            "ticker": ticker.upper(),
            "form": form,
            "error": str(listing_env.get("error") or "dependency missing"),
            "dependency_guidance": dependency_guidance,
            "_envelope": listing_env,
            "source_url": listing.get("source_url"),
        }
    items = (listing.get("data") or {}).get("filings") or []
    if not isinstance(items, list):
        items = []

    if accession_number:
        chosen = next(
            (f for f in items
             if (f.get("accession_number") or f.get("accession"))
             == accession_number),
            None,
        )
    else:
        chosen = _select_filing(items, form=form)

    if not chosen:
        return {
            "ok": False,
            "ticker": ticker.upper(),
            "form": form,
            "error": "no matching filing found",
            "_envelope": listing.get("_envelope"),
            "source_url": listing.get("source_url"),
            "dependency_guidance": dependency_guidance,
        }

    doc_url = (chosen.get("url") or chosen.get("primary_document") or "").strip()
    if not doc_url:
        return {
            "ok": False,
            "ticker": ticker.upper(),
            "form": form,
            "error": "filing entry has no document URL",
            "filing": chosen,
            "_envelope": listing.get("_envelope"),
            "dependency_guidance": dependency_guidance,
        }

    fetched = fetch_url_script.run(
        url=doc_url,
        strip_html=True,
        max_bytes=2_000_000,
        use_jina_fallback=True,
        use_scrapling_fallback=True,
    )
    markdown = fetched.get("markdown") or fetched.get("text") or ""
    section_text, matched_heading = _extract_section(
        markdown, patterns=patterns,
    )
    if section_text and len(section_text) > max_chars:
        section_text = section_text[:max_chars] + "\n\n[truncated]"

    return {
        "ok": bool(section_text),
        "ticker": ticker.upper(),
        "form": chosen.get("form") or form,
        "filing_date": chosen.get("filing_date"),
        "report_period": chosen.get("report_period"),
        "accession_number": chosen.get("accession_number") or chosen.get("accession"),
        "section": section_key,
        "section_label": label,
        "matched_heading": matched_heading,
        "content_markdown": section_text,
        "doc_url": doc_url,
        "source_url": listing.get("source_url"),
        "fetch_method": fetched.get("fetch_method"),
        "fetch_fallback_errors": fetched.get("fallback_errors"),
        "dependency_guidance": dependency_guidance,
        "_envelope": listing.get("_envelope"),
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
    parser.add_argument("--ticker", dest="ticker", default=None)
    parser.add_argument("--form", dest="form", default=None)
    parser.add_argument("--section", dest="section", default=None)
    args = parser.parse_args()

    payload = _load_payload(args)
    ticker = (args.ticker or payload.get("ticker") or "").strip().upper()
    form = args.form or payload.get("form") or "10-K"
    section = args.section or payload.get("section") or "risk_factors"

    try:
        result = run(
            ticker=ticker,
            form=form,
            section=section,
            accession_number=payload.get("accession_number"),
            max_chars=int(payload.get("max_chars") or 60_000),
        )
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
