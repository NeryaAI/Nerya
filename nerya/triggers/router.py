"""Route events, enforce dedupe + cooldown + dead-letter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core import jsonl
from ..core.atomic_write import atomic_write_text
from ..core.config import Config
from ..core.errors import TriggerRouteError
from ..core.time import now_iso
from ..db import CooldownRepository, DedupeRepository
from ..db.sqlite import connect
from .event import TriggerEvent
from .routes import TriggerRoute, _dotted_eq, load_routes


@dataclass
class RouterResult:
    event_id: str
    status: str                # "routed" | "dead_letter" | "dedup" | "cooldown" | "dry_run"
    target: str | None
    route_id: str | None
    strategy_id: str | None
    reason: str | None = None

    def asdict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "status": self.status,
            "target": self.target, "route_id": self.route_id,
            "strategy_id": self.strategy_id, "reason": self.reason,
        }


class TriggerRouter:
    def __init__(self, config: Config):
        self.config = config
        self._con = None

    def _con_lazy(self):
        if self._con is None:
            self._con = connect(self.config.paths.db)
        return self._con

    def _load_routes(self) -> list[TriggerRoute]:
        return load_routes(self.config.paths)

    # ------------------------------------------------------------ policy
    def _resolve_payload_cap(self, event: TriggerEvent,
                             route: TriggerRoute | None) -> int:
        """Resolve the payload cap for ``event``.

        Precedence: route-level cap > per-actor override > per-channel
        override > per-source override > per-kind override >
        ``triggers.router.default_max_payload_bytes`` > legacy 65_536.
        """
        if route is not None and getattr(route, "max_payload_bytes", 0):
            try:
                return int(route.max_payload_bytes)
            except (TypeError, ValueError):
                pass

        cfg = self.config.get("triggers.router", {}) or {}
        policies = cfg.get("policies", {}) or {}
        ed = event.asdict() if hasattr(event, "asdict") else {}
        actor = (ed.get("payload") or {}).get("actor") if isinstance(ed.get("payload"), dict) else None
        channel = (ed.get("payload") or {}).get("channel") if isinstance(ed.get("payload"), dict) else None

        for table, key in (
            (policies.get("by_actor") or {}, actor),
            (policies.get("by_channel") or {}, channel),
            (policies.get("by_source") or {}, event.source),
            (policies.get("by_kind") or {}, event.kind),
        ):
            if not key:
                continue
            entry = table.get(str(key))
            if isinstance(entry, dict) and "max_payload_bytes" in entry:
                try:
                    return int(entry["max_payload_bytes"])
                except (TypeError, ValueError):
                    continue

        default_cap = cfg.get("default_max_payload_bytes", 65_536)
        try:
            return int(default_cap)
        except (TypeError, ValueError):
            return 65_536

    def dry_run(self, event: TriggerEvent) -> RouterResult:
        route, target, strategy_id = self._resolve(event)
        return RouterResult(
            event_id=event.event_id, status="dry_run", target=target,
            route_id=route.id if route else None, strategy_id=strategy_id,
            reason="dry_run",
        )

    def _resolve(self, event: TriggerEvent) -> tuple[TriggerRoute | None, str | None, str | None]:
        ed = event.asdict()
        for r in self._load_routes():
            if r.matches(ed):
                return r, r.target, r.strategy_id or event.strategy_id
        # fall back to event's own target if it is a known shape
        return None, event.target, event.strategy_id

    # ------------------------------------------------------------ explain
    def explain(self, event: TriggerEvent) -> dict[str, Any]:
        """Return a trace of *why* ``event`` would route where it does.

        This is the explain surface: it never emits the event, it
        never touches the dedupe / cooldown stores, it just evaluates
        every route in declaration order and surfaces:

        * ``matched_route``: the first matching route (or ``None``)
        * ``candidates``: every route considered, with per-key match info
        * ``effective_target`` / ``effective_strategy_id``: what the
          router would use
        * ``policies``: the cooldown / rate-limit / payload cap that
          would apply if the event were emitted right now
        """
        ed = event.asdict()
        candidates: list[dict[str, Any]] = []
        matched: TriggerRoute | None = None
        for r in self._load_routes():
            per_key: dict[str, bool] = {}
            for key, expected in r.match.items():
                per_key[key] = _dotted_eq(ed, key, expected)
            is_match = all(per_key.values()) if per_key else False
            candidates.append({
                "route_id": r.id,
                "target": r.target,
                "strategy_id": r.strategy_id,
                "match": dict(r.match),
                "per_key": per_key,
                "matched": is_match,
            })
            if matched is None and is_match:
                matched = r
        eff_target = matched.target if matched else event.target
        eff_strategy = (matched.strategy_id if matched
                        else event.strategy_id) or event.strategy_id
        return {
            "event_id": event.event_id,
            "matched_route": matched.id if matched else None,
            "effective_target": eff_target,
            "effective_strategy_id": eff_strategy,
            "candidates": candidates,
            "policies": {
                "cooldown_seconds": matched.cooldown_seconds if matched else 0,
                "max_per_minute": matched.max_per_minute if matched else 0,
                "max_payload_bytes": self._resolve_payload_cap(event, matched),
            },
        }

    # ------------------------------------------------------------- replay
    def replay(self, event: TriggerEvent, *,
               reason: str = "operator_replay") -> RouterResult:
        """Re-route an event, marking the journal entry as a replay.

        Replay reuses the normal :meth:`route` pipeline but bypasses the
        idempotency dedupe store (a replay is *expected* to reuse an
        existing idempotency key) and tags the trigger journal with a
        ``replay`` flag so operators can tell replay from original
        traffic.
        """
        # Strip the idempotency key for the replay path: the dedupe
        # guard is designed to protect original traffic, replays want
        # to be observable even if the key was seen before.
        replay_event = TriggerEvent(
            event_id=event.event_id, source=event.source, kind=event.kind,
            payload=dict(event.payload or {}), target=event.target,
            strategy_id=event.strategy_id, idempotency_key=None,
            dry_run=False, occurred_at=event.occurred_at,
        )
        replay_event.payload.setdefault("_replay", True)
        replay_event.payload.setdefault("_replay_reason", reason)
        result = self.route(replay_event)
        jsonl.append(self.config.paths.journal("triggers"), {
            "kind": "trigger.replay",
            "event_id": event.event_id,
            "result": result.asdict(),
            "reason": reason,
        })
        return result

    def route(self, event: TriggerEvent) -> RouterResult:
        ed = event.asdict()
        trigger_journal = self.config.paths.journal("triggers")
        errors_journal = self.config.paths.journal("errors")
        con = self._con_lazy()
        dedupe = DedupeRepository(con)
        cooldown_repo = CooldownRepository(con)

        # payload size cap. Hard-stop before any work.
        import json as _json
        payload_bytes = len(_json.dumps(event.payload, default=str).encode("utf-8"))
        route_peek, _, _ = self._resolve(event)
        cap = self._resolve_payload_cap(event, route_peek)
        if payload_bytes > cap:
            self._dead_letter(event, errors_journal,
                              reason=f"payload_too_large:{payload_bytes}>{cap}")
            return RouterResult(event.event_id, "dead_letter", None,
                                route_peek.id if route_peek else None,
                                event.strategy_id,
                                reason="payload_too_large")

        # dedupe
        if event.idempotency_key:
            window = 86400.0
            if dedupe.seen("trigger", event.idempotency_key, window_s=window):
                jsonl.append(trigger_journal, {
                    "kind": "trigger.dedup",
                    "event": ed,
                })
                return RouterResult(event.event_id, "dedup", None, None, None,
                                    reason="duplicate_idempotency_key")

        route, target, strategy_id = self._resolve(event)

        if route is None and target == "main":
            # no explicit route + default target main is allowed iff a strategy is given;
            # otherwise dead-letter to avoid fan-out.
            if not strategy_id:
                self._dead_letter(event, errors_journal, reason="no_route_no_strategy")
                return RouterResult(event.event_id, "dead_letter", None, None, None,
                                    reason="no_route_no_strategy")

        # validate target
        try:
            TriggerEvent._validate_target(target or "")  # type: ignore[arg-type]
        except Exception:
            self._dead_letter(event, errors_journal, reason=f"bad_target:{target}")
            return RouterResult(event.event_id, "dead_letter", None,
                                route.id if route else None, strategy_id,
                                reason="bad_target")

        # cooldown
        if route and route.cooldown_seconds > 0:
            if cooldown_repo.hit_and_check("trigger_route", route.id, route.cooldown_seconds):
                jsonl.append(trigger_journal, {
                    "kind": "trigger.cooldown",
                    "route_id": route.id,
                    "event_id": event.event_id,
                })
                return RouterResult(event.event_id, "cooldown", target,
                                    route.id, strategy_id, reason="route_cooldown")

        # rate limit: max_per_minute
        if route and route.max_per_minute > 0:
            if _rate_limit_hit(con, route.id, route.max_per_minute):
                jsonl.append(trigger_journal, {
                    "kind": "trigger.rate_limited",
                    "route_id": route.id,
                    "event_id": event.event_id,
                    "limit": route.max_per_minute,
                })
                return RouterResult(event.event_id, "rate_limited", target,
                                    route.id, strategy_id,
                                    reason=f"max_per_minute:{route.max_per_minute}")

        jsonl.append(trigger_journal, {
            "kind": "trigger.routed",
            "route_id": route.id if route else None,
            "target": target,
            "strategy_id": strategy_id,
            "event_id": event.event_id,
            "source": event.source,
            "event_kind": event.kind,
        })
        # also write into strategy history if scoped
        if strategy_id:
            from ..strategy_history.store import record_trigger
            record_trigger(self.config.paths, strategy_id=strategy_id,
                           session_id=None, event=ed)
        return RouterResult(event.event_id, "routed", target,
                            route.id if route else None, strategy_id, reason=None)

    def _dead_letter(self, event: TriggerEvent, errors_journal: Path, reason: str) -> None:
        dl = self.config.paths.dead_letter
        dl.mkdir(parents=True, exist_ok=True)
        fname = f"{now_iso().replace(':', '-')}_{event.event_id}.json"
        atomic_write_text(dl / fname, json.dumps(
            {"event": event.asdict(), "reason": reason}, indent=2, default=str,
        ))
        jsonl.append(errors_journal, {
            "kind": "trigger.dead_letter",
            "event_id": event.event_id,
            "reason": reason,
            "target": event.target,
        })


def _rate_limit_hit(con, route_id: str, max_per_minute: int) -> bool:
    """Sliding 60s window rate limiter backed by the same cooldown table."""
    import time
    now = time.time()
    tbl = "trigger_rate_window"
    con.execute(
        "CREATE TABLE IF NOT EXISTS trigger_rate_window "
        "(route_id TEXT, ts REAL)"
    )
    con.execute(
        "DELETE FROM trigger_rate_window WHERE ts < ?", (now - 60.0,)
    )
    cur = con.execute(
        "SELECT COUNT(*) FROM trigger_rate_window WHERE route_id=?", (route_id,)
    )
    count = cur.fetchone()[0]
    if count >= max_per_minute:
        return True
    con.execute(
        "INSERT INTO trigger_rate_window (route_id, ts) VALUES (?, ?)",
        (route_id, now),
    )
    return False
