"""``anet.discover`` — find peers on the ANET network by skill tag."""

from __future__ import annotations

from typing import Any

from nerya.skills.manifest import cli_main

from ._common import get_client, require_anet


def run(ctx: Any = None, *, skill: str = "llm", **_extra: Any) -> dict[str, Any]:
    """Return a short peer table for ``skill``.

    ``skill`` is an ANET tag (``llm``, ``trading``, ``strategy-review``)
    — the single axis the daemon indexes on. Extra kwargs are accepted
    for forward-compat but ignored today.
    """
    del ctx  # runtime injects None; scripts run stateless
    block = require_anet(require_outbound=True)
    svc = get_client(block)
    try:
        peers = svc.discover(skill=skill)
    finally:
        try:
            svc.close()
        except Exception:
            pass
    rows = []
    for p in peers or []:
        services = [s.get("name") for s in (p.get("services") or [])
                    if isinstance(s, dict)]
        rows.append({
            "peer_id": p.get("peer_id"),
            "services": services,
            "did": p.get("did"),
            "cost_hint": (p.get("services") or [{}])[0].get("cost_model") if p.get("services") else None,
        })
    return {"ok": True, "skill": skill, "count": len(rows), "peers": rows}


if __name__ == "__main__":
    cli_main(run, default_payload={"skill": "llm"})
