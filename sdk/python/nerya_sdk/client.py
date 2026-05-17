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

    class _ChartsFacade:
        """Read and publish chart artifacts.

        The dashboard's ``ChartBlock`` already knows how to render a
        bulk envelope; this facade lets dynamic-code scripts (Agent
        ``run_shell`` Python) round-trip an artifact through the same
        workspace path that built-in skills use.

        Typical dynamic-code recipe (also in
        ``nerya/skills/builtin/coding/SKILL.md``)::

            from nerya_sdk import connect

            client = connect()
            res = client.charts.publish({
                "chart_kind": "line",
                "title": "Backtest equity curve",
                "series": [{"type": "line", "name": "equity", "data": [...]}],
                "source": {"skill": "agent", "action": "backtest"},
            })
            client.charts.emit_marker(res["chart_id"])
            # → kernel sees ``@@nerya:chart@@ <id>`` on stdout, splices
            #   the chart envelope into ``turn.blocks`` next to the
            #   ``run_shell`` call.
        """

        # Stdout marker the kernel watches for in ``run_shell`` output.
        # See ``nerya.agent.chart_hook.extract_chart_marker_ids``.
        MARKER_PREFIX = "@@nerya:chart@@"

        def __init__(self, outer: "NeryaClient"):
            self._outer = outer

        def get(self, chart_id: str) -> dict[str, Any]:
            """Read a chart artifact straight off disk.

            Returns the raw payload (``{chart_id, title, series, as_of}``)
            on hit. Raises :class:`KeyError` on miss so callers can't
            silently get an empty chart.
            """

            from nerya.charting import load_chart_artifact
            from nerya.workspace.artifact_store import ArtifactStore

            store = ArtifactStore(self._outer._client().config.paths)
            payload = load_chart_artifact(store, chart_id)
            if payload is None:
                raise KeyError(f"chart artifact not found: {chart_id!r}")
            return payload

        def publish(self, chart_block: dict[str, Any]) -> dict[str, Any]:
            """Persist ``chart_block`` to ``artifacts/charts/<id>.json``.

            ``chart_block`` is the same shape the composer emits, with
            inline ``series.data`` populated. The handler force-routes
            through ``path="bulk"`` so the resulting block carries
            ``bulk_data_uri`` references instead of inline data, which
            is what we want for dynamic code (large series stay off
            the LLM's plate).

            Returns ``{chart_id, bulk_data_uri, chart_block}``. Raises
            :class:`ValueError` with the server's diagnostic detail
            when the input fails composer validation.
            """

            if not isinstance(chart_block, dict):
                raise TypeError(
                    f"chart_block must be a dict, got {type(chart_block).__name__}"
                )
            from nerya.api.routes_charts import _post_publish

            res = _post_publish(self._outer._client(), {"chart_block": chart_block})
            if not res.get("ok"):
                raise ValueError(
                    f"chart publish rejected: {res.get('error')!r} "
                    f"({res.get('detail') or ''})"
                )
            return res

        def emit_marker(self, chart_id: str, *, file: Any = None) -> None:
            """Print ``@@nerya:chart@@ <chart_id>`` so the kernel splices
            the artifact into the chat next to the calling
            ``run_shell``. Defaults to ``sys.stdout`` so a script's last
            line becomes the marker.
            """

            import sys

            target = file if file is not None else sys.stdout
            print(f"{self.MARKER_PREFIX} {chart_id}", file=target, flush=True)

        def publish_and_announce(
            self, chart_block: dict[str, Any], *, file: Any = None
        ) -> dict[str, Any]:
            """One-shot helper: publish + print the kernel marker.

            Most dynamic-code scripts want both. This keeps the boilerplate
            to a single call so the recipe stays readable in skill docs.
            """

            res = self.publish(chart_block)
            self.emit_marker(res["chart_id"], file=file)
            return res

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

    @property
    def charts(self) -> "NeryaClient._ChartsFacade":
        return NeryaClient._ChartsFacade(self)


def connect(workspace: str | None = None) -> NeryaClient:
    return NeryaClient(workspace=workspace)
