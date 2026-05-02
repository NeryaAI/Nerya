"""End-to-end hackathon demo validation for Nerya.

This script validates the demo surfaces the pitch actually needs:

1. A short-cycle strategy visible in the dashboard strategy list.
2. A long-cycle strategy whose prompt depends on Agent analysis.
3. An Agent Team investment research run with durable team artifacts.
4. A self-learning / self-evolution proposal based on persisted strategy
   history evidence.
5. Dashboard routes and proxy APIs can read the created artifacts.

It intentionally uses paper/draft state only. No live trading is enabled.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_HERE = Path(__file__).resolve().parent
_NERYA_ROOT = _HERE.parent
if str(_NERYA_ROOT) not in sys.path:
    sys.path.insert(0, str(_NERYA_ROOT))


SHORT_ID = "demo_btc_5m_scalper"
LONG_ID = "demo_btc_macro_agent"

DEMO_PROMPTS = {
    "short_cycle": (
        "帮我创建一个 BTC 5 分钟短周期策略，只做 paper。"
        "它要能在策略页面看到 5m 触发器、风控限制和 risk_critic。"
    ),
    "long_agent": (
        "帮我创建一个 英伟达 长周期策略。每天先让 Agent Team 做技术、宏观、新闻和风险分析，再决定要不要进入 paper 执行。"
    ),
    "team_research": (
        "请 Agent Team 做一份 特斯拉 长周期投研 memo，结论要给信号、置信度、风险和是否允许后续策略执行，不要下实盘订单。"
    ),
    "self_evolution": (
        "请展示策略从运行证据到反思、proposal、validation、资产沉淀的"
        "自我学习进化过程，所有变更都必须 proposal-first。"
    ),
}


def _json_default(obj: Any) -> str:
    return str(obj)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )


def _post(base: str, path: str, body: dict[str, Any],
          *, timeout: float = 60.0) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = Request(
        f"{base}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
            return {"status": r.status, "body": json.loads(raw)}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        return {"status": exc.code, "body": {"error": exc.reason, "raw": raw}}
    except TimeoutError as exc:
        return {"status": 0, "body": {"error": f"timeout: {exc}"}}
    except URLError as exc:
        return {"status": 0, "body": {"error": str(exc)}}


def _get(base: str, path: str, *, timeout: float = 30.0) -> dict[str, Any]:
    try:
        with urlopen(f"{base}{path}", timeout=timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
            body: Any
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {"raw": raw}
            return {"status": r.status, "body": body, "len": len(raw)}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        return {
            "status": exc.code,
            "body": {"error": exc.reason, "raw": raw},
            "len": len(raw),
        }
    except TimeoutError as exc:
        return {"status": 0, "body": {"error": f"timeout: {exc}"}, "len": 0}
    except URLError as exc:
        return {"status": 0, "body": {"error": str(exc)}, "len": 0}


def _wait_health(base: str, timeout_s: float = 60.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        res = _get(base, "/health", timeout=2)
        if res["status"] == 200:
            return True
        time.sleep(0.5)
    return False


def _start_process(cmd: list[str], *, cwd: Path, log_path: Path,
                   env: dict[str, str] | None = None) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("w", encoding="utf-8")
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdout": log_fh,
        "stderr": subprocess.STDOUT,
        "env": env or os.environ.copy(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            except Exception:
                proc.terminate()
        else:
            proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _write_nerya_config(workspace: Path) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    cfg_path = workspace / "nerya.yml"
    cfg_path.write_text(
        "\n".join([
            "runtime:",
            "  live_trading_enabled: false",
            "  mock_mode: false",
            "  dev_mode: true",
            "llm:",
            "  default_tier: medium",
            "  tiers:",
            "    light:",
            "      provider: mock",
            "      model: mock-fast",
            "      max_tokens: 2048",
            "    medium:",
            "      provider: mock",
            "      model: mock-balanced",
            "      max_tokens: 4096",
            "    high:",
            "      provider: mock",
            "      model: mock-deep",
            "      max_tokens: 8192",
            "",
        ]),
        encoding="utf-8",
    )
    return cfg_path


def _strategy_payloads() -> list[dict[str, Any]]:
    return [
        {
            "strategy_id": SHORT_ID,
            "title": "Demo BTC 5m scalper",
            "description": "短周期演示策略：5 分钟 BTC 动量/均值回归混合，只做 paper。",
            "account_id": "paper_main",
            "markets": ["binance:BTCUSDT"],
            "trigger_kinds": ["candle.5m.close", "risk.guard"],
            "subagents": ["risk_critic"],
            "driver": "prompt",
            "status": "paper",
            "main_prompt": (
                "你是短周期 BTC 5m scalping 策略。只做 paper。"
                "每 5 分钟收盘评估动量、价差、成交量和风险 guard。"
                "必须限制单笔风险、交易频率和日内亏损。"
            ),
            "config": {
                "demo_lane": "short_cycle",
                "timeframe": "5m",
                "execution_policy": "paper_only",
                "signal_requirements": [
                    "5m_close",
                    "momentum_or_mean_reversion",
                    "spread_volume_guard",
                ],
            },
            "limits": {
                "allowed_markets": ["binance:BTCUSDT"],
                "max_single_order_usd": 250,
                "max_total_exposure_usd": 1000,
                "daily_loss_usd": 80,
                "max_drawdown_pct": 4,
                "min_confidence": 0.64,
                "max_slippage_bps": 18,
                "max_stale_seconds": 20,
                "approval_threshold_usd": 0,
                "kill_switch": False,
            },
        },
        {
            "strategy_id": LONG_ID,
            "title": "Demo BTC macro agent strategy",
            "description": "长周期演示策略：依赖 Agent Team 投研后再决定是否执行。",
            "account_id": "paper_main",
            "markets": ["binance:BTCUSDT", "macro:BTC"],
            "trigger_kinds": ["daily.research", "agent.analysis.ready"],
            "subagents": [
                "technical_analyst",
                "macro_news_analyst",
                "risk_critic",
                "portfolio_manager",
            ],
            "driver": "prompt",
            "status": "draft",
            "main_prompt": (
                "你是长周期 BTC 策略负责人。每天先让 Agent Team 完成技术、"
                "宏观、新闻、风险四类分析，再输出是否进入 paper 执行。"
                "没有 team memo 不允许下单；所有 live promotion 必须走审批。"
            ),
            "config": {
                "demo_lane": "long_cycle_agent_research",
                "timeframe": "1d",
                "requires_agent_team_memo": True,
                "required_research_lanes": [
                    "technical_analyst",
                    "macro_news_analyst",
                    "risk_critic",
                    "portfolio_manager",
                ],
                "execution_policy": "agent_analysis_then_paper",
            },
            "limits": {
                "allowed_markets": ["binance:BTCUSDT", "macro:BTC"],
                "max_single_order_usd": 500,
                "max_total_exposure_usd": 1500,
                "daily_loss_usd": 120,
                "max_drawdown_pct": 8,
                "min_confidence": 0.7,
                "max_slippage_bps": 35,
                "max_stale_seconds": 3600,
                "approval_threshold_usd": 0,
                "kill_switch": False,
            },
        },
    ]


def _create_strategies(api: str, out_dir: Path) -> list[dict[str, Any]]:
    results = []
    for payload in _strategy_payloads():
        create_payload = {
            k: v for k, v in payload.items()
            if k not in {"config", "limits"}
        }
        res = _post(api, "/strategy/create", create_payload)
        if res["status"] == 200 and not res["body"].get("ok") and "already exists" in str(res["body"].get("error")):
            res = _post(api, "/strategy/update", {
                "strategy_id": payload["strategy_id"],
                "title": payload["title"],
                "description": payload["description"],
                "markets": payload["markets"],
                "trigger_kinds": payload["trigger_kinds"],
                "subagents": payload["subagents"],
                "driver": payload["driver"],
                "prompts": {"main": payload["main_prompt"]},
                "reason": "hackathon_demo_refresh",
            })
        refresh = _post(api, "/strategy/update", {
            "strategy_id": payload["strategy_id"],
            "title": payload["title"],
            "description": payload["description"],
            "markets": payload["markets"],
            "trigger_kinds": payload["trigger_kinds"],
            "subagents": payload["subagents"],
            "driver": payload["driver"],
            "config": payload["config"],
            "limits": payload["limits"],
            "prompts": {"main": payload["main_prompt"]},
            "reason": "hackathon_demo_field_refresh",
        })
        results.append({
            "strategy_id": payload["strategy_id"],
            "response": res,
            "refresh": refresh,
        })
    _write_json(out_dir / "demo_prompts.json", DEMO_PROMPTS)
    _write_json(out_dir / "strategy_create.json", results)
    return results


def _seed_evolution_evidence(workspace: Path) -> dict[str, Any]:
    from nerya.core import jsonl
    from nerya.core.config import load_config
    from nerya.strategy_history import store

    cfg = load_config(workspace)
    now = datetime.now(timezone.utc)
    seeded: dict[str, Any] = {"short": [], "long": []}

    # Short-cycle evidence: overtrading, stale data, high slippage, loss.
    session = "demo-short-overtrade"
    store.record_trigger(cfg.paths, strategy_id=SHORT_ID, session_id=session, event={
        "name": "candle.5m.close",
        "payload": {"data_age_s": 180, "symbol": "BTCUSDT"},
    })
    for i in range(12):
        intent_id = f"demo-short-intent-{i}"
        ts = now.replace(microsecond=0).isoformat()
        store.record_intent(cfg.paths, strategy_id=SHORT_ID, session_id=session, intent={
            "intent_id": intent_id,
            "ts": ts,
            "reference_price": 100000,
            "limit_price": 100000,
            "side": "buy",
            "symbol": "BTCUSDT",
        })
        if i == 0:
            store.record_fill(cfg.paths, strategy_id=SHORT_ID, session_id=session, fill={
                "intent_id": intent_id,
                "order_id": "demo-short-fill-0",
                "price": 100900,
            })
    store.record_risk(cfg.paths, strategy_id=SHORT_ID, session_id=session, decision={
        "decision": "reject",
        "reasons": ["daily_loss_limit: demo seeded risk rejection"],
    })
    store.record_pnl(cfg.paths, strategy_id=SHORT_ID, session_id=session, pnl={
        "realized_usd": -120,
    })
    jsonl.append(cfg.paths.journal("agent"), {
        "kind": "agent.turn.start",
        "turn_id": "demo-short-correction",
        "strategy_id": SHORT_ID,
        "user_text": (
            "不是只验证一个策略，请补上 BTC 5 分钟短周期策略的过度交易、"
            "滑点和风控拒绝证据。"
        ),
    })
    seeded["short"].append(session)

    # Long-cycle evidence: team disagreement and missed opportunity.
    session = "demo-long-agent-disagreement"
    store.record_trigger(cfg.paths, strategy_id=LONG_ID, session_id=session, event={
        "name": "daily.research",
        "payload": {"data_age_s": 30, "symbol": "BTCUSDT"},
    })
    store.record_subagent(cfg.paths, strategy_id=LONG_ID, session_id=session, name="technical_analyst", output={
        "verdict": "buy",
        "confidence": 0.72,
        "reason": "daily trend improving",
    })
    store.record_subagent(cfg.paths, strategy_id=LONG_ID, session_id=session, name="risk_critic", output={
        "verdict": "hold",
        "confidence": 0.81,
        "reason": "macro event window unresolved",
    })
    store.record_risk(cfg.paths, strategy_id=LONG_ID, session_id=session, decision={
        "decision": "reject",
        "reasons": ["subagent_disagreement: risk_critic blocked buy signal"],
    })
    jsonl.append(cfg.paths.journal("agent"), {
        "kind": "agent.turn.start",
        "turn_id": "demo-long-correction",
        "strategy_id": LONG_ID,
        "user_text": (
            "不是只做短线策略，长周期策略必须先依赖 Agent Team 投研，"
            "没有 team memo 不允许进入执行。"
        ),
    })
    seeded["long"].append(session)
    return seeded


def _run_team_research(api: str, out_dir: Path) -> dict[str, Any]:
    templates = _get(api, "/teams/templates", timeout=30)
    _write_json(out_dir / "teams_templates.json", templates)
    res = _post(api, "/teams/run", {
        "template": "market_analysis_team",
        "goal": DEMO_PROMPTS["team_research"],
        "trigger": {"kind": "demo.research", "payload": {"asset": "BTC"}},
        "strategy_id": LONG_ID,
        "session_id": f"demo-team-{uuid.uuid4().hex[:8]}",
    }, timeout=180)
    _write_json(out_dir / "team_run.json", res)
    body = res.get("body") or {}
    run_id = (
        (body.get("run") or {}).get("id")
        or body.get("run_id")
        or body.get("id")
    )
    detail = None
    if run_id:
        detail = _post(api, "/teams/get", {"run_id": run_id}, timeout=60)
        _write_json(out_dir / "team_get.json", detail)
    return {"templates": templates, "run": res, "detail": detail}


def _text_blob(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default).lower()


def _contains_all(value: Any, needles: list[str]) -> bool:
    blob = _text_blob(value)
    return all(n.lower() in blob for n in needles)


def _strategy_checks(dashboard: dict[str, Any]) -> dict[str, bool]:
    short = (dashboard["api"]["short"].get("body") or {})
    long = (dashboard["api"]["long"].get("body") or {})
    strategy_rows = ((dashboard["api"]["list"].get("body") or {}).get("strategies") or [])
    strategy_ids = {r.get("id") for r in strategy_rows}
    return {
        "short_strategy_created": SHORT_ID in strategy_ids,
        "long_agent_strategy_created": LONG_ID in strategy_ids,
        "short_is_paper": ((short.get("strategy") or {}).get("status") == "paper"),
        "long_is_draft": ((long.get("strategy") or {}).get("status") == "draft"),
        "short_has_5m_trigger": "candle.5m.close" in ((short.get("strategy") or {}).get("trigger_kinds") or []),
        "short_has_risk_critic": "risk_critic" in ((short.get("strategy") or {}).get("subagents") or []),
        "short_config_declares_short_cycle": _contains_all(short, ["short_cycle", "5m", "paper_only"]),
        "short_prompt_visible_in_api": _contains_all(short.get("prompts") or {}, ["短周期", "5m", "paper"]),
        "long_has_agent_triggers": _contains_all(
            (long.get("strategy") or {}).get("trigger_kinds") or [],
            ["daily.research", "agent.analysis.ready"],
        ),
        "long_has_research_subagents": _contains_all(
            (long.get("strategy") or {}).get("subagents") or [],
            ["technical_analyst", "risk_critic", "portfolio_manager"],
        ),
        "long_requires_team_memo": bool((long.get("config") or {}).get("requires_agent_team_memo")),
        "long_prompt_blocks_without_team_memo": _contains_all(
            long.get("prompts") or {},
            ["agent team", "没有 team memo", "审批"],
        ),
    }


def _team_checks(team: dict[str, Any]) -> dict[str, bool]:
    team_body = team["run"].get("body") or {}
    detail_body = (team.get("detail") or {}).get("body") or {}
    run = detail_body.get("run") or {}
    tasks = detail_body.get("tasks") or []
    events = detail_body.get("events") or []
    blackboard = detail_body.get("blackboard") or []
    artifacts = detail_body.get("artifacts") or []
    final_report = str(detail_body.get("final_report") or "")
    completed = [t for t in tasks if t.get("status") == "completed"]
    owners = {str(t.get("owner") or "") for t in tasks}
    kinds = {str(row.get("kind") or "") for row in blackboard}
    return {
        "team_run_completed": bool(team_body.get("ok")) and run.get("status") == "completed",
        "team_template_is_market_analysis": run.get("template_id") == "market_analysis_team",
        "team_tasks_present": len(tasks) >= 4,
        "team_required_tasks_completed": len(completed) >= 4,
        "team_has_investment_research_lanes": {
            "technical-analyst",
            "sentiment-analyst",
            "risk-critic",
            "market-lead",
        }.issubset(owners),
        "team_blackboard_has_signal_evidence_risk": {"signal", "evidence", "risk"}.issubset(kinds),
        "team_artifacts_present": len(artifacts) >= 4,
        "team_events_show_process": _contains_all(events, ["phase.enter", "gates.evaluated", "run.completed"]),
        "team_final_report_has_demo_content": _contains_all(
            final_report,
            ["btc", "consensus", "tasks", "gates", "evidence"],
        ),
    }


def _evolution_checks(evolution: dict[str, Any]) -> dict[str, bool]:
    proposals = ((evolution["proposals"].get("body") or {}).get("proposals") or [])
    timeline = ((evolution["timeline"].get("body") or {}).get("timeline") or [])
    signals = ((evolution["signals"].get("body") or {}).get("signals") or [])
    reflect = evolution["reflect"].get("body") or {}
    stages = {str(item.get("stage") or "") for item in timeline}
    timeline_blob = _text_blob(timeline)
    strategy_ids = {str(item.get("strategy_id") or "") for item in timeline}
    return {
        "evolution_signals_collected": len(signals) > 0,
        "evolution_reflection_has_proposal": bool((reflect.get("proposal") or {}).get("id")),
        "evolution_proposal_created": len(proposals) > 0,
        "evolution_timeline_visible": len(timeline) > 0,
        "evolution_has_signal_stage": "signal" in stages,
        "evolution_has_proposal_stage": "proposal" in stages,
        "evolution_has_validation_stage": "validation" in stages,
        "evolution_mentions_demo_strategies": (
            SHORT_ID in strategy_ids or SHORT_ID in timeline_blob
        ) and (
            LONG_ID in strategy_ids or LONG_ID in timeline_blob
        ),
        "evolution_is_proposal_first": _contains_all(
            proposals,
            ["learning_update", "validation_plan_id", "evidence_refs"],
        ),
    }


def _browser_validate(dashboard: str, out_dir: Path) -> dict[str, Any]:
    """Render dashboard pages in Chromium and persist DOM + screenshots."""

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {
            "ok": False,
            "available": False,
            "error": f"playwright unavailable: {exc}",
        }

    pages = [
        {
            "name": "strategies",
            "path": "/strategies",
            "wait": f'[data-strategy-id="{SHORT_ID}"]',
            "needles": [SHORT_ID, LONG_ID, "candle.5m.close", "daily.research"],
        },
        {
            "name": "short_strategy_detail",
            "path": f"/strategies/{SHORT_ID}",
            "wait": "text=Subagent & prompt library",
            "needles": [SHORT_ID, "Demo BTC 5m scalper", "risk_critic", "candle.5m.close"],
        },
        {
            "name": "long_strategy_detail",
            "path": f"/strategies/{LONG_ID}",
            "wait": "text=Subagent & prompt library",
            "needles": [LONG_ID, "Demo BTC macro agent strategy", "portfolio_manager", "daily.research"],
        },
        {
            "name": "self_evolution",
            "path": "/self-evolution",
            "wait": "text=Self Evolution",
            "needles": ["Self Evolution", "Signals", "Proposal", "Validation"],
        },
    ]
    results: list[dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        for spec in pages:
            url = f"{dashboard}{spec['path']}"
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_selector(spec["wait"], timeout=60000)
            page.wait_for_timeout(500)
            text = page.locator("body").inner_text(timeout=10000)
            html = page.content()
            missing = [
                needle for needle in spec["needles"]
                if needle.lower() not in text.lower()
                and needle.lower() not in html.lower()
            ]
            shot = out_dir / "screenshots" / f"{spec['name']}.png"
            dom = out_dir / "dom" / f"{spec['name']}.txt"
            shot.parent.mkdir(parents=True, exist_ok=True)
            dom.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shot), full_page=True)
            dom.write_text(text, encoding="utf-8")
            results.append({
                "name": spec["name"],
                "url": url,
                "ok": not missing and shot.exists() and shot.stat().st_size > 5000,
                "missing": missing,
                "screenshot": str(shot),
                "dom": str(dom),
                "text_len": len(text),
            })
        browser.close()
    return {
        "ok": all(row["ok"] for row in results),
        "available": True,
        "pages": results,
    }


def _run_evolution(api: str, out_dir: Path) -> dict[str, Any]:
    signals = _post(api, "/evolution/signals", {"refresh": True, "limit": 200}, timeout=60)
    reflect = _post(api, "/evolution/reflect", {}, timeout=90)
    timeline = _post(api, "/evolution/timeline", {"limit": 160}, timeout=60)
    proposals = _post(api, "/evolution/proposals", {}, timeout=60)
    out = {
        "signals": signals,
        "reflect": reflect,
        "timeline": timeline,
        "proposals": proposals,
    }
    _write_json(out_dir / "evolution.json", out)
    return out


def _validate_dashboard(api: str, dashboard: str, out_dir: Path) -> dict[str, Any]:
    api_list = _post(api, "/strategy/list_all", {"include_archived": True})
    api_short = _post(api, "/strategy/get", {"strategy_id": SHORT_ID})
    api_long = _post(api, "/strategy/get", {"strategy_id": LONG_ID})
    pages = {
        "/dashboard": _get(dashboard, "/dashboard", timeout=20),
        "/strategies": _get(dashboard, "/strategies", timeout=20),
        f"/strategies/{SHORT_ID}": _get(dashboard, f"/strategies/{SHORT_ID}", timeout=20),
        f"/strategies/{LONG_ID}": _get(dashboard, f"/strategies/{LONG_ID}", timeout=20),
        "/self-evolution": _get(dashboard, "/self-evolution", timeout=20),
    }
    proxy = {
        "strategies": _post(dashboard, "/api/proxy/strategy/list_all", {"include_archived": True}, timeout=30),
        "short": _post(dashboard, "/api/proxy/strategy/get", {"strategy_id": SHORT_ID}, timeout=30),
        "long": _post(dashboard, "/api/proxy/strategy/get", {"strategy_id": LONG_ID}, timeout=30),
        "timeline": _post(dashboard, "/api/proxy/evolution/timeline", {"limit": 80}, timeout=30),
        "teams": _post(dashboard, "/api/proxy/teams/runs", {"limit": 20}, timeout=30),
    }
    browser = _browser_validate(dashboard, out_dir)
    out = {
        "api": {"list": api_list, "short": api_short, "long": api_long},
        "pages": pages,
        "proxy": proxy,
        "browser": browser,
    }
    _write_json(out_dir / "dashboard_validation.json", out)
    return out


def _summarize(
    *,
    strategies: list[dict[str, Any]],
    team: dict[str, Any],
    evolution: dict[str, Any],
    dashboard: dict[str, Any],
) -> dict[str, Any]:
    pages_ok = {
        path: res.get("status") == 200 and int(res.get("len") or 0) > 1000
        for path, res in dashboard["pages"].items()
    }
    proxy_body = dashboard["proxy"]["strategies"].get("body") or {}
    proxy_ids = {r.get("id") for r in (proxy_body.get("strategies") or [])}
    strategy_checks = _strategy_checks(dashboard)
    team_checks = _team_checks(team)
    evolution_checks = _evolution_checks(evolution)
    browser_ok = bool((dashboard.get("browser") or {}).get("ok"))
    checks = {
        **strategy_checks,
        "frontend_proxy_sees_short": SHORT_ID in proxy_ids,
        "frontend_proxy_sees_long": LONG_ID in proxy_ids,
        "dashboard_pages_ok": all(pages_ok.values()),
        "browser_dom_and_screenshots_ok": browser_ok,
        **team_checks,
        **evolution_checks,
    }
    team_body = team["run"].get("body") or {}
    team_tasks = ((team.get("detail") or {}).get("body") or {}).get("tasks") or []
    proposals = ((evolution["proposals"].get("body") or {}).get("proposals") or [])
    timeline = ((evolution["timeline"].get("body") or {}).get("timeline") or [])
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "demo_prompts": DEMO_PROMPTS,
        "strategy_create": strategies,
        "team_status": {
            "ok": team_body.get("ok"),
            "status": team_body.get("status"),
            "tasks": len(team_tasks),
            "run_id": (
                (team_body.get("run") or {}).get("id")
                or team_body.get("run_id")
                or team_body.get("id")
            ),
        },
        "evolution": {
            "proposal_count": len(proposals),
            "timeline_count": len(timeline),
        },
        "pages_ok": pages_ok,
        "browser": dashboard.get("browser") or {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--api-port", type=int, default=18321)
    parser.add_argument("--dashboard-port", type=int, default=3011)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument("--skip-dashboard-server", action="store_true")
    args = parser.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    workspace = (args.workspace or (_NERYA_ROOT / ".hackathon_demo_runs" / ts)).resolve()
    out_dir = (args.out or (workspace / "_validation_outputs")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_nerya_config(workspace)

    api = f"http://127.0.0.1:{args.api_port}"
    dashboard = f"http://127.0.0.1:{args.dashboard_port}"
    api_proc = None
    dash_proc = None

    try:
        api_proc = _start_process(
            [
                sys.executable, "-m", "nerya.cli.app",
                "serve", "--workspace", str(workspace),
                "--host", "127.0.0.1", "--port", str(args.api_port),
                "--no-dashboard",
            ],
            cwd=_NERYA_ROOT,
            log_path=workspace / "logs" / "api.log",
        )
        if not _wait_health(api, timeout_s=90):
            raise RuntimeError(f"API did not become healthy at {api}")

        if not args.skip_dashboard_server:
            env = os.environ.copy()
            env["NERYA_API"] = api
            env["NEXT_PUBLIC_NERYA_API_BASE"] = api
            env["PORT"] = str(args.dashboard_port)
            npm = shutil.which("npm") or shutil.which("npm.cmd")
            if npm is None:
                raise RuntimeError("npm not found; cannot start dashboard")
            dash_proc = _start_process(
                [npm, "run", "dev"],
                cwd=_NERYA_ROOT / "dashboard",
                log_path=workspace / "logs" / "dashboard.log",
                env=env,
            )
            deadline = time.time() + 90
            while time.time() < deadline:
                res = _get(dashboard, "/strategies", timeout=15)
                if res["status"] == 200 and int(res.get("len") or 0) > 1000:
                    break
                time.sleep(1)
            else:
                raise RuntimeError(f"dashboard did not become ready at {dashboard}")

        strategies = _create_strategies(api, out_dir)
        seeded = _seed_evolution_evidence(workspace)
        _write_json(out_dir / "seeded_evidence.json", seeded)
        team = _run_team_research(api, out_dir)
        evolution = _run_evolution(api, out_dir)
        dashboard_validation = _validate_dashboard(api, dashboard, out_dir)
        summary = _summarize(
            strategies=strategies,
            team=team,
            evolution=evolution,
            dashboard=dashboard_validation,
        )
        summary.update({
            "workspace": str(workspace),
            "api": api,
            "dashboard": dashboard,
            "out_dir": str(out_dir),
        })
        _write_json(out_dir / "summary.json", summary)

        print(json.dumps(summary, indent=2, ensure_ascii=False, default=_json_default))
        if args.keep_running:
            print(f"[demo] keeping API={api} dashboard={dashboard} running")
            while True:
                time.sleep(1)
        return 0 if summary["ok"] else 1
    finally:
        if not args.keep_running:
            _stop_process(dash_proc)
            _stop_process(api_proc)


if __name__ == "__main__":
    raise SystemExit(main())
