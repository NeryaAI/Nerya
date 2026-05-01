"""Shared helper: queue a structured outbound message.

Mirrors the previous ``message_skill.scripts.handlers``
``send_message`` action, just without the ``ctx`` plumbing — every notify
script ultimately produces a record in:

* ``outbox/messages/<id>.json``  (dashboard / local delivery picks up)
* ``journals/messages.jsonl``    (audit trail)

The actual transport (Telegram / Discord / email / webhook) is
configured in ``messages/channels.yml`` and handled by
:mod:`nerya.messaging.pipeline`. Keeping this script-side helper thin
means the agent can author new notification scripts without
re-implementing the persistence dance.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def queue_message(
    *,
    channel: str,
    text: str,
    severity: str | None = None,
    extra: dict[str, Any] | None = None,
    workspace: str | None = None,
    strategy_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Write the outbox + journal entries for one outbound message.

    Returns ``{"message_id", "state": "queued", "outbox_path"}``.
    """

    from nerya.core import jsonl
    from nerya.core.atomic_write import atomic_write_text
    from nerya.core.ids import message_id
    from nerya.core.paths import WorkspacePaths
    from nerya.core.time import now_iso
    from nerya.strategy_history import store as history_store

    root = Path(workspace).expanduser().resolve() if workspace else Path(os.getcwd()).resolve()
    paths = WorkspacePaths(root=root)

    mid = message_id()
    record: dict[str, Any] = {
        "message_id": mid,
        "channel": channel or "default",
        "text": text if isinstance(text, str) else json.dumps(text, default=str),
        "severity": severity,
        "state": "queued",
        "ts": now_iso(),
    }
    if extra:
        record["extra"] = dict(extra)
    if strategy_id:
        record["strategy_id"] = strategy_id
    if session_id:
        record["session_id"] = session_id

    jsonl.append(paths.journal("messages"), record)
    if strategy_id:
        history_store.record_message(
            paths,
            strategy_id=strategy_id,
            session_id=session_id,
            message=record,
        )
    out_path = paths.outbox_messages / f"{mid}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out_path, json.dumps(record, indent=2, default=str))

    return {
        "message_id": mid,
        "state": "queued",
        "outbox_path": str(out_path),
    }


__all__ = ["queue_message"]
