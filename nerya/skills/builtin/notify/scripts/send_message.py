"""Send a message to the operator chat.

Standalone CLI usage::

    python -m nerya.skills.builtin.notify.scripts.send_message \\
        --json '{"text": "Hyperliquid funding spiked +0.4% / hr",
                 "severity": "info"}'

Defaults to the ``operator`` channel — the message-pipeline picks the
configured transport (Telegram / Discord / Slack / dashboard). Every
message lands in ``journals/messages.jsonl`` regardless of transport.

Output schema::

    {"message_id": str, "state": "queued", "outbox_path": str,
     "channel": "operator"}
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ._outbox import queue_message


def run(
    *,
    text: str,
    severity: str | None = None,
    workspace: str | None = None,
    strategy_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    if not text or not isinstance(text, str):
        return {"ok": False, "error": "text is required"}
    res = queue_message(
        channel="operator",
        text=text,
        severity=severity,
        workspace=workspace,
        strategy_id=strategy_id,
        session_id=session_id,
    )
    return {"ok": True, "channel": "operator", **res}


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
    parser.add_argument("--text", dest="text", default=None)
    args = parser.parse_args()

    payload = _load_payload(args)
    text = args.text or payload.get("text") or ""
    workspace = args.workspace or payload.get("workspace")
    try:
        result = run(
            text=text,
            severity=payload.get("severity"),
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
