from __future__ import annotations


def routes():
    def submit(client, payload):
        return client.trading.submit_intent(**payload)

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
