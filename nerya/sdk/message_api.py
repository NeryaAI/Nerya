"""Message SDK wrapper.

The legacy ``message`` skill (``send_message`` / ``list_messages``) was
archived during the workspace-native rewrite. Outbound messaging now
goes through the new ``notify`` skill's standalone scripts (which
already write to ``workspace/outbox/messages/``), so this SDK wrapper
talks to the on-disk outbox directly. The dashboard's
``POST /messages/list`` endpoint feeds off :meth:`list`, so it has to
keep working without the bridge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..core.config import Config
from ..skills.kernel import SkillKernel
from ..workspace.outbox import write_message


@dataclass
class MessageAPI:
    config: Config
    skills: SkillKernel

    def send(self, *, channel: str, text: str,
             strategy_id: str | None = None, session_id: str | None = None,
             caller: str = "sdk") -> dict[str, Any]:
        # The legacy ``message.send_message`` skill is gone — write
        # directly to the outbox. The downstream messaging pipeline
        # picks files up from the same directory.
        from ..core.time import now_iso

        message: dict[str, Any] = {
            "ts": now_iso(),
            "channel": channel,
            "text": text,
            "strategy_id": strategy_id,
            "session_id": session_id,
            "caller": caller,
        }
        path = write_message(self.config.paths.outbox_messages, message)
        return {"ok": True, "path": str(path), "message": message}

    def list(self, *, limit: int = 50) -> dict[str, Any]:
        outbox = self.config.paths.outbox_messages
        if not outbox.is_dir():
            return {"messages": [], "count": 0}
        files = sorted(
            (p for p in outbox.iterdir() if p.is_file() and p.suffix == ".json"),
            reverse=True,
        )[: max(1, int(limit))]
        out: list[dict[str, Any]] = []
        for p in files:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            data["_path"] = str(p)
            out.append(data)
        return {"messages": out, "count": len(out)}
