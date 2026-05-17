from __future__ import annotations

from ..evolution import assets as evolution_assets
from ..evolution.event_store import list_events, list_signals
from ..evolution.runner import evolve
from ..evolution.patch_proposal import list_proposals
from ..evolution.periodic_reflection import (
    PERIODIC_REFLECTION_SCHEDULE_ID,
    configure_periodic_reflection,
    ensure_periodic_reflection,
    get_periodic_reflection,
)
from ..evolution.promotion import apply_proposal
from ..evolution.ranking import (
    build_evidence, rank_proposals, write_ranking_snapshot,
)
from ..evolution.rollback import rollback_proposal
from ..evolution.signals import collect_signals
from ..evolution.timeline import build_timeline
from ..evolution.validation_plan import run_validation_plan
from ..evidence import autoingest as _evidence_autoingest


def _apply_handler(client, payload):
    """``POST /evolution/apply`` — apply a strategy proposal and emit
    an evidence vault record for the promotion.
    """

    proposal_id = (payload or {}).get("proposal_id")
    result = apply_proposal(client.config.paths, proposal_id)
    # Best-effort vault ingest. Vault failures must never break the
    # promotion call site, so we wrap defensively.
    try:
        if isinstance(result, dict) and result.get("ok"):
            strategy_id = (
                result.get("strategy_id")
                or (result.get("proposal") or {}).get("strategy_id")
                or "unknown"
            )
            title = (
                result.get("title")
                or f"Strategy proposal {proposal_id} applied"
            )
            summary = (
                result.get("summary")
                or f"Proposal {proposal_id} promoted on strategy {strategy_id}."
            )
            _evidence_autoingest.on_strategy_promote(
                client,
                strategy_id=str(strategy_id),
                proposal_id=str(proposal_id),
                title=str(title),
                summary=str(summary),
                body=str(summary),
                tags=["promotion"],
            )
    except Exception:  # pragma: no cover - defensive
        pass
    return result


def routes():
    def reflect(client, payload):
        """POST /evolution/reflect — run a reflection tick and return the
        ranked proposal seeds with evidence attached."""
        return evolve(client.config)

    def rank(client, payload):
        """POST /evolution/rank — rank open proposals using attribution.

        consume the evidence surfaces (reflection
        findings and paper/live divergence) to score every open
        proposal on severity, freshness, and scope. Optionally writes
        a snapshot to ``workspace/evolution/ranking.json`` for UIs.
        """
        payload = payload or {}
        strategy_id = payload.get("strategy_id")
        states = payload.get("states")
        persist = bool(payload.get("persist", False))
        ranked = rank_proposals(
            client.config.paths,
            strategy_id=strategy_id,
            states=tuple(states) if states else ("draft", "proposed"),
        )
        out: dict = {
            "strategy_id": strategy_id,
            "count": len(ranked),
            "ranked": [rp.asdict() for rp in ranked],
        }
        if persist:
            out["snapshot"] = write_ranking_snapshot(
                client.config.paths, ranked,
            )
        return out

    def evidence(client, payload):
        """POST /evolution/evidence — return the raw evidence bundle for
        one strategy so an operator can judge ranking quality."""
        sid = (payload or {}).get("strategy_id")
        if not sid:
            return {"error": "strategy_id required"}
        bundle = build_evidence(client.config.paths, sid)
        return {
            "strategy_id": bundle.strategy_id,
            "severity": bundle.severity(),
            "signals": bundle.signals(),
            "divergence": bundle.divergence,
            "counts": {
                "losses": len(bundle.losses),
                "bad_triggers": len(bundle.bad_triggers),
                "high_slippage": len(bundle.high_slippage),
                "stale_data": len(bundle.stale_data),
                "subagent_disagreement": len(bundle.subagent_disagreement),
                "overtrading": len(bundle.overtrading),
                "missed_opportunity": len(bundle.missed_opportunity),
            },
        }

    def signals(client, payload):
        payload = payload or {}
        refresh = str(payload.get("refresh") or "").lower() in {"1", "true", "yes"}
        if refresh:
            collected = collect_signals(
                client.config.paths,
                strategy_id=payload.get("strategy_id"),
                persist=True,
                limit=int(payload.get("limit") or 200),
            )
        else:
            collected = []
        rows = list_signals(
            client.config.paths,
            source=payload.get("source"),
            strategy_id=payload.get("strategy_id"),
            severity=payload.get("severity"),
            kind=payload.get("kind"),
            limit=int(payload.get("limit") or 100),
        )
        return {"signals": rows, "count": len(rows), "collected": collected}

    def events(client, payload):
        payload = payload or {}
        rows = list_events(
            client.config.paths,
            strategy_id=payload.get("strategy_id"),
            proposal_id=payload.get("proposal_id"),
            outcome=payload.get("outcome"),
            limit=int(payload.get("limit") or 100),
        )
        return {"events": rows, "count": len(rows)}

    def assets(client, payload):
        payload = payload or {}
        rows = evolution_assets.search_assets(
            client.config.paths,
            kind=payload.get("kind"),
            query=payload.get("query"),
            strategy_id=payload.get("strategy_id"),
            limit=int(payload.get("limit") or 100),
        )
        candidates = evolution_assets.list_candidates(
            client.config.paths,
            limit=int(payload.get("candidate_limit") or 100),
        )
        return {
            "assets": rows,
            "candidates": candidates,
            "count": len(rows),
            "candidate_count": len(candidates),
        }

    def candidate(client, payload):
        payload = payload or {}
        return evolution_assets.create_candidate(
            client.config.paths,
            kind=str(payload.get("kind") or "capsule"),
            summary=str(payload.get("summary") or ""),
            payload=dict(payload.get("payload") or {}),
            evidence_refs=list(payload.get("evidence_refs") or []),
            source_event_id=payload.get("source_event_id"),
            strategy_id=payload.get("strategy_id"),
        )

    def promote_asset(client, payload):
        payload = payload or {}
        return evolution_assets.promote_candidate(
            client.config.paths,
            str(payload.get("candidate_id") or ""),
            operator=payload.get("operator"),
        )

    def reject_asset(client, payload):
        payload = payload or {}
        return evolution_assets.reject_candidate(
            client.config.paths,
            str(payload.get("candidate_id") or ""),
            reason=str(payload.get("reason") or ""),
            operator=payload.get("operator"),
        )

    def validate(client, payload):
        payload = payload or {}
        return run_validation_plan(
            client.config.paths,
            plan_id=payload.get("plan_id"),
            proposal_id=payload.get("proposal_id"),
            dry_run=bool(payload.get("dry_run", True)),
        )

    def timeline(client, payload):
        payload = payload or {}
        return build_timeline(
            client.config,
            strategy_id=payload.get("strategy_id"),
            query=payload.get("query"),
            limit=int(payload.get("limit") or 120),
        )

    def reflection_schedule(client, payload):
        payload = payload or {}
        if not payload:
            return {
                "ok": True,
                "schedule": get_periodic_reflection(client.config.paths),
            }
        return configure_periodic_reflection(
            client.config.paths,
            enabled=bool(payload.get("enabled", False)),
            time=payload.get("time"),
            cron=payload.get("cron"),
            timezone=payload.get("timezone"),
        )

    def reflection_schedule_run_now(client, payload):
        ensure_periodic_reflection(client.config.paths)
        return client.triggers.run_schedule_now(
            id=PERIODIC_REFLECTION_SCHEDULE_ID,
            reason=str((payload or {}).get("reason") or "operator_dream_now"),
        )

    return [
        ("POST", "/evolution/proposals",
         lambda client, payload: {
             "proposals": [p.asdict() for p in list_proposals(client.config.paths)],
         }),
        ("POST", "/evolution/apply", _apply_handler),
        ("POST", "/evolution/rollback",
         lambda client, payload: rollback_proposal(client.config.paths,
                                                   payload["proposal_id"])),
        ("POST", "/evolution/reflect", reflect),
        ("POST", "/evolution/rank", rank),
        ("POST", "/evolution/evidence", evidence),
        ("GET", "/evolution/signals", signals),
        ("POST", "/evolution/signals", signals),
        ("GET", "/evolution/events", events),
        ("POST", "/evolution/events", events),
        ("GET", "/evolution/timeline", timeline),
        ("POST", "/evolution/timeline", timeline),
        ("GET", "/evolution/reflection_schedule", reflection_schedule),
        ("POST", "/evolution/reflection_schedule", reflection_schedule),
        ("POST", "/evolution/reflection_schedule/run_now", reflection_schedule_run_now),
        ("GET", "/evolution/assets", assets),
        ("POST", "/evolution/assets", assets),
        ("POST", "/evolution/assets/candidate", candidate),
        ("POST", "/evolution/assets/promote", promote_asset),
        ("POST", "/evolution/assets/reject", reject_asset),
        ("POST", "/evolution/validation/run", validate),
    ]
