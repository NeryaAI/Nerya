from __future__ import annotations


def routes():
    return [
        ("POST", "/messages/send",
         lambda client, payload: client.messages.send(
             channel=payload["channel"], text=payload["text"],
             strategy_id=payload.get("strategy_id"),
             session_id=payload.get("session_id"),
         )),
        ("POST", "/messages/list",
         lambda client, payload: client.messages.list(
             limit=int(payload.get("limit", 50)))),
    ]
