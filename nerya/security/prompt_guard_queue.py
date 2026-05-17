"""Prompt Guard Review Queue.

Backs the ``allow | review | block`` flow with a
persistent on-disk queue that the Action Inbox renders. Operators can
approve once, reject, mark a source trusted, or escalate the policy.

Storage layout::

    workspace/security/prompt_guard_queue.jsonl

Each record is a JSON line with the schema::

    {
      "id": "pg_...",
      "ts": "2026-05-13T00:00:00Z",
      "verdict": "review" | "block",
      "policy": "prompt_guard.review_v1",
      "source_route": "POST /agent/run_turn",
      "source_channel": "telegram|discord|webhook|dashboard|...",
      "matched": ["pattern1", "pattern2"],
      "excerpt": "redacted excerpt",
      "content_hash": "sha256:...",
      "recommended_action": "approve|reject|escalate",
      "affected_action": "trading.submit_order",
      "operator_id": "operator",
      "state": "pending" | "approved" | "rejected" | "escalated",
      "resolution": {...}
    }

The queue intentionally never stores the raw prompt unless ``allow_raw=True``
and the caller asserts the content has already been redacted.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _queue_file(client) -> Path:
    return client.config.paths.security / "prompt_guard_queue.jsonl"


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _read_all(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _write_all(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def enqueue(
    client,
    *,
    verdict: str,
    policy: str,
    matched: list[str],
    excerpt: str,
    content_hash: Optional[str] = None,
    source_route: str = "",
    source_channel: str = "",
    affected_action: str = "",
    recommended_action: str = "approve",
    raw_content: Optional[str] = None,
) -> dict[str, Any]:
    """Record a new review/block event.

    ``raw_content`` is never persisted; the caller passes it so we can
    compute a deterministic hash. The sanitized ``excerpt`` is the only
    text we store in the queue.
    """
    qid = "pg_" + secrets.token_hex(6)
    rec: dict[str, Any] = {
        "id": qid,
        "ts": _now_iso(),
        "verdict": verdict,
        "policy": policy,
        "matched": list(matched),
        "excerpt": str(excerpt or "")[:512],
        "content_hash": content_hash or _hash(raw_content or excerpt or ""),
        "source_route": source_route,
        "source_channel": source_channel,
        "affected_action": affected_action,
        "recommended_action": recommended_action,
        "state": "pending",
    }
    path = _queue_file(client)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def list_items(client, *, state: Optional[str] = None) -> list[dict[str, Any]]:
    rows = _read_all(_queue_file(client))
    if state:
        rows = [r for r in rows if r.get("state") == state]
    return sorted(rows, key=lambda r: r.get("ts") or "", reverse=True)


def resolve(
    client,
    *,
    item_id: str,
    decision: str,  # "approve" | "reject" | "trust_source" | "escalate"
    operator_id: str = "operator",
    note: str = "",
) -> dict[str, Any]:
    decision = (decision or "").strip().lower()
    valid = {"approve", "reject", "trust_source", "escalate"}
    if decision not in valid:
        raise ValueError(f"invalid decision {decision!r}; expected one of {sorted(valid)}")
    path = _queue_file(client)
    rows = _read_all(path)
    found = None
    for row in rows:
        if row.get("id") == item_id:
            row["state"] = (
                "approved" if decision == "approve" else
                "rejected" if decision == "reject" else
                "approved" if decision == "trust_source" else
                "escalated"
            )
            row["resolution"] = {
                "decision": decision,
                "operator_id": operator_id,
                "ts": _now_iso(),
                "note": note,
            }
            found = row
            break
    if found is None:
        raise KeyError(f"prompt_guard item {item_id!r} not found")
    _write_all(path, rows)
    return found


def stats(client) -> dict[str, Any]:
    rows = _read_all(_queue_file(client))
    by_state: dict[str, int] = {}
    by_verdict: dict[str, int] = {}
    for row in rows:
        by_state[row.get("state", "?")] = by_state.get(row.get("state", "?"), 0) + 1
        by_verdict[row.get("verdict", "?")] = by_verdict.get(row.get("verdict", "?"), 0) + 1
    return {
        "total": len(rows),
        "by_state": by_state,
        "by_verdict": by_verdict,
    }
