from __future__ import annotations

from ..strategy_history.attribution import (
    attribute_session,
    execution_quality,
    subagent_contribution,
    paper_vs_live_divergence,
    indicator_sensitivity,
)
from ..strategy_history.scenario_replay import (
    ScenarioOverrides,
    scenario_replay,
)
from ..trading import strategy_versions


def routes():
    def attribution(client, payload):
        sid = payload["strategy_id"]
        session_id = payload["session_id"]
        bundle = attribute_session(client.config.paths, sid, session_id)
        return {
            "bundle": bundle.as_dict(),
            "execution_quality": execution_quality(client.config.paths, sid, session_id),
            "subagent_contribution": subagent_contribution(
                client.config.paths, sid, session_id),
            "indicator_sensitivity": indicator_sensitivity(
                client.config.paths, sid, session_id),
        }

    def divergence(client, payload):
        return paper_vs_live_divergence(
            client.config.paths, payload["strategy_id"],
            window_sessions=int(payload.get("window_sessions", 25)),
        )

    def versions(client, payload):
        paths = client.config.paths
        sid = payload["strategy_id"]
        return {
            "active_version_id": strategy_versions.active_version_id(paths, sid),
            "versions": [v.asdict() for v in strategy_versions.list_versions(paths, sid)],
            "promotions": [p.asdict() for p in strategy_versions.list_promotions(paths, sid)],
        }

    def compare_versions(client, payload):
        return strategy_versions.compare_versions(
            client.config.paths, payload["strategy_id"],
            left=payload["left"], right=payload["right"],
        )

    def scenario(client, payload):
        ov = ScenarioOverrides(**{
            k: payload[k]
            for k in ScenarioOverrides.__dataclass_fields__
            if k in payload
        })
        return scenario_replay(
            client.config.paths,
            payload["strategy_id"], payload["session_id"],
            overrides=ov,
        ).asdict()

    return [
        ("POST", "/strategy/history",
         lambda client, payload: client.strategy.history(
             payload["strategy_id"], limit=int(payload.get("limit", 20)))),
        ("POST", "/strategy/explain",
         lambda client, payload: client.strategy.explain_trade(
             payload["strategy_id"], payload["order_id"])),
        ("POST", "/strategy/review",
         lambda client, payload: client.strategy.review(
             payload["strategy_id"], payload["session_id"],
             stage=payload.get("stage", "immediate"))),
        ("POST", "/strategy/attribution", attribution),
        ("POST", "/strategy/divergence", divergence),
        ("POST", "/strategy/versions", versions),
        ("POST", "/strategy/versions/compare", compare_versions),
        ("POST", "/strategy/scenario_replay", scenario),
    ]
