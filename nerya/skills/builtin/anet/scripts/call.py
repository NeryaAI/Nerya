"""``anet.call`` — invoke a peer's service once, with approval guard.

The approval guard honours
``integrations.anet.outbound.require_approval_above_credits``: priced
calls above the threshold are routed through the Approval Gate before
the daemon is allowed to charge the wallet. Free calls skip the gate.
"""

from __future__ import annotations

from typing import Any

from nerya.skills.manifest import cli_main

from ._common import get_client, require_anet


def _estimate_cost(peers_meta: dict[str, Any], body: dict[str, Any]) -> int:
    """Conservative upper-bound estimate for approval decisions.

    We don't know the exact KB cost until the response streams, so
    fall back to ``per_call`` plus a small body-size cushion.
    """
    model = peers_meta.get("cost_model") or {}
    per_call = int(model.get("per_call") or 0)
    per_kb = int(model.get("per_kb") or 0)
    approx_kb = max(1, (len(str(body or {})) // 1024) + 1)
    return per_call + per_kb * approx_kb


def run(
    ctx: Any = None,
    *,
    peer_id: str,
    name: str,
    path: str,
    method: str = "POST",
    body: dict[str, Any] | None = None,
    **_extra: Any,
) -> dict[str, Any]:
    del ctx
    block = require_anet(require_outbound=True)
    svc = get_client(block)
    try:
        # Threshold check (integrators' wallet guard).
        threshold = int(((block.get("outbound") or {})
                         .get("require_approval_above_credits") or 0))
        # We can't reach into the daemon's ledger from here, but we can
        # fetch the peer's advertised cost model via meta.
        try:
            peer_meta = svc.meta(peer_id, name) or {}
        except Exception:
            peer_meta = {}
        est = _estimate_cost(peer_meta, body or {})
        if est > threshold:
            return {
                "ok": False,
                "error": "approval_required",
                "estimated_cost": est,
                "threshold": threshold,
                "peer_id": peer_id,
                "service": name,
                "hint": "raise integrations.anet.outbound."
                        "require_approval_above_credits or request "
                        "explicit operator approval before calling.",
            }
        resp = svc.call(peer_id, name, path, method=method, body=body or {})
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
        "status": resp.get("status"),
        "body": resp.get("body"),
    }


if __name__ == "__main__":
    cli_main(run)
