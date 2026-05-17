from __future__ import annotations

from ..evidence import autoingest as _evidence_autoingest


def routes():
    def submit(client, payload):
        result = client.trading.submit_intent(**payload)
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
                    or payload.get("account_id")
                    or ""
                )
                symbol = (
                    result.get("symbol")
                    or payload.get("symbol")
                    or payload.get("market")
                    or ""
                )
                side = result.get("side") or payload.get("side") or ""
                qty = float(
                    result.get("qty")
                    or payload.get("qty")
                    or payload.get("size")
                    or 0
                )
                strategy_id = (
                    result.get("strategy_id")
                    or payload.get("strategy_id")
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
