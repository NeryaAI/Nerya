from __future__ import annotations

from typing import Any

from ..evidence import autoingest as _evidence_autoingest
from ..trading.access_control import guard_http_trade_scope


def _operator_submit_spec(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a fail-closed TradeIntent spec for the public HTTP route.

    ``POST /trading/submit`` is an operator-facing boundary.  Its JSON body is
    untrusted, so a caller must not be able to claim ``strategy_runtime`` (or
    any other unattended source) and inherit strategy auto-approval.  Internal
    strategies do not use this route; ``StrategyContext`` calls the SDK in
    process and stamps its own trusted source.
    """

    spec = dict(payload or {})
    trusted_actor_id = str(spec.pop("_auth_actor_id", "") or "").strip()
    spec.pop("_auth_scope", None)
    spec.pop("_auth_scopes", None)

    requested_source = str(spec.get("source") or "").strip()
    raw_meta = spec.get("meta")
    meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
    requested_actor_id = str(meta.get("actor_id") or "").strip()

    if requested_source:
        meta["requested_source"] = requested_source
    if requested_actor_id and requested_actor_id != trusted_actor_id:
        meta["requested_actor_id"] = requested_actor_id

    # Both fields are authoritative at this boundary.  ``agent:native`` is an
    # operator-agent source in RiskGate, so even otherwise-safe paper orders
    # are frozen and shown in the approval UI before execution.
    meta["actor_id"] = trusted_actor_id or "operator:http"
    meta["order_origin"] = "operator_http"
    spec["source"] = "agent:native"
    spec["meta"] = meta
    return spec


def routes():
    def submit(client, payload):
        denial = guard_http_trade_scope(
            getattr(client, "config", None),
            payload,
            account_id=str((payload or {}).get("account_id") or "").strip(),
            action="submit_order",
        )
        if denial is not None:
            return denial
        spec = _operator_submit_spec(payload)
        result = client.trading.submit_intent(**spec)
        # Auto-emit a vault record for the order outcome so the operator
        # always has a citeable audit trail.
        try:
            if isinstance(result, dict):
                order_id = (
                    result.get("order_id")
                    or (result.get("order") or {}).get("order_id")
                    or (result.get("result") or {}).get("order_id")
                    or ""
                )
                status = str(
                    result.get("status")
                    or (result.get("order") or {}).get("status")
                    or (result.get("result") or {}).get("status")
                    or "submitted"
                )
                account_id = (
                    result.get("account_id")
                    or spec.get("account_id")
                    or ""
                )
                symbol = (
                    result.get("symbol")
                    or spec.get("symbol")
                    or spec.get("market")
                    or ""
                )
                side = result.get("side") or spec.get("side") or ""
                qty = float(
                    result.get("qty")
                    or spec.get("qty")
                    or spec.get("size")
                    or 0
                )
                strategy_id = (
                    result.get("strategy_id")
                    or spec.get("strategy_id")
                )
                rejection_reason = (
                    result.get("rejection_reason")
                    or (result.get("result") or {}).get("reason")
                )
                if order_id:
                    _evidence_autoingest.on_order_filled(
                        client,
                        account_id=str(account_id),
                        order_id=str(order_id),
                        symbol=str(symbol),
                        side=str(side),
                        qty=qty,
                        status=status,
                        strategy_id=str(strategy_id) if strategy_id else None,
                        rejection_reason=(
                            str(rejection_reason) if rejection_reason else None
                        ),
                    )
        except Exception:  # pragma: no cover - defensive
            pass
        return result

    def cancel(client, payload):
        return client.trading.cancel_order(
            strategy_id=payload["strategy_id"],
            order_id=payload["order_id"],
            auth_context=payload,
        )

    def history(client, payload):
        return client.trading.get_strategy_history(
            strategy_id=payload["strategy_id"],
            limit=int(payload.get("limit", 20)),
        )

    return [
        ("POST", "/trading/submit", submit),
        ("POST", "/trading/cancel", cancel),
        ("POST", "/trading/history", history),
    ]
