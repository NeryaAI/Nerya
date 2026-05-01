"""Compose and queue an outbound email.

Standalone CLI usage::

    python -m nerya.skills.builtin.notify.scripts.send_email \\
        --json '{"to": "ops@example.com",
                 "subject": "Daily PnL digest",
                 "body": "Cash 12_345 USD\\nEquity 18_900 USD"}'

Email transports vary by deployment (SMTP via ``messaging/`` config,
SES integration, etc.), so this script does **not** open a connection
itself. It writes a structured outbox entry tagged ``channel=email``
with the recipient + subject + body in ``extra``, and lets the
configured email transport (``messages/channels.yml``) deliver it.

If no email transport is configured the message still lands in
``outbox/messages`` and ``journals/messages.jsonl`` — operators
periodically reconcile undelivered entries from there.

Output::

    {"ok": true, "message_id": str, "channel": "email",
     "state": "queued"}
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ._outbox import queue_message


def run(
    *,
    to: str,
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    workspace: str | None = None,
    strategy_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    if not to:
        return {"ok": False, "error": "to is required"}
    if not subject:
        return {"ok": False, "error": "subject is required"}
    if not body:
        return {"ok": False, "error": "body is required"}
    extra: dict[str, Any] = {
        "to": to,
        "subject": subject,
    }
    if cc:
        extra["cc"] = list(cc)
    if bcc:
        extra["bcc"] = list(bcc)
    text = f"Subject: {subject}\nTo: {to}\n\n{body}".strip()
    res = queue_message(
        channel="email",
        text=text,
        severity=None,
        extra=extra,
        workspace=workspace,
        strategy_id=strategy_id,
        session_id=session_id,
    )
    return {"ok": True, "channel": "email", **res}


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
            to=str(payload.get("to") or ""),
            subject=str(payload.get("subject") or ""),
            body=str(payload.get("body") or ""),
            cc=payload.get("cc") if isinstance(payload.get("cc"), list) else None,
            bcc=payload.get("bcc") if isinstance(payload.get("bcc"), list) else None,
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
