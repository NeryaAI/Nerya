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
from typing import Any, Callable, Optional


# Fields we will never drop from a compacted summary. Order is significant
# for the human-readable summary.
AUDIT_FIELDS: tuple[str, ...] = (
    "order_id", "client_order_id", "account_id", "strategy_id",
    "approval_id", "risk_reason", "rejection_reason", "error", "error_code",
    "exchange_error_code", "ts", "timestamp", "symbol", "side", "qty",
    "price", "status",
)

# Soft threshold below which compaction is skipped (already small enough).
_DEFAULT_SIZE_THRESHOLD = 2_048

# Hard upper bound on the kept ``summary`` string. Anything larger triggers
# a second-pass tail truncation.
_SUMMARY_MAX_CHARS = 4_096


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


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------


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
    title = str(output.get("title") or "")
    url = str(output.get("url") or output.get("source_url") or "")
    text = str(output.get("text") or output.get("content") or "")
    headings = output.get("headings") or []
    snippet = _truncate(text, 1200)
    summary = f"web_fetch: {title or url} ({len(text)} chars)"
    kept = {
        "title": title,
        "url": url,
        "headings": list(headings)[:20],
        "snippet": snippet,
    }
    return CompactedResult(
        rule_id="research.web_fetch",
        summary=summary,
        kept=kept,
        original_bytes=_bytes_of(output),
    )


def _reduce_candles(name: str, output: Any) -> Optional[CompactedResult]:
    if "candle" not in name and "kline" not in name and "ohlcv" not in name:
        return None
    rows: Optional[list[Any]] = None
    symbol = ""
    interval = ""
    if isinstance(output, dict):
        rows = output.get("rows") or output.get("candles") or output.get("ohlcv")
        symbol = str(output.get("symbol") or "")
        interval = str(output.get("interval") or "")
    elif isinstance(output, list):
        rows = output
    if not isinstance(rows, list) or not rows:
        return None
    summary = (
        f"candles {symbol or '?'} {interval or '?'}: rows={len(rows)}"
    )
    first = rows[0] if rows else None
    last = rows[-1] if rows else None
    kept = {
        "symbol": symbol,
        "interval": interval,
        "rows": len(rows),
        "first": first,
        "last": last,
    }
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
    summary = f"backtest: metrics={list(metrics.keys())}, errors={len(errors)}"
    kept = {
        "metrics": metrics,
        "window": window,
        "symbols": list(symbols)[:10],
        "errors": list(errors)[:5],
        "artifact_refs": list(artifact_refs)[:5],
    }
    return CompactedResult(
        rule_id="backtest.report",
        summary=summary,
        kept=kept,
        original_bytes=_bytes_of(output),
    )


def _reduce_generic_json(name: str, output: Any) -> Optional[CompactedResult]:
    if not isinstance(output, (dict, list)):
        return None
    audit = _extract_audit(output)
    if isinstance(output, dict):
        top_keys = list(output.keys())[:25]
        summary = f"json dict: top_keys={top_keys}"
        kept = {"top_keys": top_keys, **audit}
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
