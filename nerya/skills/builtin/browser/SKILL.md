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

Use for pages that require DOM inspection, JavaScript, clicks, forms,
screenshots, console/network evidence, or viewport-level visual QA.

## Flow

IF a plain HTTP fetch proves the fact:
USE the lighter fetch path.

IF JavaScript, login, scrolling, canvas, or interaction matters:
OPEN browser session.
CAPTURE page state before acting.
ACT with the smallest click/type/navigation step.
VERIFY with DOM, screenshot, console, `api_requests`, or network evidence.
RETURN only the evidence relevant to the user task.

## Scripts

- `scripts/browser_session.py` for browser session helpers.

Use `script_run` directly for normal browser work. Do not inspect the
script first unless you are changing the script or the operation is
unknown. The script is JSON-in / JSON-out.

Open an interactive session:

```json
{
  "skill_id": "browser",
  "name": "browser_session.py",
  "args": [
    "--json",
    "{\"operation\":\"open\",\"session_id\":\"browser-smoke-1\",\"url\":\"https://example.com\",\"interactive\":true,\"engine\":\"cloakbrowser\",\"wait_until\":\"commit\",\"timeout_s\":30}"
  ]
}
```

Inspect DOM state with a direct operation. JavaScript goes in
`expression`:

```json
{
  "skill_id": "browser",
  "name": "browser_session.py",
  "args": [
    "--json",
    "{\"operation\":\"eval\",\"session_id\":\"browser-smoke-1\",\"expression\":\"document.title\",\"interactive\":true,\"timeout_s\":20}"
  ]
}
```

Click a selector:

```json
{
  "skill_id": "browser",
  "name": "browser_session.py",
  "args": [
    "--json",
    "{\"operation\":\"click\",\"session_id\":\"browser-smoke-1\",\"selector\":\"button[type=submit]\",\"interactive\":true,\"timeout_s\":20}"
  ]
}
```

Capture page evidence:

```json
{
  "skill_id": "browser",
  "name": "browser_session.py",
  "args": [
    "--json",
    "{\"operation\":\"snapshot\",\"session_id\":\"browser-smoke-1\",\"interactive\":true,\"timeout_s\":20}"
  ]
}
```

Close the session:

```json
{
  "skill_id": "browser",
  "name": "browser_session.py",
  "args": [
    "--json",
    "{\"operation\":\"close\",\"session_id\":\"browser-smoke-1\",\"interactive\":true,\"timeout_s\":20}"
  ]
}
```

Use direct operations for normal browser work. `operation:"action"` is
only a compatibility path for older examples and low-level CDP actions.

Common interactive operations: `snapshot`, `screenshot`, `click`,
`type`, `press`, `scroll`, `drag`, `eval`, `console`, `network`,
`api_requests`, `api_fetch`, `wait`, and `wait_for_selector`.

## Lazy References

- `references/full-playbook.md` for browser-agent operating rules.
