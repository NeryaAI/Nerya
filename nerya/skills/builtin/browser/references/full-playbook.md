<!-- nerya-skill-frontmatter-start -->
---
name: browser
description: "Use when an agent needs to operate a live browser session: open pages, click, type, scroll, drag, inspect snapshots, read console output, inspect network/API requests, run browser-context fetches, or capture screenshots. For multi-step browsing tasks, spawn a focused browser subagent and keep the main agent as coordinator."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Browser Playbook

This skill is the agent-facing browser control surface. It wraps the
runtime browser-session API so a main agent or a dedicated subagent can
operate the configured headless browser without inventing ad-hoc shell
commands.

## When to Use

Use this skill for any task that needs live page state:

- Navigate to a URL and inspect the rendered page.
- Click, type, press keys, scroll, drag elements, or wait for selectors.
- Capture a screenshot or DOM/text snapshot.
- Read browser console messages and page errors.
- Inspect network traffic or only API-like requests (`fetch`, `xhr`,
  websocket, `/api/*`, `/graphql`).
- Call an API from inside the browser page context, preserving same-origin
  cookies/session state.

For simple article/content fetching, prefer the `research` skill first.
Escalate to this skill when JavaScript rendering, interaction, console,
or network evidence matters.

## Subagent Operating Model

For multi-step browsing, spawn one focused browser subagent and give it:

1. The target URL and expected evidence.
2. The exact browser actions it is allowed to perform.
3. The output contract: current URL, screenshot path when relevant,
   console/network findings, and a short conclusion.

Keep credentials and secrets out of prompts. The browser API redacts
common token-bearing query parameters and console/network payloads, but
the subagent should still avoid requesting or printing secrets.

## Headless Backend Strategy

Nerya supports different engines through `nerya.integrations.browser_engines`.
Choose the implementation path by capability:

| Backend | Best use | Implementation path |
| --- | --- | --- |
| `camofox` | Default agent browser for multi-step web operation | REST-backed interactive session API: `cdp_open`, `cdp_action`, `cdp_screenshot`, `cdp_close` (historical route names). Supports tab creation, accessibility snapshots with refs, click by ref/selector/coordinates, type, press, scroll, ref-based drag through `/act`, wait, screenshot, JS eval, and browser-context `fetch`. Console/network evidence is available only as Camofox trace archives, not as live event streams. |
| `cloakbrowser` | Full live-event capture and stealth Chromium | CDP/session API: `cdp_open`, `cdp_action`, `cdp_screenshot`, `cdp_close`. Supports click, type, scroll, drag, console/network capture, browser-context `fetch`, and screenshots. Use this when drag or live console/API request capture is required. |
| `lightpanda` | Fast markdown/html page dump | Simple engine API: `start`, `navigate`, `snapshot`, `screenshot`. Use for fetch/screenshot only until a CDP bridge is enabled. |
| `obscura` | Lightweight stealth HTML/screenshot | Simple engine API: `start`, `navigate`, `snapshot`, `screenshot`. Use for fetch/screenshot only until a CDP bridge is enabled. |
| Live Chrome / external CDP | Operator-owned browser session | Route through the API/browser session layer when configured; use low-level CDP only for edge cases. |

Recommended order in the UI and in agent routing is:
`camofox`, `cloakbrowser`, `lightpanda`, `obscura`.

Default rule: use `interactive=true` with `camofox` for normal agent
browser work. Switch to `cloakbrowser` when the task needs coordinate
drag or live console/network/API-event capture. Use simple mode only for
one-shot fetches or screenshot probes.

## Bundled Script

`scripts/browser_session.py` is the single JSON-in/JSON-out entry point.
It talks to `NERYA_API` (default `http://127.0.0.1:18317`) and uses
`NERYA_API_TOKEN` / `NERYA_AUTH_TOKEN` when auth is token-based.

Examples:

```bash
python -m nerya.skills.builtin.browser.scripts.browser_session --json "{\"operation\":\"status\"}"
```

```bash
python -m nerya.skills.builtin.browser.scripts.browser_session --json "{\"operation\":\"open\",\"url\":\"https://example.com\",\"interactive\":true}"
```

```bash
python -m nerya.skills.builtin.browser.scripts.browser_session --json "{\"operation\":\"click\",\"session_id\":\"bs_123\",\"selector\":\"button[type=submit]\"}"
```

```bash
python -m nerya.skills.builtin.browser.scripts.browser_session --json "{\"operation\":\"network\",\"session_id\":\"bs_123\",\"api_only\":true,\"limit\":20}"
```

Supported `operation` values:

- `registry`, `status`, `list`, `get`
- `open`, `navigate`, `snapshot`, `screenshot`, `close`
- `action` for raw `cdp_action`
- `click`, `type`, `press`, `scroll`, `drag`, `wait`,
  `wait_for_selector`
- `eval`, `console`, `network`, `api_requests`, `clear_events`
- `api_fetch`

## Evidence Contract

When reporting browser work, include:

- `session_id`
- current URL
- action result or error
- screenshot path when captured
- console/network/API evidence when relevant
- any backend limitation, for example simple engines cannot provide
  console or network events.
