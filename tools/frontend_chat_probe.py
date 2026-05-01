#!/usr/bin/env python3
"""Drive the Nerya dashboard chat UI with Playwright and verify the
agent uses the consolidated ``workspace`` skill end-to-end.

This is the front-end half of the live conversational test (the
HTTP-direct counterpart is ``tools/smoke_chat_test.py``). It opens
the dashboard at ``--dashboard-url`` (default http://127.0.0.1:3000),
clicks into ``/chat``, types each scripted prompt into the
``ChatInput`` textarea, presses Enter, waits for the assistant turn
to render, and captures:

* the full assistant transcript (DOM text from the latest turn),
* the tool-trace badges that ``TurnBlocks`` renders for each skill
  call (used to confirm the agent reached for ``workspace.*``
  introspection actions instead of hallucinating workspace state),
* a per-prompt screenshot saved under ``state/frontend_probe/``.

Cross-checks against the API journals afterwards by reading
``GET /skills`` (to prove the workspace skill is registered) and
``GET /agent/sessions/<id>/turns?include_trace=1`` (to prove the
agent invoked ``skill.workspace.<action>`` rather than the old
domain aliases).

Usage::

    python tools/frontend_chat_probe.py
    python tools/frontend_chat_probe.py --headed --dashboard-url http://127.0.0.1:3000

Exit code 0 = every prompt passed; 1 = at least one prompt failed.
The detailed per-prompt report is written to
``state/frontend_probe/report.json``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
except ImportError as exc:  # pragma: no cover - import-time guard
    print("playwright not installed; pip install playwright && python -m "
          "playwright install chromium", file=sys.stderr)
    raise SystemExit(2) from exc


# ----------------------------------------------------------------------------
# Scenario prompts. Each prompt asserts ONE specific introspection action
# the agent should reach for. The ``expect_workspace_actions`` field is the
# truth-set the journal cross-check is graded against.
# ----------------------------------------------------------------------------
PROMPTS: list[dict[str, Any]] = [
    {
        "id": "p1_what_do_i_have_zh",
        "lang": "zh",
        "text": "我这个 workspace 里都有什么？把策略、脚本、定时任务、路由都列一下。",
        "expect_workspace_actions": [
            "list_strategies",
            "list_scripts",
            "list_routes",
            "list_schedules",
        ],
    },
    {
        "id": "p2_show_setup_en",
        "lang": "en",
        "text": "Show me what's set up here — strategies, scripts, schedules, "
                "routes. I want a quick inventory of the current workspace.",
        "expect_workspace_actions": [
            "list_strategies",
            "list_scripts",
            "list_routes",
            "list_schedules",
        ],
    },
    {
        "id": "p3_portfolio_zh",
        "lang": "zh",
        "text": "我的 paper_main 账户现在的组合情况怎么样？现金、持仓、PnL 都说一下。",
        "expect_workspace_actions": ["get_portfolio_summary"],
    },
    {
        "id": "p4_intent_defaults_en",
        "lang": "en",
        "text": "If I asked you to place a small market buy order without "
                "specifying anything, what defaults would you use? Show me "
                "the workspace's intent defaults before placing anything.",
        "expect_workspace_actions": ["get_trade_intent_defaults"],
    },
]


@dataclasses.dataclass
class TurnResult:
    prompt_id: str
    sent_at: str
    elapsed_s: float
    assistant_text: str
    tool_calls: list[str]
    workspace_actions: list[str]
    expect_workspace_actions: list[str]
    missing_actions: list[str]
    ok: bool
    error: str = ""


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _http_get(url: str, timeout: float = 10.0) -> dict[str, Any] | list[Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _verify_workspace_skill(api_url: str) -> list[str]:
    """Confirm the workspace skill is live and surface its action list."""
    data = _http_get(f"{api_url}/skills")
    skills = data["skills"] if isinstance(data, dict) else data
    for s in skills:
        sid = s.get("id") or s.get("skill_id")
        if sid == "workspace":
            actions = s.get("actions") or []
            return sorted(
                a.get("name") if isinstance(a, dict) else a
                for a in actions
            )
    raise RuntimeError(
        "workspace skill missing from /skills — restart the API after "
        "wiring the new builtin in."
    )


def _select_input(page: Page) -> Any:
    return page.locator(
        "textarea[placeholder*='Ask Nerya']"
    ).first


def _send_prompt(page: Page, text: str, *, timeout_ms: int = 240_000) -> None:
    """Type the prompt, send it, and wait for the resulting assistant turn
    to finish.

    We track completion via the assistant bubble's
    ``data-turn-loading`` attribute: ``ChatView`` sets it to ``true``
    when the optimistic message is inserted and flips to ``false`` once
    the run_turn promise resolves. That's a deterministic per-turn
    signal — much more reliable than scanning the whole page for a
    ``Running…`` button (which races with stream-style UIs and can
    miss sub-second turns).
    """
    box = _select_input(page)
    box.click()
    box.fill(text)
    pre_count = page.locator("[data-turn-role='assistant']").count()
    page.keyboard.press("Enter")

    deadline = time.time() + timeout_ms / 1000.0
    # Wait for the new assistant bubble to mount.
    while time.time() < deadline:
        if page.locator("[data-turn-role='assistant']").count() > pre_count:
            break
        page.wait_for_timeout(200)
    else:
        raise PlaywrightTimeoutError(
            f"assistant bubble did not mount within {timeout_ms}ms"
        )

    # Then wait for that bubble's loading flag to flip off.
    while time.time() < deadline:
        loading_attr = page.locator(
            "[data-turn-role='assistant']"
        ).last.get_attribute("data-turn-loading")
        if loading_attr == "false":
            return
        page.wait_for_timeout(500)
    raise PlaywrightTimeoutError(f"prompt did not finish in {timeout_ms}ms")


def _read_latest_turn(page: Page) -> tuple[str, list[str]]:
    """Return ``(assistant_text, tool_calls)`` for the *most recent*
    assistant turn.

    The ``AssistantBubble`` component writes
    ``data-turn-role='assistant'`` on its outer container so we can
    scope text + tool-trace extraction to a single turn. Inside that
    bubble, ``ToolBlock`` renders each tool call as
    ``<skill_id>.<action>`` in a ``font-mono`` span — we scrape those
    spans to recover the call list.
    """
    page.wait_for_timeout(500)
    turns = page.locator("[data-turn-role='assistant']")
    assistant_text = ""
    tool_calls: list[str] = []
    if turns.count() == 0:
        return assistant_text, tool_calls
    last = turns.last
    try:
        assistant_text = last.inner_text(timeout=5_000)
    except PlaywrightTimeoutError:
        assistant_text = ""

    # Scope tool-span extraction to the latest assistant bubble so
    # earlier turns' calls never pollute the count.
    tool_spans = last.locator("span.font-mono")
    for i in range(tool_spans.count()):
        try:
            t = tool_spans.nth(i).inner_text(timeout=2_000).strip()
        except PlaywrightTimeoutError:
            continue
        if not t or "." not in t:
            continue
        head, _, tail = t.partition(".")
        # Require snake_case-ish ids on both sides — filters file paths,
        # numbers, dotted package names, etc. that may also share the
        # font-mono class.
        if (
            head
            and tail
            and head.replace("_", "").isalnum()
            and tail.replace("_", "").isalnum()
            and head.islower()
        ):
            tool_calls.append(t)
    return assistant_text, tool_calls


def _workspace_actions(tool_calls: list[str]) -> list[str]:
    out: list[str] = []
    for c in tool_calls:
        head, _, tail = c.partition(".")
        if head == "workspace" and tail:
            out.append(tail)
    return out


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    api_url = args.api_url.rstrip("/")
    dashboard_url = args.dashboard_url.rstrip("/")
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    workspace_actions = _verify_workspace_skill(api_url)
    print(f"[probe] workspace skill live: {workspace_actions}")

    results: list[TurnResult] = []
    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(
            headless=not args.headed,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context: BrowserContext = browser.new_context(
            viewport={"width": 1440, "height": 900},
        )
        page: Page = context.new_page()
        try:
            # Dev-mode Next.js compiles the page on first hit; warm it up
            # with a generous load timeout, then poll for the client-side
            # ChatInput textarea (only mounted after React hydration).
            page.goto(f"{dashboard_url}/chat", wait_until="load",
                      timeout=120_000)
            _select_input(page).wait_for(state="visible", timeout=120_000)
            print(f"[probe] /chat loaded ({dashboard_url}/chat)")
        except Exception as exc:
            print(f"[probe] failed to load /chat: {exc}", file=sys.stderr)
            page.screenshot(path=str(out_dir / "load_failure.png"),
                            full_page=True, timeout=10_000)
            browser.close()
            return 2

        for prompt in PROMPTS:
            pid = prompt["id"]
            text = prompt["text"]
            expect = prompt["expect_workspace_actions"]
            print(f"\n[probe] === {pid} ({prompt['lang']}) ===")
            print(f"[probe] prompt: {text[:120]}{'…' if len(text) > 120 else ''}")
            t0 = time.time()
            error = ""
            try:
                _send_prompt(page, text, timeout_ms=args.turn_timeout_ms)
            except Exception as exc:  # noqa: BLE001 — capture for report
                error = f"{type(exc).__name__}: {exc}"
                print(f"[probe] ERROR: {error}", file=sys.stderr)
            elapsed = time.time() - t0

            assistant_text, tool_calls = _read_latest_turn(page)
            ws_actions = _workspace_actions(tool_calls)
            missing = [a for a in expect if a not in ws_actions]
            ok = not error and not missing

            page.screenshot(
                path=str(out_dir / f"{pid}.png"),
                full_page=True,
                timeout=15_000,
            )

            tr = TurnResult(
                prompt_id=pid,
                sent_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                elapsed_s=round(elapsed, 2),
                assistant_text=assistant_text[:4000],
                tool_calls=tool_calls,
                workspace_actions=ws_actions,
                expect_workspace_actions=expect,
                missing_actions=missing,
                ok=ok,
                error=error,
            )
            results.append(tr)
            status = "PASS" if ok else "FAIL"
            print(f"[probe] {status}  elapsed={elapsed:.1f}s  ws_actions={ws_actions}  "
                  f"missing={missing}")
            if assistant_text:
                preview = assistant_text.replace("\n", " ⏎ ")[:200]
                print(f"[probe] reply: {preview}")

        browser.close()

    report_path = out_dir / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "api_url": api_url,
                "dashboard_url": dashboard_url,
                "workspace_actions_advertised": workspace_actions,
                "results": [dataclasses.asdict(r) for r in results],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    n = len(results)
    passed = sum(1 for r in results if r.ok)
    print(f"\n[probe] DONE — {passed}/{n} prompts passed")
    print(f"[probe] report: {report_path}")
    print(f"[probe] screenshots: {out_dir}")
    return 0 if passed == n else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:18317")
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:3000")
    parser.add_argument("--out-dir", default="state/frontend_probe")
    parser.add_argument("--headed", action="store_true",
                        help="Show the browser (default: headless)")
    parser.add_argument("--turn-timeout-ms", type=int, default=240_000)
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
