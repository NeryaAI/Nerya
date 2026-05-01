"""Compose + send a daily / weekly digest message.

Standalone CLI usage::

    python -m nerya.skills.builtin.notify.scripts.send_digest \\
        --json '{"channel": "operator",
                 "title": "Daily digest",
                 "sections": [
                   {"heading": "PnL", "lines": ["BTC +1.2%", "ETH +0.4%"]},
                   {"heading": "Risk", "lines": ["No breaches"]}
                 ]}'

The script renders the sections into a single block of plain text,
then queues it through the standard notify outbox (so transport
pickup, journals, and history all work the same as a normal message).
``severity`` defaults to ``digest`` so receivers can rate-limit /
filter on it client-side.

Output::

    {"ok": true, "message_id": str, "channel": str, "state": "queued",
     "rendered_chars": int}
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ._outbox import queue_message


def _render(title: str, sections: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    if title:
        parts.append(f"# {title}".rstrip())
    for sec in sections or []:
        if not isinstance(sec, dict):
            continue
        heading = str(sec.get("heading") or "").strip()
        lines = sec.get("lines") or []
        if heading:
            parts.append("")
            parts.append(f"## {heading}")
        if isinstance(lines, list):
            for line in lines:
                parts.append(f"- {line}")
        elif isinstance(lines, str):
            parts.append(lines.strip())
    return "\n".join(parts).strip()


def run(
    *,
    channel: str = "operator",
    title: str = "",
    sections: list[dict[str, Any]] | None = None,
    severity: str = "digest",
    workspace: str | None = None,
    strategy_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    sections = sections or []
    if not isinstance(sections, list):
        return {"ok": False, "error": "sections must be a list of {heading, lines}"}
    if not title and not sections:
        return {"ok": False, "error": "provide at least one of title / sections"}
    rendered = _render(title, sections)
    if not rendered:
        return {"ok": False, "error": "rendered digest is empty"}
    res = queue_message(
        channel=channel or "operator",
        text=rendered,
        severity=severity or "digest",
        workspace=workspace,
        strategy_id=strategy_id,
        session_id=session_id,
    )
    return {
        "ok": True,
        "channel": channel or "operator",
        "rendered_chars": len(rendered),
        **res,
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
    parser.add_argument("--workspace", dest="workspace", default=None)
    args = parser.parse_args()

    payload = _load_payload(args)
    workspace = args.workspace or payload.get("workspace")
    try:
        result = run(
            channel=str(payload.get("channel") or "operator"),
            title=str(payload.get("title") or ""),
            sections=payload.get("sections") if isinstance(payload.get("sections"), list) else None,
            severity=str(payload.get("severity") or "digest"),
            workspace=workspace,
            strategy_id=payload.get("strategy_id"),
            session_id=payload.get("session_id"),
        )
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
