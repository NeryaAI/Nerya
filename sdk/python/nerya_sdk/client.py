"""NeryaClient — file-mode and in-process bridges.

In-process mode (default in this slice) is a thin wrapper around
`nerya.sdk.InternalClient`. File-mode writes into `workspace/inbox/`
so a long-running Nerya daemon can pick events up; the daemon is not
started automatically by this SDK.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nerya.sdk import InternalClient


@dataclass
class NeryaClient:
    workspace: str | None = None
    _internal: InternalClient | None = field(default=None, init=False, repr=False)

    def _client(self) -> InternalClient:
        if self._internal is None:
            self._internal = InternalClient.boot(self.workspace)
        return self._internal

    # ---------- trigger surface ----------
    class _TriggersFacade:
        def __init__(self, outer: "NeryaClient"):
            self._outer = outer

        def emit(self, *, source: str, kind: str,
                 payload: dict[str, Any] | None = None,
                 target: str = "main", strategy_id: str | None = None,
                 idempotency_key: str | None = None,
                 dry_run: bool = False) -> dict[str, Any]:
            return self._outer._client().triggers.emit(
                source=source, kind=kind, payload=payload or {},
                target=target, strategy_id=strategy_id,
                idempotency_key=idempotency_key, dry_run=dry_run,
            )

        def dry_run(self, **kw) -> dict[str, Any]:
            kw["dry_run"] = True
            return self._outer._client().triggers.emit(**kw)

        def list_routes(self) -> list[dict[str, Any]]:
            return self._outer._client().triggers.list_routes()

        def wait_for_result(self, event_id: str, *, timeout_s: float = 5.0,
                            poll_s: float = 0.1) -> dict[str, Any] | None:
            return self._outer._client().triggers.wait_for_result(
                event_id, timeout_s=timeout_s, poll_s=poll_s
            )

        def emit_to_file(self, *, source: str, kind: str,
                         payload: dict[str, Any] | None = None,
                         target: str = "main", strategy_id: str | None = None,
                         idempotency_key: str | None = None) -> Path:
            paths = self._outer._client().config.paths
            paths.inbox_triggers.mkdir(parents=True, exist_ok=True)
            eid = idempotency_key or uuid.uuid4().hex
            ev = {
                "source": source, "kind": kind, "payload": payload or {},
                "target": target, "strategy_id": strategy_id,
                "idempotency_key": idempotency_key,
            }
            path = paths.inbox_triggers / f"{eid}.json"
            path.write_text(json.dumps(ev, indent=2), encoding="utf-8")
            return path

    class _TradingFacade:
        def __init__(self, outer: "NeryaClient"):
            self._outer = outer

        def submit_intent(self, **payload) -> dict[str, Any]:
            return self._outer._client().trading.submit_intent(**payload)

        def submit_to_file(self, **payload) -> Path:
            paths = self._outer._client().config.paths
            paths.inbox_sdk_orders.mkdir(parents=True, exist_ok=True)
            path = paths.inbox_sdk_orders / f"{uuid.uuid4().hex}.json"
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            return path

    class _LLMFacade:
        def __init__(self, outer: "NeryaClient"):
            self._outer = outer

        def classify(self, **kw) -> dict[str, Any]:
            return self._outer._client().llm.classify(**kw)

        def extract_json(self, **kw) -> dict[str, Any]:
            return self._outer._client().llm.extract_json(**kw)

        def analyze_signal(self, **kw) -> dict[str, Any]:
            return self._outer._client().llm.analyze_signal(**kw)

        def compress(self, **kw) -> dict[str, Any]:
            return self._outer._client().llm.compress(**kw)

    class _StrategyFacade:
        def __init__(self, outer: "NeryaClient"):
            self._outer = outer

        def history(self, strategy_id: str, *, limit: int = 20) -> dict[str, Any]:
            return self._outer._client().strategy.history(strategy_id, limit=limit)

        def review(self, strategy_id: str, session_id: str, *, stage: str = "immediate") -> dict[str, Any]:
            return self._outer._client().strategy.review(strategy_id, session_id, stage=stage)

    class _MessageFacade:
        def __init__(self, outer: "NeryaClient"):
            self._outer = outer

        def send(self, **kw) -> dict[str, Any]:
            return self._outer._client().messages.send(**kw)

    class _AgentFacade:
        """workspace-native agent surface for the SDK.

        Mirrors :class:`nerya.sdk.agent_api.AgentAPI`. ``run_turn`` returns
        the same dashboard-shaped dict the HTTP API serves at
        ``POST /agent/run_turn``, including ``blocks`` (provider-native
        envelopes) and ``tool_trace`` (executed tool calls) so SDK callers
        can render the new transcript without going through the
        dashboard.
        """

        def __init__(self, outer: "NeryaClient"):
            self._outer = outer

        def run_turn(
            self,
            *,
            text: str | None = None,
            trigger: dict[str, Any] | None = None,
            strategy_id: str | None = None,
            session_id: str | None = None,
            attached_skills: list[str] | None = None,
        ) -> dict[str, Any]:
            return self._outer._client().agent.run_turn(
                text=text,
                trigger=trigger,
                strategy_id=strategy_id,
                session_id=session_id,
                attached_skills=attached_skills,
            )

        def list_tools(self) -> dict[str, Any]:
            return self._outer._client().agent.list_tools()

    @property
    def triggers(self) -> "NeryaClient._TriggersFacade":
        return NeryaClient._TriggersFacade(self)

    @property
    def trading(self) -> "NeryaClient._TradingFacade":
        return NeryaClient._TradingFacade(self)

    @property
    def llm(self) -> "NeryaClient._LLMFacade":
        return NeryaClient._LLMFacade(self)

    @property
    def strategy(self) -> "NeryaClient._StrategyFacade":
        return NeryaClient._StrategyFacade(self)

    @property
    def messages(self) -> "NeryaClient._MessageFacade":
        return NeryaClient._MessageFacade(self)

    @property
    def agent(self) -> "NeryaClient._AgentFacade":
        return NeryaClient._AgentFacade(self)


def connect(workspace: str | None = None) -> NeryaClient:
    return NeryaClient(workspace=workspace)
