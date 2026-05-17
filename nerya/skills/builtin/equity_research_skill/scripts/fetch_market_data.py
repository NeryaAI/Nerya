"""CLI wrapper for ``EquitiesClient`` market-data surfaces (price, news, insiders).

CLI usage::

    python -m nerya.skills.builtin.equity_research_skill.scripts.fetch_market_data \\
        --json '{"ticker": "AAPL", "command": "news", "limit": 20}'

Supported ``command`` values:

| value           | behaviour                          |
|-----------------|------------------------------------|
| ``price``       | latest price snapshot              |
| ``prices``      | price history                      |
| ``news``        | recent news                        |
| ``insider``     | insider trades                     |
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .....data.equities import EquitiesClient

# ``nerya.charting`` is imported lazily inside ``_maybe_attach_chart``
# — eager imports would form a cycle through ``nerya.agent`` during
# CLI bootstrap (same reason as ``backtest/scripts/render_chart.py``).


def _maybe_attach_chart(
    *,
    payload: dict[str, Any],
    ticker: str,
    interval: str,
    workspace: str | None,
) -> None:
    """If the upstream returned price history, attach a chart_blocks entry.

    We attempt OHLCV first (more informative); fall back to a single
    close-line if the upstream only ships closes. Path defaults to
    ``bulk`` when a workspace was given so the LLM context stays lean
    even on multi-year history; standalone CLI calls degrade to inline.
    """

    rows = payload.get("prices") or payload.get("data") or []
    if not isinstance(rows, list) or not rows:
        return

    from .....charting import (  # local import — see top-of-file note
        BulkContext,
        candle_chart_from_rows,
        line_chart_from_rows,
    )
    from .....core.paths import WorkspacePaths
    from .....workspace.artifact_store import ArtifactStore

    path = "bulk" if workspace else "inline"
    ctx = None
    if path == "bulk" and workspace:
        try:
            ctx = BulkContext(
                artifact_store=ArtifactStore(
                    WorkspacePaths(root=Path(workspace).expanduser().resolve())
                ),
            )
        except Exception:
            # Workspace unavailable / unwritable — degrade to inline so
            # the agent still sees *something*. This is rare (the agent
            # daemon's workspace is always writable).
            path = "inline"
            ctx = None

    title = f"{ticker} · {interval} · {len(rows)} bars"
    chart = candle_chart_from_rows(
        rows,
        title=title,
        skill="equity_research",
        action="fetch_market_data",
        path=path,
        ctx=ctx,
        as_of=str(payload.get("as_of") or ""),
    )
    if chart is None:
        chart = line_chart_from_rows(
            rows,
            title=title,
            skill="equity_research",
            action="fetch_market_data",
            series_name="close",
            color="#22c55e",
            path=path,
            ctx=ctx,
            as_of=str(payload.get("as_of") or ""),
        )
    if chart is not None:
        payload["chart_blocks"] = [chart]


def _apply_dependency_guidance(payload: dict[str, Any]) -> dict[str, Any] | None:
    env = payload.get("_envelope") or {}
    if not isinstance(env, dict):
        return None
    if env.get("missing_key") or "Financial Datasets API key is not configured" in str(
        env.get("error") or "",
    ):
        guidance = env.get("setup_guidance")
        if isinstance(guidance, dict):
            payload["dependency_guidance"] = guidance
        payload["error"] = str(env.get("error") or "dependency missing")
        return guidance if isinstance(guidance, dict) else {"error": "dependency_missing"}
    return None


def run(
    *,
    ticker: str,
    command: str = "price",
    limit: int = 20,
    interval: str = "day",
    workspace: str | None = None,
) -> dict[str, Any]:
    if not ticker:
        return {"ok": False, "error": "ticker is required"}

    client = EquitiesClient()
    started = time.monotonic()
    dependency_guidance: dict[str, Any] | None = None

    cmd = command.lower().strip()
    if cmd in ("price", "prices"):
        payload = client.prices(ticker, interval=interval, limit=limit)
        _maybe_attach_chart(
            payload=payload,
            ticker=ticker,
            interval=interval,
            workspace=workspace,
        )
    elif cmd == "news":
        payload = client.news(ticker, limit=limit)
    elif cmd in ("insider", "insider_trades"):
        payload = client.insider_trades(ticker, limit=limit)
    elif cmd in ("snapshot", "metrics"):
        payload = client.metrics_snapshot(ticker)
    else:
        return {"ok": False, "error": f"unsupported command: {command!r}"}

    guidance = _apply_dependency_guidance(payload)
    if guidance is not None:
        dependency_guidance = guidance

    payload["ok"] = guidance is None
    payload["ticker"] = ticker
    payload["command"] = cmd
    payload["dependency_guidance"] = dependency_guidance
    payload["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return payload


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    if args.payload_json:
        return json.loads(args.payload_json) or {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", dest="payload_json", default=None)
    parser.add_argument("--payload-file", dest="payload_file", default=None)
    parser.add_argument("--ticker", dest="ticker", default=None)
    parser.add_argument("--command", dest="command", default=None)
    parser.add_argument("--workspace", dest="workspace", default=None)
    args = parser.parse_args()

    payload = _load_payload(args)
    ticker = (args.ticker or payload.get("ticker") or "").strip().upper()
    command = args.command or payload.get("command") or "price"

    try:
        result = run(
            ticker=ticker,
            command=command,
            limit=int(payload.get("limit") or 20),
            interval=str(payload.get("interval") or "day"),
            workspace=args.workspace or payload.get("workspace"),
        )
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
