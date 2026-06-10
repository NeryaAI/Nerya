<!-- nerya-skill-frontmatter-start -->
---
name: browser
description: "Use when the agent must operate, inspect, or verify a live browser session."
version: 0.1.0
license: MIT
author: Nerya
---
<!-- nerya-skill-frontmatter-end -->

# Browser

Use for rendered pages that need DOM inspection, JavaScript, clicks,
forms, screenshots, console/network evidence, or viewport-level visual QA.

## Flow

IF a plain HTTP fetch proves the fact:
USE the lighter fetch/research path.

IF JavaScript, login, scrolling, canvas, or interaction matters:
OPEN a browser session.
CAPTURE page state before acting.
ACT with the smallest click/type/navigation step.
VERIFY with DOM, screenshot, console, `api_requests`, or network evidence.
RETURN only the evidence relevant to the user task.

For multi-step browsing, spawn one focused browser subagent and keep the
main agent as coordinator.

## Script

RUN `scripts/browser_session.py` through `script_run`; it is JSON-in /
JSON-out and talks to the configured Nerya API.

Common operations:

- `open`, `navigate`, `snapshot`, `screenshot`, `close`
- `click`, `type`, `press`, `scroll`, `drag`, `wait`, `wait_for_selector`
- `eval`, `console`, `network`, `api_requests`, `api_fetch`

Use `interactive=true` for normal browser work. Use `cloakbrowser` when
live console/network/API-event capture or coordinate drag is required.

## Evidence Contract

Report:

- `session_id`
- current URL
- action result or error
- screenshot path when captured
- console/network/API evidence when relevant
- backend limitation when the selected engine cannot provide an evidence type

## Lazy References

- `references/full-playbook.md` for browser operating rules, backend
  capabilities, and JSON command examples.
