"""Pure-Python tool surface exposed by the Nerya MCP server.

Design rules
------------
* Read-only wherever possible: portfolio, market data, news, social, on-chain,
  proposals, strategy history / explain.
* Mutating operations are limited to:
    - ``trigger_emit`` / ``trigger_dry_run`` — still go through
      :class:`TriggerRouter` (rate limits, payload cap, cooldown).
    - ``risk_preview`` — strictly dry-run, never touches the executor.
* Things that must stay on the operator-only CLI and are deliberately NOT
  exposed here:
    - proposal approve / apply / rollback
    - vault mutations
    - direct trade submission (live or paper)
    - signer policy edits
    - live_trading / kill switch toggles

Every tool returns JSON-serialisable dicts / lists and never raises — errors
are wrapped in :class:`ToolError` with a stable ``code`` + ``message`` so
MCP clients get predictable responses.
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core import yaml_io
from ..sdk.internal_client import InternalClient


class ToolError(Exception):
    """Structured MCP-tool error with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def asdict(self) -> dict[str, str]:
        return {"error": {"code": self.code, "message": self.message}}


def _safe(fn):
    """Wrap a NeryaTools method so it always returns a JSON-serialisable dict.

    Converts :class:`ToolError` into ``{"error": {...}}`` and catches every
    other exception into ``{"error": {"code": "internal", ...}}`` so that
    neither the caller nor the transport ever sees a raw traceback.
    """

    def _wrap(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except ToolError as e:
            return e.asdict()
        except Exception as e:  # pragma: no cover - defensive
            return {
                "error": {
                    "code": "internal",
                    "message": f"{type(e).__name__}: {e}",
                    "trace": traceback.format_exc(limit=4),
                }
            }

    _wrap.__name__ = fn.__name__
    _wrap.__doc__ = fn.__doc__
    return _wrap


@dataclass
class NeryaTools:
    """Thin facade that wires an :class:`InternalClient` to the MCP surface."""

    client: InternalClient

    # --------------------------------------------------------------- boot
    @classmethod
    def boot(cls, workspace: str | Path | None = None) -> "NeryaTools":
        return cls(client=InternalClient.boot(workspace))

    # ----------------------------------------------------------- meta
    @_safe
    def info(self) -> dict[str, Any]:
        """Return the Nerya workspace summary (version, mode, enabled skills).

        Safe to call from any MCP client; contains zero secrets.
        """
        cfg = self.client.config
        paths = cfg.paths
        nerya_yml = yaml_io.load(paths.config, default={}) or {}
        runtime = nerya_yml.get("runtime", {}) or {}
        return {
            "workspace": str(paths.root),
            "version": nerya_yml.get("version", "unknown"),
            "mode": runtime.get("mode", "paper"),
            "live_trading_enabled": bool(runtime.get("live_trading_enabled", False)),
            "kill_switch": bool(runtime.get("kill_switch", False)),
            "skills_enabled": [e.manifest.id for e in self.client.skills.registry.list()],
        }

    @_safe
    def skills_list(self) -> dict[str, Any]:
        """List currently enabled skills with their actions and permissions."""
        out = []
        for entry in self.client.skills.registry.list():
            m = entry.manifest
            out.append({
                "id": m.id,
                "version": getattr(m, "version", ""),
                "permissions": list(getattr(m, "permissions", []) or []),
                "actions": sorted(m.actions.keys()),
            })
        return {"skills": out}

    # ----------------------------------------------------------- market
    @_safe
    def market_ticker(self, market: str) -> dict[str, Any]:
        """Return the most recent ticker for a market like ``BINANCE:BTCUSDT``."""
        if not market or ":" not in market:
            raise ToolError("bad_request", "market must be 'VENUE:SYMBOL'")
        return self.client.skill.call(
            "market_data", "get_ticker",
            payload={"market": market}, caller="mcp",
        )

    @_safe
    def market_klines(self, market: str, interval: str = "1m",
                      count: int = 60) -> dict[str, Any]:
        """Return recent OHLCV candles for a market.

        ``interval`` examples: ``1m``, ``5m``, ``1h``. ``count`` is capped at 500.
        """
        count = max(1, min(int(count), 500))
        return self.client.skill.call(
            "market_data", "get_candles",
            payload={"market": market, "interval": interval, "count": count},
            caller="mcp",
        )

    # --------------------------------------------------------- portfolio
    @_safe
    def portfolio_summary(self) -> dict[str, Any]:
        """Return the cross-account portfolio snapshot (balances + positions)."""
        return self.client.skill.call(
            "portfolio", "get_portfolio_summary",
            payload={}, caller="mcp",
        )

    # --------------------------------------------------------- news + social
    @_safe
    def news_recent(self, limit: int = 10) -> dict[str, Any]:
        """Return the latest crypto headlines with extracted tickers."""
        limit = max(1, min(int(limit), 50))
        return self.client.skill.call(
            "news_social", "get_recent_news",
            payload={"limit": limit}, caller="mcp",
        )

    @_safe
    def social_signals(self, limit: int = 10) -> dict[str, Any]:
        """Return hot social-sentiment posts for major chains."""
        limit = max(1, min(int(limit), 50))
        return self.client.skill.call(
            "news_social", "get_social_signals",
            payload={"limit": limit}, caller="mcp",
        )

    # --------------------------------------------------------- onchain
    @_safe
    def onchain_whale_events(self, chain: str, limit: int = 5) -> dict[str, Any]:
        """Return recent large native-asset transfers on ``chain`` (EVM / Solana)."""
        if not chain:
            raise ToolError("bad_request", "chain is required")
        return self.client.skill.call(
            "onchain", "get_whale_events",
            payload={"chain": chain, "limit": max(1, min(int(limit), 20))},
            caller="mcp",
        )

    # --------------------------------------------------------- strategy
    @_safe
    def strategy_history(self, strategy_id: str, limit: int = 20) -> dict[str, Any]:
        """Return the last N events for a strategy."""
        if not strategy_id:
            raise ToolError("bad_request", "strategy_id required")
        return {
            "strategy_id": strategy_id,
            "history": self.client.strategy.history(strategy_id, limit=limit),
        }

    @_safe
    def strategy_explain_trade(self, strategy_id: str, order_id: str) -> dict[str, Any]:
        """Return a human-readable explanation of a past trade."""
        if not strategy_id or not order_id:
            raise ToolError("bad_request", "strategy_id and order_id required")
        return self.client.strategy.explain_trade(strategy_id, order_id)

    # --------------------------------------------------------- risk
    @_safe
    def risk_preview(self, intent: dict[str, Any]) -> dict[str, Any]:
        """Preview the risk-gate decision for a :class:`TradeIntent`-shape dict.

        Strictly dry-run: no order is placed even if the gate allows it.
        """
        if not isinstance(intent, dict) or not intent:
            raise ToolError("bad_request", "intent dict required")
        return self.client.skill.call(
            "risk", "check_intent",
            payload={"intent": dict(intent)}, caller="mcp",
        )

    # --------------------------------------------------------- triggers
    @_safe
    def trigger_emit(self, *, kind: str, payload: dict[str, Any] | None = None,
                     target: str = "main", strategy_id: str | None = None,
                     idempotency_key: str | None = None,
                     dry_run: bool = False) -> dict[str, Any]:
        """Emit a trigger (or dry-run it). Source is forced to ``webhook``."""
        if not kind:
            raise ToolError("bad_request", "kind required")
        return self.client.triggers.emit(
            source="webhook", kind=kind, payload=payload or {},
            target=target, strategy_id=strategy_id,
            idempotency_key=idempotency_key, dry_run=bool(dry_run),
        )

    @_safe
    def trigger_dry_run(self, *, kind: str, payload: dict[str, Any] | None = None,
                        target: str = "main",
                        strategy_id: str | None = None) -> dict[str, Any]:
        """Return the route decision for a trigger without executing it."""
        return self.trigger_emit(
            kind=kind, payload=payload, target=target,
            strategy_id=strategy_id, dry_run=True,
        )

    @_safe
    def trigger_routes(self) -> dict[str, Any]:
        """List configured trigger routes with their cooldown / rate limits."""
        return {"routes": self.client.triggers.list_routes()}

    # --------------------------------------------------------- proposals
    @_safe
    def proposals_list(self) -> dict[str, Any]:
        """List all evolution proposals with their state."""
        from ..evolution import patch_proposal
        props = patch_proposal.list_proposals(self.client.config.paths)
        return {"proposals": [p.asdict() for p in props]}

    @_safe
    def proposals_show(self, proposal_id: str) -> dict[str, Any]:
        """Return the manifest + rationale + diff files of a proposal."""
        if not proposal_id:
            raise ToolError("bad_request", "proposal_id required")
        d = self.client.config.paths.proposals / proposal_id
        if not d.exists():
            raise ToolError("not_found", f"proposal {proposal_id} not found")
        out: dict[str, Any] = {"id": proposal_id, "files": [p.name for p in d.iterdir()]}
        manifest = d / "proposal.yml"
        if manifest.exists():
            out["proposal"] = yaml_io.load(manifest)
        rationale = d / "rationale.md"
        if rationale.exists():
            out["rationale"] = rationale.read_text(encoding="utf-8")
        diff = d / "diff.patch"
        if diff.exists():
            out["diff"] = diff.read_text(encoding="utf-8")
        return out

    # --------------------------------------------------------- messages
    @_safe
    def messages_list(self, limit: int = 50) -> dict[str, Any]:
        """Return the last N outbound messages written to journals/messages.jsonl."""
        return {"messages": self.client.messages.list(
            limit=max(1, min(int(limit), 500))
        )}

    # --------------------------------------------------------- util for wiring
    def registry(self) -> list[dict[str, Any]]:
        """Return a static registry used by :func:`nerya.mcp.server.create_server`.

        Each entry has ``name``, ``description``, ``fn``; the server turns
        them into FastMCP ``@mcp.tool()`` wrappers.
        """
        pairs = [
            ("nerya_info", self.info),
            ("nerya_skills_list", self.skills_list),
            ("nerya_market_ticker", self.market_ticker),
            ("nerya_market_klines", self.market_klines),
            ("nerya_portfolio_summary", self.portfolio_summary),
            ("nerya_news_recent", self.news_recent),
            ("nerya_social_signals", self.social_signals),
            ("nerya_onchain_whale_events", self.onchain_whale_events),
            ("nerya_strategy_history", self.strategy_history),
            ("nerya_strategy_explain_trade", self.strategy_explain_trade),
            ("nerya_risk_preview", self.risk_preview),
            ("nerya_trigger_emit", self.trigger_emit),
            ("nerya_trigger_dry_run", self.trigger_dry_run),
            ("nerya_trigger_routes", self.trigger_routes),
            ("nerya_proposals_list", self.proposals_list),
            ("nerya_proposals_show", self.proposals_show),
            ("nerya_messages_list", self.messages_list),
        ]
        out = []
        for name, fn in pairs:
            doc = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
            out.append({"name": name, "description": doc, "fn": fn})
        return out


def tools_as_json(tools: NeryaTools) -> str:
    """Serialise the tool registry for documentation / client discovery."""
    return json.dumps(
        [{"name": r["name"], "description": r["description"]} for r in tools.registry()],
        indent=2,
    )
