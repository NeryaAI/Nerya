"""Tool result compaction.

Reduces large tool outputs to a structured summary that preserves
audit-critical fields (order ids, account ids, strategy ids, risk
reasons, error codes, timestamps) and stores the raw payload behind
a reference id retrievable later.

Public surface:

- :func:`compact_tool_result(name, output, *, size_threshold=...)` ->
  ``CompactedResult`` with summary, kept fields, dropped bytes, rule id,
  and an optional raw reference.
- :func:`stats()` returns counters useful for tracing.

The reducers are intentionally tolerant: any malformed input falls
through to the generic JSON reducer or returns the original output
unchanged. Audit-critical fields (see :data:`AUDIT_FIELDS`) are never
elided from the summary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional


# Fields we will never drop from a compacted summary. Order is significant
# for the human-readable summary.
AUDIT_FIELDS: tuple[str, ...] = (
    "order_id", "client_order_id", "account_id", "strategy_id",
    "proposal_id", "task_id", "run_id", "session_id", "approval_id",
    "next_required_action", "proposal_paths", "saved_path", "capture_paths",
    "risk_reason",
    "rejection_reason", "error", "error_code", "exchange_error_code", "ts",
    "timestamp", "symbol", "side", "qty", "price", "status",
)

# Soft threshold below which compaction is skipped (already small enough).
_DEFAULT_SIZE_THRESHOLD = 2_048

# Hard upper bound on the kept ``summary`` string. Anything larger triggers
# a second-pass tail truncation.
_SUMMARY_MAX_CHARS = 4_096
_DOC_SNIPPET_MAX_CHARS = 1_200
_JSON_EVIDENCE_TEXT_MAX_CHARS = 65_536
_JSON_EVIDENCE_STRING_MAX_CHARS = 220
_JSON_EVIDENCE_KEY_LIMIT = 16
_JSON_EVIDENCE_LIST_LIMIT = 8
_JSON_EVIDENCE_LEAF_LIMIT = 32
_NUMERIC_EVIDENCE_RE = re.compile(
    r"(\$|€|£|¥|%|\b20\d{2}\b|\b\d+(?:[.,]\d+)?\s*"
    r"(?:billion|million|trillion|thousand|bps|x|times)\b)",
    re.I,
)
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_SENSITIVE_JSON_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|bearer|cookie|credential|password|secret|token)",
    re.I,
)
_COMPACTION_INTERNAL_KEYS = frozenset({
    "raw",
    "traceback",
    "stack_trace",
    "debug",
    "padding",
})
_TOP_LEVEL_FIELD_LIMIT = 48
_TOP_LEVEL_LIST_LIMIT = 8
_TOP_LEVEL_STRING_LIMIT = 800


@dataclass
class CompactedResult:
    rule_id: str
    summary: str
    kept: dict[str, Any] = field(default_factory=dict)
    raw_ref: Optional[str] = None
    original_bytes: int = 0
    compacted_bytes: int = 0
    skipped: bool = False
    skipped_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "summary": self.summary,
            "kept": dict(self.kept),
            "raw_ref": self.raw_ref,
            "original_bytes": self.original_bytes,
            "compacted_bytes": self.compacted_bytes,
            "skipped": self.skipped,
            "skipped_reason": self.skipped_reason,
        }


_STATS: dict[str, int] = {
    "compacted": 0,
    "skipped_small": 0,
    "skipped_no_rule": 0,
    "bytes_saved": 0,
}


def stats() -> dict[str, int]:
    return dict(_STATS)


def _bytes_of(value: Any) -> int:
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8", errors="ignore"))
    try:
        return len(json.dumps(value, default=str).encode("utf-8", errors="ignore"))
    except Exception:
        return len(repr(value).encode("utf-8", errors="ignore"))


def _truncate(text: str, limit: int = _SUMMARY_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit - 32]
    return head + "\n…[truncated]"


def _clean_document_line(line: str) -> str:
    line = " ".join(str(line or "").replace("\xa0", " ").split())
    if not line or _TABLE_SEPARATOR_RE.match(line):
        return ""
    if line.startswith("![") or line.startswith("<svg"):
        return ""
    return line


def _document_line_score(line: str) -> int:
    if len(line) < 16:
        return 0
    score = 0
    has_digit = any(ch.isdigit() for ch in line)
    if _NUMERIC_EVIDENCE_RE.search(line):
        score += 5
    if "|" in line and has_digit:
        score += 4
    if has_digit:
        score += 2
    if 40 <= len(line) <= 260:
        score += 1
    lower = line.lower()
    if "http" in lower:
        score -= min(3, lower.count("http"))
    tokens = [token for token in re.split(r"\W+", lower) if token]
    if len(tokens) >= 8 and len(set(tokens)) <= max(3, len(tokens) // 5):
        score -= 4
    return score


def _extract_document_snippet(text: str, *, limit: int = _DOC_SNIPPET_MAX_CHARS) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    candidates: list[tuple[int, int, str]] = []
    fallback_lines: list[str] = []
    for idx, raw_line in enumerate(text.splitlines()):
        line = _clean_document_line(raw_line)
        if not line:
            continue
        if len(fallback_lines) < 20:
            fallback_lines.append(line)
        score = _document_line_score(line)
        if score > 0:
            candidates.append((score, idx, line))
    if candidates:
        selected = sorted(
            sorted(candidates, key=lambda item: (-item[0], item[1]))[:12],
            key=lambda item: item[1],
        )
        lines: list[str] = []
        seen: set[str] = set()
        for _, _, line in selected:
            if line in seen:
                continue
            seen.add(line)
            prospective = "\n".join([*lines, line]) if lines else line
            if len(prospective) > limit:
                break
            lines.append(line)
        if lines:
            return "\n".join(lines)
    return _truncate(" ".join(fallback_lines), limit)


def compact_json_evidence_preview(value: Any) -> Any:
    """Return a bounded JSON preview suitable for compact evidence.

    This is intentionally generic: public API responses often encode the
    important answer as compact JSON rather than prose. Preserve enough scalar
    leaves for final synthesis without flooding the transcript.
    """

    leaf_budget = [_JSON_EVIDENCE_LEAF_LIMIT]

    def compact(node: Any, depth: int = 0) -> Any:
        if leaf_budget[0] <= 0:
            return "[truncated]"
        if isinstance(node, dict):
            if depth >= 4:
                return "[object]"
            kept: dict[str, Any] = {}
            for key, item in list(node.items())[:_JSON_EVIDENCE_KEY_LIMIT]:
                key_text = str(key)
                if _SENSITIVE_JSON_KEY_RE.search(key_text):
                    kept[key_text] = "[redacted]"
                    continue
                kept[key_text] = compact(item, depth + 1)
            if len(node) > len(kept):
                kept["_truncated_keys"] = len(node) - len(kept)
            return kept
        if isinstance(node, list):
            if depth >= 4:
                return f"[list:{len(node)}]"
            return [compact(item, depth + 1) for item in node[:_JSON_EVIDENCE_LIST_LIMIT]]
        leaf_budget[0] -= 1
        if isinstance(node, str):
            return _truncate(node, _JSON_EVIDENCE_STRING_MAX_CHARS)
        if isinstance(node, (int, float, bool)) or node is None:
            return node
        return _truncate(str(node), _JSON_EVIDENCE_STRING_MAX_CHARS)

    return compact(value)


def json_evidence_from_text(
    text: str,
    *,
    content_type: str = "",
    url: str = "",
) -> Any | None:
    stripped = str(text or "").strip()
    if not stripped or len(stripped) > _JSON_EVIDENCE_TEXT_MAX_CHARS:
        return None
    lower_type = str(content_type or "").lower()
    lower_url = str(url or "").lower()
    looks_json = (
        "json" in lower_type
        or lower_url.endswith(".json")
        or stripped.startswith(("{", "["))
    )
    if not looks_json:
        return None
    try:
        parsed = json.loads(stripped)
    except Exception:
        return None
    return compact_json_evidence_preview(parsed)


def _extract_audit(blob: Any) -> dict[str, Any]:
    """Walk ``blob`` (dict/list) and lift any AUDIT_FIELDS to a flat dict.

    The first occurrence wins. Lists are walked shallowly so the first
    row of an orders/fills payload still surfaces order_id/status.
    """

    kept: dict[str, Any] = {}

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in AUDIT_FIELDS and k not in kept:
                    kept[k] = v
                if isinstance(v, (dict, list)):
                    _walk(v)
        elif isinstance(node, list):
            for item in node[:5]:
                _walk(item)

    _walk(blob)
    return kept


def _compact_risk_decision(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    kept: dict[str, Any] = {}
    for key in (
        "decision",
        "estimated_notional_usd",
        "risk_evaluation_id",
        "reservation_blocked_usd",
        "shadow_only",
        "promotion_state",
    ):
        raw = value.get(key)
        if raw not in (None, "", [], {}):
            kept[key] = raw
    reasons = value.get("reasons")
    if isinstance(reasons, list):
        kept["reasons"] = reasons[:8]
    elif reasons not in (None, "", [], {}):
        kept["reasons"] = reasons
    fix_hints = value.get("fix_hints")
    if isinstance(fix_hints, list) and fix_hints:
        kept["fix_hints"] = fix_hints[:5]
    return kept or None


def _compact_normalization(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    kept: dict[str, Any] = {}
    sizing = value.get("sizing")
    if isinstance(sizing, dict):
        sizing_kept = {
            key: sizing.get(key)
            for key in (
                "method",
                "size_pct_nav",
                "max_size_pct_nav",
                "nav_usd",
                "nav_source",
            )
            if sizing.get(key) not in (None, "", [], {})
        }
        if sizing_kept:
            kept["sizing"] = sizing_kept
    return kept or None


def _extract_structured_audit(output: Any) -> dict[str, Any]:
    if not isinstance(output, dict):
        return {}
    kept: dict[str, Any] = {}
    risk_decision = _compact_risk_decision(output.get("risk_decision"))
    if risk_decision:
        kept["risk_decision"] = risk_decision
    normalization = _compact_normalization(output.get("normalization"))
    if normalization:
        kept["normalization"] = normalization
    return kept


def _compact_top_level_value(value: Any) -> Any:
    """Keep a bounded, redacted preview without knowing the producer schema."""

    if isinstance(value, str):
        return _truncate(value, _TOP_LEVEL_STRING_LIMIT)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return compact_json_evidence_preview(value)
    if isinstance(value, (list, tuple)):
        items = [
            compact_json_evidence_preview(item)
            for item in value[:_TOP_LEVEL_LIST_LIMIT]
        ]
        if len(value) > _TOP_LEVEL_LIST_LIMIT:
            items.append({"_truncated_items": len(value) - _TOP_LEVEL_LIST_LIMIT})
        return items
    return _truncate(str(value), _TOP_LEVEL_STRING_LIMIT)


def _compact_top_level_fields(output: Any) -> dict[str, Any]:
    """Preserve useful top-level state while bounding arbitrary JSON output."""

    if not isinstance(output, dict):
        return {}
    entries: list[tuple[str, Any]] = []
    for raw_key, value in output.items():
        key = str(raw_key)
        normalized = key.lower().replace("-", "_")
        if (
            normalized in _COMPACTION_INTERNAL_KEYS
            or _SENSITIVE_JSON_KEY_RE.search(key)
            or value in (None, "", [], {})
        ):
            continue
        entries.append((key, value))

    # Scalars carry status and identifiers most often; keep them before bulky
    # collections so a producer cannot hide its outcome behind a large list.
    entries.sort(
        key=lambda item: not isinstance(item[1], (str, int, float, bool))
    )
    kept: dict[str, Any] = {}
    for key, value in entries[:_TOP_LEVEL_FIELD_LIMIT]:
        kept[key] = _compact_top_level_value(value)
    if len(entries) > _TOP_LEVEL_FIELD_LIMIT:
        kept["_truncated_fields"] = len(entries) - _TOP_LEVEL_FIELD_LIMIT
    return kept


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------


def _compact_search_results(value: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    kept: list[dict[str, Any]] = []
    for row in value[:limit]:
        if not isinstance(row, dict):
            continue
        kept.append({
            "title": str(row.get("title") or ""),
            "url": str(row.get("url") or ""),
            "snippet": _truncate(str(row.get("snippet") or ""), 500),
            "source": str(row.get("source") or ""),
            "engine": str(row.get("engine") or ""),
            "underlying_engine": str(row.get("underlying_engine") or ""),
        })
    return kept


def _reduce_shell_git(name: str, output: Any) -> Optional[CompactedResult]:
    if not isinstance(output, str):
        return None
    if "git" not in name and "git status" not in output[:64].lower():
        return None
    lines = output.splitlines()
    branch = ""
    for line in lines[:5]:
        if line.lower().startswith("on branch"):
            branch = line[10:].strip()
            break
    counts = {"changed": 0, "staged": 0, "untracked": 0}
    for line in lines:
        s = line.strip()
        if s.startswith("modified:"):
            counts["changed"] += 1
        elif s.startswith("new file:"):
            counts["staged"] += 1
        elif s.startswith("?"):
            counts["untracked"] += 1
    summary = (
        f"git status (branch={branch or '?'}): "
        f"changed={counts['changed']}, staged={counts['staged']}, "
        f"untracked={counts['untracked']}"
    )
    return CompactedResult(
        rule_id="shell.git_status",
        summary=summary,
        kept={"branch": branch, "counts": counts},
        original_bytes=_bytes_of(output),
    )


def _reduce_shell_pytest(name: str, output: Any) -> Optional[CompactedResult]:
    if not isinstance(output, str):
        return None
    if "pytest" not in name and " passed" not in output[-512:] and " failed" not in output[-512:]:
        return None
    fail_count = len(re.findall(r"FAILED\s+", output))
    pass_match = re.search(r"(\d+)\s+passed", output)
    fail_match = re.search(r"(\d+)\s+failed", output)
    err_match = re.search(r"(\d+)\s+error", output)
    passed = int(pass_match.group(1)) if pass_match else 0
    failed = int(fail_match.group(1)) if fail_match else fail_count
    errors = int(err_match.group(1)) if err_match else 0
    # extract first traceback if any
    first_tb = ""
    tb_match = re.search(r"={3,}\s+FAILURES\s+={3,}.*?(?=\n=={3,}|\Z)", output, re.S)
    if tb_match:
        first_tb = _truncate(tb_match.group(0), 1200)
    summary = (
        f"pytest: passed={passed}, failed={failed}, errors={errors}"
    )
    kept: dict[str, Any] = {"passed": passed, "failed": failed, "errors": errors}
    if first_tb:
        kept["first_failure"] = first_tb
    return CompactedResult(
        rule_id="shell.pytest",
        summary=summary,
        kept=kept,
        original_bytes=_bytes_of(output),
    )


def _reduce_web_fetch(name: str, output: Any) -> Optional[CompactedResult]:
    if "web" not in name and "fetch" not in name:
        return None
    if not isinstance(output, dict):
        return None
    documents = output.get("documents")
    if isinstance(documents, list):
        kept_docs: list[dict[str, Any]] = []
        total_chars = 0
        ok_count = 0
        failed_count = 0
        for doc in documents[:8]:
            if not isinstance(doc, dict):
                continue
            body = str(
                doc.get("markdown")
                or doc.get("text")
                or doc.get("content")
                or doc.get("snippet")
                or ""
            )
            total_chars += len(body)
            if doc.get("ok"):
                ok_count += 1
            else:
                failed_count += 1
            kept_docs.append({
                "rank": doc.get("rank"),
                "title": str(doc.get("title") or ""),
                "url": str(doc.get("url") or ""),
                "ok": bool(doc.get("ok")),
                "status": doc.get("status"),
                "fetch_method": str(doc.get("fetch_method") or ""),
                "bytes": doc.get("bytes") or 0,
                "source": str(doc.get("source") or ""),
                "snippet": _extract_document_snippet(body),
                "fallback_errors": list(doc.get("fallback_errors") or [])[:5],
            })
        if len(documents) > len(kept_docs):
            failed_count += max(0, len(documents) - len(kept_docs) - ok_count)
        query = str(output.get("query") or "")
        search_results: list[dict[str, Any]] = []
        search = output.get("search")
        if isinstance(search, dict):
            search_results = _compact_search_results(search.get("results"))
        top_url = str((search_results[0] if search_results else {}).get("url") or "")
        summary = (
            f"web_search_fetch: {query or 'results'} "
            f"docs={len(documents)} ok={ok_count} failed={failed_count} "
            f"({total_chars} chars)"
        )
        if not kept_docs and search_results:
            summary += f" search_results={len(search_results)} top={top_url}"
        kept = {
            "ok": bool(output.get("ok")),
            "query": query,
            "count": output.get("count"),
            "attempted": output.get("attempted"),
            "documents": kept_docs,
        }
        if isinstance(search, dict):
            kept["search"] = {
                "ok": bool(search.get("ok")),
                "engine": search.get("engine"),
                "count": search.get("count"),
                "results": search_results,
                "fallback_errors": list(search.get("fallback_errors") or [])[:5],
            }
        return CompactedResult(
            rule_id="research.web_search_fetch",
            summary=summary,
            kept=kept,
            original_bytes=_bytes_of(output),
        )

    results = output.get("results")
    if "search" in name and isinstance(results, list):
        kept_results = _compact_search_results(results)
        query = str(output.get("query") or "")
        top_url = str((kept_results[0] if kept_results else {}).get("url") or "")
        summary = (
            f"web_search: {query or 'results'} "
            f"results={output.get('count', len(results))}"
        )
        if top_url:
            summary += f" top={top_url}"
        return CompactedResult(
            rule_id="research.web_search",
            summary=summary,
            kept={
                "ok": bool(output.get("ok")),
                "query": query,
                "engine": output.get("engine"),
                "count": output.get("count"),
                "results": kept_results,
                "fallback_errors": list(output.get("fallback_errors") or [])[:5],
            },
            original_bytes=_bytes_of(output),
        )

    title = str(output.get("title") or "")
    url = str(output.get("url") or output.get("source_url") or "")
    text = str(output.get("text") or output.get("markdown") or output.get("content") or "")
    content_type = str(output.get("content_type") or "")
    headings = output.get("headings") or []
    snippet = _extract_document_snippet(text)
    status = output.get("status")
    fetch_method = str(output.get("fetch_method") or "")
    ok = bool(output.get("ok", True))
    response_json = json_evidence_from_text(
        text,
        content_type=content_type,
        url=url,
    )
    suffix = []
    if status is not None:
        suffix.append(f"status={status}")
    if fetch_method:
        suffix.append(f"method={fetch_method}")
    if not ok:
        suffix.append("ok=false")
    detail = ", " + ", ".join(suffix) if suffix else ""
    summary = f"web_fetch: {title or url} ({len(text)} chars{detail})"
    kept = {
        "ok": ok,
        "status": status,
        "fetch_method": fetch_method,
        "content_type": content_type,
        "error": output.get("error"),
        "title": title,
        "url": url,
        "headings": list(headings)[:20],
        "snippet": snippet,
        "fallback_errors": list(output.get("fallback_errors") or [])[:10],
    }
    if response_json is not None:
        kept["response_json"] = response_json
    return CompactedResult(
        rule_id="research.web_fetch",
        summary=summary,
        kept=kept,
        original_bytes=_bytes_of(output),
    )


def _reduce_candles(name: str, output: Any) -> Optional[CompactedResult]:
    is_candle_tool = (
        "candle" in name
        or "kline" in name
        or "ohlcv" in name
        or name == "market_data"
        or name.endswith(".market_data")
    )
    if not is_candle_tool:
        return None
    rows: Optional[list[Any]] = None
    symbol = ""
    market = ""
    venue = ""
    interval = ""
    if isinstance(output, dict):
        rows = output.get("rows") or output.get("candles") or output.get("ohlcv")
        symbol = str(output.get("symbol") or output.get("market") or "")
        market = str(output.get("market") or symbol)
        venue = str(output.get("venue") or output.get("source") or output.get("provider") or "")
        interval = str(output.get("interval") or "")
    elif isinstance(output, list):
        rows = output
    if not isinstance(rows, list) or not rows:
        return None
    row_count = len(rows)
    if isinstance(output, dict):
        try:
            row_count = int(output.get("count") or len(rows))
        except (TypeError, ValueError):
            row_count = len(rows)
    first = rows[0]
    last = rows[-1]

    def _row_ts(row: Any) -> Any:
        if isinstance(row, dict):
            return (
                row.get("timestamp")
                or row.get("ts")
                or row.get("time")
                or row.get("datetime")
                or row.get("date")
            )
        if isinstance(row, (list, tuple)) and row:
            return row[0]
        return None

    def _iso_utc(value: Any) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            seconds = float(value)
            if seconds > 1_000_000_000_000:
                seconds /= 1000.0
            try:
                return (
                    datetime.fromtimestamp(seconds, tz=timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except (OSError, OverflowError, ValueError):
                return None
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z") and "T" in text:
            return text
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (
            parsed.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    first_ts = _row_ts(first)
    last_ts = _row_ts(last)
    first_iso = (
        output.get("first_timestamp_iso")
        if isinstance(output, dict)
        else None
    ) or _iso_utc(first_ts)
    last_iso = (
        output.get("last_timestamp_iso")
        if isinstance(output, dict)
        else None
    ) or _iso_utc(last_ts)
    label = market or symbol or "?"
    source = venue or "?"
    summary = (
        f"market_data candles: source={source} market={label} "
        f"interval={interval or '?'} rows={row_count}"
    )
    if first_iso or last_iso:
        summary += f" first={first_iso or '?'} last={last_iso or '?'}"
    elif first_ts or last_ts:
        summary += f" first={first_ts or '?'} last={last_ts or '?'}"
    kept = {
        "venue": venue,
        "market": market,
        "symbol": symbol,
        "interval": interval,
        "count": row_count,
        "rows": len(rows),
        "rows_sample": rows[:5],
        "first": first,
        "last": last,
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "first_timestamp_iso": first_iso,
        "last_timestamp_iso": last_iso,
    }
    if isinstance(output, dict):
        if isinstance(output.get("coverage"), dict):
            kept["coverage"] = output["coverage"]
        if isinstance(output.get("features"), dict):
            kept["features"] = output["features"]
        context = str(output.get("context") or "").strip()
        if context:
            kept["context"] = _truncate(context, 1_200)
    return CompactedResult(
        rule_id="trading.candles",
        summary=summary,
        kept=kept,
        original_bytes=_bytes_of(output),
    )


def _reduce_orders(name: str, output: Any) -> Optional[CompactedResult]:
    if "order" not in name and "fill" not in name:
        return None
    rows: Optional[list[Any]] = None
    if isinstance(output, dict):
        rows = output.get("orders") or output.get("fills") or output.get("rows")
    elif isinstance(output, list):
        rows = output
    if not isinstance(rows, list):
        return None
    by_status: dict[str, int] = {}
    rejections: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        st = str(row.get("status") or "?").lower()
        by_status[st] = by_status.get(st, 0) + 1
        if st in ("rejected", "error") and row.get("rejection_reason"):
            rejections.append(str(row["rejection_reason"]))
    audit = _extract_audit(rows)
    summary = f"orders/fills: total={len(rows)}, by_status={by_status}"
    kept = {
        "total": len(rows),
        "by_status": by_status,
        "rejections_top": rejections[:5],
        "newest": rows[-3:],
        **audit,
    }
    return CompactedResult(
        rule_id="trading.orders",
        summary=summary,
        kept=kept,
        original_bytes=_bytes_of(output),
    )


def _reduce_backtest(name: str, output: Any) -> Optional[CompactedResult]:
    if "backtest" not in name:
        return None
    if not isinstance(output, dict):
        return None
    metrics = output.get("metrics") or {}
    window = output.get("window") or output.get("date_range") or ""
    symbols = output.get("symbols") or []
    errors = output.get("errors") or []
    artifact_refs = output.get("artifact_refs") or []
    metric_keys = list(metrics.keys()) if isinstance(metrics, dict) else []
    error_count = len(errors) if isinstance(errors, (dict, list, tuple)) else int(bool(errors))
    summary = f"backtest: metrics={metric_keys}, errors={error_count}"
    kept = _compact_top_level_fields(output)
    # Keep the report's common collections bounded even when a producer adds
    # thousands of metrics, errors, or symbols.
    for key, value in (
        ("metrics", metrics),
        ("window", window),
        ("symbols", symbols),
        ("errors", errors),
        ("artifact_refs", artifact_refs),
    ):
        if value not in (None, "", [], {}):
            kept[key] = _compact_top_level_value(value)
    for key in (
        "out_dir",
        "backtest_dir",
        "metrics_path",
        "raw_metrics_file",
        "report_path",
        "chart_path",
        "equity_path",
        "trades_path",
        "result_path",
    ):
        value = output.get(key)
        if value:
            kept[key] = value
    return CompactedResult(
        rule_id="backtest.report",
        summary=summary,
        kept=kept,
        original_bytes=_bytes_of(output),
    )


def _reduce_script_run(name: str, output: Any) -> Optional[CompactedResult]:
    if "script_run" not in name or not isinstance(output, dict):
        return None
    if "stdout" not in output or "exit_code" not in output:
        return None
    stdout = str(output.get("stdout") or "")
    stderr = str(output.get("stderr") or "")
    skill_id = str(output.get("skill_id") or "")
    script_name = str(output.get("name") or "")
    exit_code = output.get("exit_code")
    summary = (
        f"script_run: {skill_id or '?'}/{script_name or '?'} "
        f"exit={exit_code} stdout={len(stdout)} stderr={len(stderr)}"
    )
    kept: dict[str, Any] = {
        "skill_id": skill_id,
        "name": script_name,
        "exit_code": exit_code,
        "duration_sec": output.get("duration_sec"),
        "stderr": _truncate(stderr, 1200),
    }
    parsed: Any = output.get("stdout_json")
    if parsed is None and stdout.strip():
        try:
            parsed = json.loads(stdout)
        except Exception:
            parsed = None
    if isinstance(parsed, dict):
        kept["stdout_json"] = _compact_script_stdout_json(parsed)
        count = parsed.get("count")
        if count is not None:
            summary += f" count={count}"
        if parsed.get("ok") is False:
            summary += " ok=false"
    elif isinstance(parsed, list):
        kept["stdout_json"] = {
            "count": len(parsed),
            "items": parsed[:8],
        }
        summary += f" count={len(parsed)}"
    else:
        kept["stdout"] = _truncate(stdout, 2400)
    return CompactedResult(
        rule_id="skill.script_run",
        summary=summary,
        kept=kept,
        original_bytes=_bytes_of(output),
    )


def _compact_script_stdout_json(data: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key in ("ok", "source", "sources", "tickers", "count", "errors", "notes"):
        if key in data:
            kept[key] = data.get(key)
    time_filter = data.get("time_filter")
    if isinstance(time_filter, dict):
        kept["time_filter"] = _compact_script_metadata_map(time_filter)
    items = data.get("items")
    if isinstance(items, list):
        kept["items"] = [
            {
                key: item.get(key)
                for key in ("source", "title", "summary", "url", "published_at", "tickers")
                if isinstance(item, dict) and key in item
            }
            for item in items[:8]
            if isinstance(item, dict)
        ]
    return kept


def _compact_script_metadata_map(data: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str):
            kept[key] = _truncate(value, 240)
        elif isinstance(value, (int, float, bool)) or value is None:
            kept[key] = value
    return kept


def _compact_connector_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("id", "runtime"):
        value = row.get(key)
        if value not in (None, "", [], {}):
            out[key] = value
    status = row.get("credential_status")
    if isinstance(status, dict):
        out["setup_status"] = {
            key: status.get(key)
            for key in (
                "required",
                "status",
                "configured",
                "should_retry",
            )
            if status.get(key) not in (None, "", [], {})
        }
    return out


def _reduce_connector_list(name: str, output: Any) -> Optional[CompactedResult]:
    if "connector_list" not in name or not isinstance(output, dict):
        return None
    connectors = output.get("connectors")
    if not isinstance(connectors, list):
        return None

    count = output.get("count", len(connectors))
    sample = [
        _compact_connector_row(row)
        for row in connectors[:8]
        if isinstance(row, dict)
    ]
    ids = [str(row.get("id") or "?") for row in sample]
    kept: dict[str, Any] = {
        "count": count,
        "status": "available" if isinstance(count, int) and count > 0 else "missing",
        "connectors_sample": sample,
        "truncated": isinstance(count, int) and len(connectors) < count,
    }
    for key in ("blocked_until_data_api", "next_required_action"):
        value = output.get(key)
        if value not in (None, "", [], {}):
            kept[key] = value

    summary = f"connector_list: count={count}; ids={', '.join(ids[:8]) or 'none'}"
    setup_missing = [
        str(row.get("id") or "?")
        for row in sample
        if (row.get("setup_status") or {}).get("status") == "missing"
    ]
    if setup_missing:
        summary += "; setup_missing=" + ", ".join(setup_missing[:5])
    if output.get("next_required_action"):
        summary += "; next_required_action=present"

    return CompactedResult(
        rule_id="connector_list.summary",
        summary=summary,
        kept=kept,
        original_bytes=_bytes_of(output),
    )


def _compact_data_api_action_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in ("provider", "action", "title", "output_kind")
        if row.get(key) not in (None, "", [], {})
    }


def _data_api_next_required_action_name(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    candidates = [value]
    for key in ("arguments", "args", "input"):
        nested = value.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for candidate in candidates:
        action = candidate.get("action")
        if isinstance(action, str) and action.strip():
            return action.strip()
    return ""


def _reduce_data_api(name: str, output: Any) -> Optional[CompactedResult]:
    if "data_api" not in name or not isinstance(output, dict):
        return None
    if isinstance(output.get("actions"), list) and isinstance(output.get("providers"), list):
        actions = output.get("actions") or []
        next_action_name = _data_api_next_required_action_name(
            output.get("next_required_action")
        )
        actions_for_sample = actions[:12]
        if next_action_name:
            matching_actions = [
                row
                for row in actions
                if isinstance(row, dict) and row.get("action") == next_action_name
            ]
            if matching_actions:
                actions_for_sample = matching_actions[:1]
        action_sample = [
            _compact_data_api_action_row(row)
            for row in actions_for_sample
            if isinstance(row, dict)
        ]
        provider = str(output.get("provider") or output.get("requested_provider") or "")
        kept: dict[str, Any] = {
            "requested_provider": output.get("requested_provider"),
            "provider": output.get("provider"),
            "count": output.get("count", len(actions)),
            "limit": output.get("limit"),
            "actions_sample": action_sample,
        }
        for key in ("next_required_action", "hint"):
            value = output.get(key)
            if value not in (None, "", [], {}):
                kept[key] = (
                    _truncate(value, 900)
                    if isinstance(value, str)
                    else value
                )
        kept = {k: v for k, v in kept.items() if v not in (None, "", [], {})}
        action_names = [
            f"{row.get('provider')}.{row.get('action')}"
            for row in action_sample
            if row.get("provider") and row.get("action")
        ]
        summary = (
            "data_api catalog: "
            f"provider={provider or 'all'}; count={kept.get('count')}; "
            f"actions={', '.join(action_names[:8]) or 'none'}"
        )
        if output.get("next_required_action"):
            summary += "; next_required_action=present"
        return CompactedResult(
            rule_id="data_api.catalog",
            summary=summary,
            kept=kept,
            original_bytes=_bytes_of(output),
        )

    provider = str(output.get("provider") or "")
    action = str(output.get("action") or "")
    kind = str(output.get("kind") or "")
    if not provider or not action:
        return None
    kept: dict[str, Any] = {
        "provider": provider,
        "action": action,
        "kind": kind,
    }
    if kind == "table":
        rows = output.get("rows") if isinstance(output.get("rows"), list) else []
        kept.update({
            "row_count": output.get("row_count", len(rows)),
            "truncated": bool(output.get("truncated")),
            "rows_sample": rows[:5],
        })
        summary = (
            f"data_api {provider}.{action}: table "
            f"rows={kept['row_count']} sample={len(kept['rows_sample'])}"
        )
        return CompactedResult(
            rule_id="data_api.table",
            summary=summary,
            kept=kept,
            original_bytes=_bytes_of(output),
        )

    data = output.get("data")
    if not isinstance(data, dict):
        kept["data"] = data
        return CompactedResult(
            rule_id="data_api.value",
            summary=f"data_api {provider}.{action}: {kind or type(data).__name__}",
            kept=kept,
            original_bytes=_bytes_of(output),
        )

    if provider == "onchainos" and action in {
        "token_holders",
        "token_top_trader",
        "token_trades",
    }:
        rows = _extract_nested_rows(data)
        if rows:
            projected = [_project_onchainos_row(action, row) for row in rows[:5]]
            projected = [row for row in projected if row]
            top_profit = _top_profit_rows(action, rows)
            kept.update({
                "row_count": len(rows),
                "rows_sample": projected,
                "top_profit_rows": top_profit,
            })
            summary = (
                f"data_api {provider}.{action}: rows={len(rows)} "
                f"sample={len(projected)}"
            )
            if top_profit:
                best = top_profit[0]
                wallet = best.get("wallet") or best.get("address") or "?"
                pnl = best.get("realizedPnlUsd") or best.get("totalPnlUsd")
                summary += f" best_wallet={wallet} pnl={pnl}"
            return CompactedResult(
                rule_id="data_api.onchainos_rows",
                summary=summary,
                kept=kept,
                original_bytes=_bytes_of(output),
            )

    selected_route = data.get("selected_route") if isinstance(data.get("selected_route"), dict) else {}
    selection = data.get("selection") if isinstance(data.get("selection"), dict) else {}
    authoring = data.get("authoring_contract") if isinstance(data.get("authoring_contract"), dict) else {}
    kept.update({
        "next_required_action": data.get("next_required_action"),
        "selected_route": selected_route,
        "preferred_provider": data.get("preferred_provider"),
    })
    if selection:
        kept["selection"] = {
            "mode": selection.get("mode"),
            "preference": selection.get("preference"),
            "available_route_count": selection.get("available_route_count"),
            "fallback": selection.get("fallback"),
        }
    if authoring:
        kept["authoring_contract"] = {
            "skill": authoring.get("skill"),
            "sdk_import": authoring.get("sdk_import"),
            "proposal_tool_role": authoring.get("proposal_tool_role"),
            "prompt_contract": authoring.get("prompt_contract"),
            "live_guardrail": authoring.get("live_guardrail"),
            "address_policy": authoring.get("address_policy"),
        }
    bounded = data.get("bounded_sequence")
    if isinstance(bounded, list):
        kept["bounded_sequence"] = [
            {
                "step": row.get("step"),
                "tool": row.get("tool"),
                "call": row.get("call"),
                "calls": row.get("calls")[:3] if isinstance(row.get("calls"), list) else row.get("calls"),
                "call_shape": row.get("call_shape"),
            }
            for row in bounded[:5]
            if isinstance(row, dict)
        ]
    for key in ("minimum_evidence", "anti_patterns", "rules"):
        value = data.get(key)
        if isinstance(value, list):
            kept[key] = value[:5]
    status = data.get("ready")
    if status is not None:
        kept["ready"] = bool(status)
    if "provider_status" in data:
        kept["provider_status"] = data.get("provider_status")
    route_label = selected_route.get("canonical") or selected_route.get("provider") or "?"
    summary = (
        f"data_api {provider}.{action}: object route={route_label} "
        f"ready={selected_route.get('ready', status)}"
    )
    return CompactedResult(
        rule_id="data_api.object",
        summary=summary,
        kept=kept,
        original_bytes=_bytes_of(output),
    )


def _extract_nested_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if not isinstance(value, dict):
        return []
    for key in (
        "data",
        "rows",
        "items",
        "list",
        "holders",
        "traders",
        "trades",
        "result",
    ):
        nested = value.get(key)
        rows = _extract_nested_rows(nested)
        if rows:
            return rows
    return []


def _project_onchainos_row(action: str, row: dict[str, Any]) -> dict[str, Any]:
    keys_by_action = {
        "token_holders": (
            "holderWalletAddress",
            "walletAddress",
            "fundingSource",
            "realizedPnlUsd",
            "totalPnlUsd",
            "unrealizedPnlUsd",
            "avgBuyPrice",
            "avgSellPrice",
            "boughtAmount",
            "holdAmount",
            "holdPercent",
            "totalSellAmount",
            "nativeTokenBalance",
        ),
        "token_top_trader": (
            "walletAddress",
            "holderWalletAddress",
            "address",
            "realizedPnlUsd",
            "totalPnlUsd",
            "unrealizedPnlUsd",
            "avgBuyPrice",
            "avgSellPrice",
            "buyVolumeUsd",
            "sellVolumeUsd",
            "tradeCount",
            "winRate",
        ),
        "token_trades": (
            "timestamp",
            "time",
            "txHash",
            "side",
            "walletAddress",
            "maker",
            "price",
            "amount",
            "amountUsd",
            "volumeUsd",
        ),
    }
    projected: dict[str, Any] = {}
    for key in keys_by_action.get(action, ()):
        value = row.get(key)
        if value not in (None, ""):
            projected[key] = value
    return projected


def _top_profit_rows(action: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if action not in {"token_holders", "token_top_trader"}:
        return []

    def _num(value: Any) -> float:
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return 0.0

    ranked = sorted(
        rows,
        key=lambda row: max(_num(row.get("realizedPnlUsd")), _num(row.get("totalPnlUsd"))),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for row in ranked[:3]:
        wallet = (
            row.get("holderWalletAddress")
            or row.get("walletAddress")
            or row.get("address")
        )
        item = {
            "wallet": wallet,
            "realizedPnlUsd": row.get("realizedPnlUsd"),
            "totalPnlUsd": row.get("totalPnlUsd"),
            "avgBuyPrice": row.get("avgBuyPrice"),
            "avgSellPrice": row.get("avgSellPrice"),
            "holdAmount": row.get("holdAmount"),
        }
        out.append({k: v for k, v in item.items() if v not in (None, "")})
    return out


_TEAM_ROLE_OUTPUT_PRIORITY: tuple[str, ...] = (
    "rating",
    "verdict",
    "thesis",
    "direction",
    "bias",
    "confidence",
    "recommended_size_pct",
    "position_guidance",
    "target_price",
    "price_target",
    "upside_range",
    "downside_range",
    "invalidation",
    "review_triggers",
    "reasons",
    "bull_points",
    "bear_points",
    "narratives",
    "evidence",
    "data_coverage",
    "summary",
)


def _compact_scalar(value: Any, *, limit: int = 500) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate(value, limit)
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = repr(value)
    return _truncate(text, limit)


def _compact_list_sample(value: list[Any], *, limit: int = 3) -> list[Any]:
    out: list[Any] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            out.append(_compact_dict_sample(item, max_items=4, scalar_limit=240))
        elif isinstance(item, list):
            out.append(_compact_list_sample(item, limit=2))
        else:
            out.append(_compact_scalar(item, limit=240))
    return out


def _compact_dict_sample(
    value: dict[str, Any],
    *,
    priority: tuple[str, ...] = (),
    max_items: int = 8,
    scalar_limit: int = 500,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    ordered_keys: list[str] = []
    for key in priority:
        if key in value:
            ordered_keys.append(key)
    for key in value.keys():
        if key not in ordered_keys:
            ordered_keys.append(key)
    for key in ordered_keys:
        if len(out) >= max_items:
            break
        raw = value.get(key)
        if raw in (None, "", [], {}):
            continue
        if isinstance(raw, dict):
            out[key] = _compact_dict_sample(raw, max_items=5, scalar_limit=240)
        elif isinstance(raw, list):
            out[key] = _compact_list_sample(raw)
        else:
            out[key] = _compact_scalar(raw, limit=scalar_limit)
    return out


def _team_summary_from_output(output: Any) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not isinstance(output, dict):
        return None, {}
    if isinstance(output.get("team_summary"), dict):
        return output["team_summary"], {
            "task_id": output.get("task_id") or output.get("requested_task_id"),
            "task_state": output.get("state") or output.get("status"),
            "task_name": output.get("name"),
        }
    nested_output = output.get("output")
    if isinstance(nested_output, dict) and isinstance(nested_output.get("team_summary"), dict):
        return nested_output["team_summary"], {
            "task_id": output.get("task_id") or output.get("requested_task_id"),
            "task_state": output.get("state") or output.get("status"),
            "task_name": output.get("name"),
        }
    if (
        output.get("team_run_id")
        and isinstance(output.get("results"), list)
        and isinstance(output.get("roles_succeeded"), list)
    ):
        return output, {}
    return None, {}


def _summarize_team_member(entry: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "subagent": entry.get("subagent"),
        "ok": entry.get("ok"),
    }
    for key in ("tokens", "usd", "wall_ms", "error_kind", "error"):
        value = entry.get(key)
        if value not in (None, "", [], {}):
            out[key] = _compact_scalar(value, limit=300)
    member_output = entry.get("output")
    if isinstance(member_output, dict):
        out["output"] = _compact_dict_sample(
            member_output,
            priority=_TEAM_ROLE_OUTPUT_PRIORITY,
            max_items=10,
            scalar_limit=700,
        )
    elif member_output not in (None, "", [], {}):
        out["output"] = _compact_scalar(member_output, limit=700)
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def _reduce_team_run(name: str, output: Any) -> Optional[CompactedResult]:
    summary, wrapper = _team_summary_from_output(output)
    if summary is None:
        return None

    role_outputs: list[dict[str, Any]] = []
    for entry in summary.get("results") or []:
        if isinstance(entry, dict):
            role_outputs.append(_summarize_team_member(entry))

    failures: list[dict[str, Any]] = []
    for entry in summary.get("failures") or []:
        if isinstance(entry, dict):
            failures.append(_summarize_team_member(entry))

    aggregated = summary.get("aggregated")
    aggregated_summary: dict[str, Any] = {}
    if isinstance(aggregated, dict):
        aggregated_summary = _compact_dict_sample(
            aggregated,
            priority=("rating", "verdict", "avg_confidence", "summary"),
            max_items=5,
            scalar_limit=900,
        )

    kept: dict[str, Any] = {
        "team_run_id": summary.get("team_run_id"),
        "status": summary.get("status"),
        "ok": summary.get("ok"),
        "team_template": summary.get("team_template"),
        "task": _compact_scalar(summary.get("task"), limit=500),
        "output_language": summary.get("output_language"),
        "analysis_language": summary.get("analysis_language"),
        "roles_requested": summary.get("roles_requested") or [],
        "roles_succeeded": summary.get("roles_succeeded") or [],
        "roles_failed": summary.get("roles_failed") or [],
        "tokens_total": summary.get("tokens_total"),
        "usd_total": summary.get("usd_total"),
        "role_outputs": role_outputs,
        "failures": failures,
        "aggregated": aggregated_summary,
        "next_action": _compact_scalar(summary.get("next_action"), limit=700),
        # Prevent nested member/tool errors from being lifted as a fake
        # top-level team_run error by the generic audit-field pass.
        "error": summary.get("error"),
    }
    kept.update({k: v for k, v in wrapper.items() if v not in (None, "", [], {})})
    empty_list_fields = {"roles_failed", "failures"}
    kept = {
        k: v for k, v in kept.items()
        if v not in ("", {}) and (v != [] or k in empty_list_fields)
    }

    succeeded = len(kept.get("roles_succeeded") or [])
    failed = len(kept.get("roles_failed") or [])
    highlights: list[str] = []
    for row in role_outputs[:6]:
        role = str(row.get("subagent") or "").strip()
        role_out = row.get("output") if isinstance(row.get("output"), dict) else {}
        if not role or not isinstance(role_out, dict):
            continue
        signal = (
            role_out.get("rating")
            or role_out.get("verdict")
            or role_out.get("direction")
            or role_out.get("bias")
            or role_out.get("confidence")
        )
        if signal not in (None, "", [], {}):
            highlights.append(f"{role}={_compact_scalar(signal, limit=80)}")

    summary_text = (
        "team_run summary: "
        f"status={kept.get('status')}; "
        f"template={kept.get('team_template')}; "
        f"roles_succeeded={succeeded}; roles_failed={failed}"
    )
    if highlights:
        summary_text += "; highlights=" + ", ".join(highlights)

    return CompactedResult(
        rule_id="team_run.summary",
        summary=summary_text,
        kept=kept,
        original_bytes=_bytes_of(output),
    )


def _reduce_data_source_status(name: str, output: Any) -> Optional[CompactedResult]:
    if name != "data_source_status" or not isinstance(output, dict):
        return None
    summary = output.get("summary") if isinstance(output.get("summary"), dict) else {}
    sources = output.get("sources")
    if not isinstance(sources, list):
        sources = summary.get("sources") if isinstance(summary.get("sources"), list) else []
    events = output.get("events")
    if not isinstance(events, list):
        events = []
    total = output.get("total", summary.get("total", len(sources)))
    stale_count = output.get("stale_count", summary.get("stale_count", 0))

    kept_sources: list[dict[str, Any]] = []
    for row in sources[:12]:
        if not isinstance(row, dict):
            continue
        kept_sources.append({
            key: row.get(key)
            for key in (
                "source_id",
                "kind",
                "provider",
                "enabled",
                "stale",
                "last_success_at",
                "last_error",
            )
            if row.get(key) not in (None, "", [], {})
        })

    kept_events: list[dict[str, Any]] = []
    for row in events[:8]:
        if not isinstance(row, dict):
            continue
        kept_events.append({
            key: row.get(key)
            for key in ("source_id", "event", "status", "ts", "message")
            if row.get(key) not in (None, "", [], {})
        })

    kept = {
        "ok": output.get("ok"),
        "summary": {
            "total": total,
            "stale_count": stale_count,
            "generated_at": summary.get("generated_at") or output.get("generated_at"),
            "stale_ids": list(summary.get("stale_ids") or output.get("stale_ids") or [])[:12],
        },
        "sources": kept_sources,
        "events": kept_events,
    }
    kept = {
        key: value
        for key, value in kept.items()
        if value not in (None, "", [], {})
    }
    summary_text = (
        "data_source_status: "
        f"total={total}; stale_count={stale_count}; sources={len(sources)}"
    )
    if kept_events:
        summary_text += f"; events={len(events)}"
    return CompactedResult(
        rule_id="data_source.status",
        summary=summary_text,
        kept=kept,
        original_bytes=_bytes_of(output),
    )


def _reduce_generic_json(name: str, output: Any) -> Optional[CompactedResult]:
    if not isinstance(output, (dict, list)):
        return None
    audit = _extract_audit(output)
    if isinstance(output, dict):
        structured = _extract_structured_audit(output)
        top_keys = list(output.keys())[:25]
        summary = f"json dict: top_keys={top_keys}"
        kept = {
            "top_keys": top_keys,
            **_compact_top_level_fields(output),
            **audit,
            **structured,
        }
    else:
        summary = f"json list: count={len(output)}"
        kept = {"count": len(output), "first": output[:3], **audit}
    return CompactedResult(
        rule_id="json.large",
        summary=summary,
        kept=kept,
        original_bytes=_bytes_of(output),
    )


def _reduce_generic_text(name: str, output: Any) -> Optional[CompactedResult]:
    if not isinstance(output, str):
        return None
    text = output
    head = text[:600]
    tail = text[-600:]
    summary = f"text output ({len(text)} chars)"
    kept = {"chars": len(text), "head": head, "tail": tail}
    return CompactedResult(
        rule_id="text.large",
        summary=summary,
        kept=kept,
        original_bytes=_bytes_of(output),
    )


_REDUCERS: tuple[Callable[[str, Any], Optional[CompactedResult]], ...] = (
    _reduce_shell_git,
    _reduce_shell_pytest,
    _reduce_web_fetch,
    _reduce_candles,
    _reduce_orders,
    _reduce_backtest,
    _reduce_script_run,
    _reduce_connector_list,
    _reduce_data_api,
    _reduce_team_run,
    _reduce_data_source_status,
    _reduce_generic_json,
    _reduce_generic_text,
)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def compact_tool_result(
    name: str,
    output: Any,
    *,
    size_threshold: int = _DEFAULT_SIZE_THRESHOLD,
    raw_ref: Optional[str] = None,
) -> CompactedResult:
    """Compact ``output`` from a tool/skill named ``name``.

    Falls through to the original output via ``skipped=True`` when:
    - output is None or smaller than ``size_threshold``,
    - no reducer matches and the output is already small.
    """

    name = (name or "").lower()
    original_bytes = _bytes_of(output)

    if output is None:
        return CompactedResult(
            rule_id="noop",
            summary="(empty)",
            original_bytes=0,
            compacted_bytes=0,
            skipped=True,
            skipped_reason="empty",
            raw_ref=raw_ref,
        )

    if original_bytes < size_threshold:
        _STATS["skipped_small"] += 1
        return CompactedResult(
            rule_id="noop",
            summary=f"output small ({original_bytes} bytes)",
            kept={"output": output},
            original_bytes=original_bytes,
            compacted_bytes=original_bytes,
            skipped=True,
            skipped_reason="below_threshold",
            raw_ref=raw_ref,
        )

    for reducer in _REDUCERS:
        try:
            result = reducer(name, output)
        except Exception:  # pragma: no cover - defensive
            continue
        if result is None:
            continue
        # Always preserve audit fields by re-extracting from the original.
        audit = _extract_audit(output)
        for k, v in audit.items():
            result.kept.setdefault(k, v)
        result.summary = _truncate(result.summary)
        result.compacted_bytes = _bytes_of(result.kept) + _bytes_of(result.summary)
        result.raw_ref = raw_ref
        _STATS["compacted"] += 1
        saved = max(0, result.original_bytes - result.compacted_bytes)
        _STATS["bytes_saved"] += saved
        return result

    # No reducer matched. Return a noop so the caller can keep the
    # original output, but record it for visibility.
    _STATS["skipped_no_rule"] += 1
    return CompactedResult(
        rule_id="noop",
        summary=f"no reducer matched for {name!r}",
        kept={"output": output},
        original_bytes=original_bytes,
        compacted_bytes=original_bytes,
        skipped=True,
        skipped_reason="no_rule",
        raw_ref=raw_ref,
    )
