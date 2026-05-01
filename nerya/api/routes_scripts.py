from __future__ import annotations


def routes():
    def run(client, payload):
        return client.skill.call("script", "run_script",
                                 payload={"script_id": payload["script_id"],
                                          "args": payload.get("args") or {}})

    def analyze(client, payload):
        return client.skill.call("script", "static_analyze_script",
                                 payload={"script_id": payload["script_id"]})

    return [
        ("POST", "/scripts/run", run),
        ("POST", "/scripts/analyze", analyze),
    ]
