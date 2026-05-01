"""``anet.audit`` — dump the local daemon's recent svc_call_log rows.

Used by reflection: "which peers did we call, what did they cost,
did they fail?" — feeds straight into the evolution pipeline so the
agent learns to avoid expensive or unreliable peers.
"""

from __future__ import annotations

from typing import Any

from nerya.skills.manifest import cli_main

from ._common import get_client, require_anet


def run(ctx: Any = None, *, limit: int = 20, **_extra: Any) -> dict[str, Any]:
    del ctx
    block = require_anet(require_outbound=False)  # reading our own log doesn't need outbound
    svc = get_client(block)
    try:
        rows = svc.audit(limit=int(limit))
    finally:
        try:
            svc.close()
        except Exception:
            pass
    return {"ok": True, "count": len(rows or []), "rows": list(rows or [])}


if __name__ == "__main__":
    cli_main(run, default_payload={"limit": 20})
