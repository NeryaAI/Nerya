"""``anet.stream`` — consume a server-stream from a discovered peer.

Returns the full concatenated event list so the caller can post-
process. For genuinely long streams the agent should loop and write
each chunk into strategy memory rather than collecting in-memory.
"""

from __future__ import annotations

from typing import Any

from nerya.skills.manifest import cli_main

from ._common import get_client, require_anet


def run(
    ctx: Any = None,
    *,
    peer_id: str,
    name: str,
    path: str,
    method: str = "POST",
    mode: str = "server-stream",
    body: dict[str, Any] | None = None,
    max_events: int = 200,
    **_extra: Any,
) -> dict[str, Any]:
    del ctx
    block = require_anet(require_outbound=True)
    svc = get_client(block)
    events: list[dict[str, Any]] = []
    try:
        for ev in svc.stream(peer_id, name, path, method=method, mode=mode,
                             body=body or {}):
            events.append({
                "event": getattr(ev, "event", None),
                "data": getattr(ev, "data", None),
                "is_terminal": getattr(ev, "is_terminal", False),
            })
            if len(events) >= int(max_events):
                break
            if getattr(ev, "is_terminal", False):
                break
    finally:
        try:
            svc.close()
        except Exception:
            pass
    return {
        "ok": True,
        "peer_id": peer_id,
        "service": name,
        "path": path,
        "count": len(events),
        "events": events,
    }


if __name__ == "__main__":
    cli_main(run)
