"""Control Nerya browser sessions through the local runtime API.

Standalone CLI usage::

    python -m nerya.skills.builtin.browser.scripts.browser_session \
        --json '{"operation": "open", "url": "https://example.com"}'

The script is intentionally thin: browser state, backend selection,
console/network capture, and cleanup remain inside the API runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_DEFAULT_API_BASE = "http://127.0.0.1:18317"
_INTERACTIVE_ACTIONS = {
    "api_fetch",
    "clear_events",
    "click",
    "console",
    "drag",
    "eval",
    "network",
    "api_requests",
    "press",
    "scroll",
    "type",
    "wait",
    "wait_for_selector",
}

_LEGACY_ACTION_OPERATIONS = {
    "api_fetch",
    "api_requests",
    "clear_events",
    "click",
    "console",
    "drag",
    "eval",
    "navigate",
    "network",
    "press",
    "screenshot",
    "scroll",
    "snapshot",
    "type",
    "wait",
    "wait_for_selector",
}


def _api_base(value: str | None = None) -> str:
    raw = (
        value
        or os.environ.get("NERYA_API")
        or os.environ.get("NERYA_API_BASE")
        or _DEFAULT_API_BASE
    )
    return str(raw).rstrip("/")


def _auth_token(value: str | None = None) -> str:
    return str(
        value
        or os.environ.get("NERYA_API_TOKEN")
        or os.environ.get("NERYA_AUTH_TOKEN")
        or ""
    ).strip()


def _request(
    method: str,
    path: str,
    *,
    api_base: str | None = None,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        headers["Content-Type"] = "application/json"
    resolved_token = _auth_token(token)
    if resolved_token:
        bearer = (
            resolved_token
            if resolved_token.lower().startswith("bearer ")
            else f"Bearer {resolved_token}"
        )
        clean_token = (
            resolved_token[7:].strip()
            if resolved_token.lower().startswith("bearer ")
            else resolved_token
        )
        headers["Authorization"] = bearer
        headers["X-Nerya-Token"] = clean_token

    req = Request(
        _api_base(api_base) + path,
        data=body,
        headers=headers,
        method=method.upper(),
    )
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {"ok": True}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {"body": raw}
        data.update({"ok": False, "status": exc.code, "error": data.get("error") or "http_error"})
        return data
    except URLError as exc:
        return {"ok": False, "error": "url_error", "detail": str(exc)}


def _query(path: str, params: dict[str, Any]) -> str:
    cleaned = {k: v for k, v in params.items() if v is not None and v != ""}
    if not cleaned:
        return path
    return path + "?" + urlencode(cleaned)


def _session_id(payload: dict[str, Any]) -> str:
    return str(payload.get("session_id") or "").strip()


def _latest_session_id(
    *,
    api_base: str | None,
    token: str | None,
    timeout_s: float,
) -> str:
    data = _request(
        "GET",
        "/browsers/session/list",
        api_base=api_base,
        token=token,
        timeout_s=timeout_s,
    )
    if not data.get("ok"):
        return ""
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        return ""
    for row in sessions:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("session_id") or "").strip()
        if sid and bool(row.get("cdp", True)):
            return sid
    return ""


def _with_default_session_id(
    payload: dict[str, Any],
    *,
    api_base: str | None,
    token: str | None,
    timeout_s: float,
) -> dict[str, Any]:
    if _session_id(payload):
        return payload
    sid = _latest_session_id(api_base=api_base, token=token, timeout_s=timeout_s)
    if not sid:
        return payload
    updated = dict(payload)
    updated["session_id"] = sid
    return updated


def _click_fallback_expression(selector: str) -> str:
    selector_json = json.dumps(selector, ensure_ascii=False)
    return f"""
(() => {{
  const selector = {selector_json};
  const textPrefix = 'text=';
  const isText = selector.startsWith(textPrefix);
  const wanted = isText ? selector.slice(textPrefix.length).trim() : '';
  const textOf = (el) => (el.innerText || el.textContent || el.value || '').trim();
  let el = null;
  if (isText) {{
    const candidates = Array.from(document.querySelectorAll('button,[role="button"],a,label,input,[data-option],.option,.choice,#options-container *'));
    el = candidates.find((node) => textOf(node) === wanted)
      || candidates.find((node) => textOf(node).includes(wanted));
  }} else {{
    try {{
      el = document.querySelector(selector);
    }} catch (_err) {{
      el = null;
    }}
  }}
  if (!el) {{
    return {{clicked:false, error:'not_found', selector}};
  }}
  if (typeof el.scrollIntoView === 'function') {{
    el.scrollIntoView({{block:'center', inline:'center'}});
  }}
  if (typeof el.click !== 'function') {{
    return {{clicked:false, error:'not_clickable', selector, text:textOf(el)}};
  }}
  el.click();
  return {{clicked:true, selector, text:textOf(el)}};
}})()
""".strip()


def _omit_data_uri_by_default(
    result: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    if bool(payload.get("include_data_uri", False)):
        return result
    data_uri = result.get("data_uri")
    if not isinstance(data_uri, str) or not data_uri:
        return result
    cleaned = dict(result)
    cleaned.pop("data_uri", None)
    cleaned["data_uri_omitted"] = True
    cleaned["data_uri_length"] = len(data_uri)
    return cleaned


def _normalise_legacy_action_payload(
    action: str,
    payload: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Map old ``operation=action`` examples to first-class operations.

    Early browser skill docs showed calls such as
    ``{"operation":"action","action":"click"}``, but the runtime API
    exposes high-level actions as direct operations. Keep those old
    payloads working so a model copying prior examples does not produce
    long runs of avoidable ``unknown_action`` browser failures.
    """

    op = str(action or "").strip().lower()
    if op not in _LEGACY_ACTION_OPERATIONS:
        return None
    normalised = dict(payload)
    normalised.pop("operation", None)
    normalised.pop("action", None)
    if op == "eval" and not normalised.get("expression"):
        for key in ("script", "js", "code"):
            if normalised.get(key):
                normalised["expression"] = normalised[key]
                break
    if op == "wait" and not normalised.get("ms") and normalised.get("seconds") is not None:
        try:
            normalised["ms"] = int(float(normalised["seconds"]) * 1000)
        except (TypeError, ValueError):
            pass
    if op == "scroll":
        if "dx" not in normalised and "x" in normalised:
            normalised["dx"] = normalised["x"]
        if "dy" not in normalised and "y" in normalised:
            normalised["dy"] = normalised["y"]
    return op, normalised


def _cdp_action(
    action: str,
    payload: dict[str, Any],
    *,
    api_base: str | None,
    token: str | None,
    timeout_s: float,
) -> dict[str, Any]:
    payload = _with_default_session_id(
        payload,
        api_base=api_base,
        token=token,
        timeout_s=timeout_s,
    )
    sid = _session_id(payload)
    if not sid:
        return {"ok": False, "error": "session_id is required"}
    params = dict(payload.get("payload") or {})
    for key, value in payload.items():
        if key not in {"operation", "api_base", "token", "timeout_s", "session_id", "action", "payload"}:
            params.setdefault(key, value)
    return _request(
        "POST",
        "/browsers/session/cdp_action",
        api_base=api_base,
        token=token,
        payload={"session_id": sid, "action": action, "payload": params},
        timeout_s=timeout_s,
    )


def run(
    *,
    operation: str = "status",
    api_base: str | None = None,
    token: str | None = None,
    timeout_s: float = 60.0,
    **payload: Any,
) -> dict[str, Any]:
    op = str(operation or "status").strip().lower()
    timeout_s = float(timeout_s or payload.get("timeout_s") or 60.0)

    if op == "registry":
        return _request("GET", "/browsers/registry", api_base=api_base, token=token, timeout_s=timeout_s)
    if op == "status":
        return _request("GET", "/browsers/status", api_base=api_base, token=token, timeout_s=timeout_s)
    if op == "list":
        return _request("GET", "/browsers/session/list", api_base=api_base, token=token, timeout_s=timeout_s)
    if op == "get":
        sid = _session_id(payload)
        if not sid:
            return {"ok": False, "error": "session_id is required"}
        return _request(
            "GET",
            _query("/browsers/session/get", {"session_id": sid}),
            api_base=api_base,
            token=token,
            timeout_s=timeout_s,
        )

    if op == "open":
        if not payload.get("url"):
            return {"ok": False, "error": "url is required"}
        interactive = bool(payload.get("interactive", True))
        endpoint = "/browsers/session/cdp_open" if interactive else "/browsers/session/start"
        return _request("POST", endpoint, api_base=api_base, token=token, payload=payload, timeout_s=timeout_s)

    if op == "navigate":
        if bool(payload.get("interactive", True)):
            return _cdp_action("goto", payload, api_base=api_base, token=token, timeout_s=timeout_s)
        return _request(
            "POST",
            "/browsers/session/navigate",
            api_base=api_base,
            token=token,
            payload=payload,
            timeout_s=timeout_s,
        )

    if op == "snapshot":
        payload = _with_default_session_id(
            payload,
            api_base=api_base,
            token=token,
            timeout_s=timeout_s,
        )
        if bool(payload.get("interactive", True)):
            return _cdp_action("snapshot", payload, api_base=api_base, token=token, timeout_s=timeout_s)
        return _request(
            "POST",
            "/browsers/session/snapshot",
            api_base=api_base,
            token=token,
            payload=payload,
            timeout_s=timeout_s,
        )

    if op == "screenshot":
        payload = _with_default_session_id(
            payload,
            api_base=api_base,
            token=token,
            timeout_s=timeout_s,
        )
        endpoint = (
            "/browsers/session/cdp_screenshot"
            if bool(payload.get("interactive", True))
            else "/browsers/session/screenshot"
        )
        result = _request(
            "POST",
            endpoint,
            api_base=api_base,
            token=token,
            payload=payload,
            timeout_s=timeout_s,
        )
        return _omit_data_uri_by_default(result, payload)

    if op == "close":
        payload = _with_default_session_id(
            payload,
            api_base=api_base,
            token=token,
            timeout_s=timeout_s,
        )
        sid = _session_id(payload)
        if not sid:
            return {"ok": False, "error": "session_id is required"}
        if bool(payload.get("interactive", True)):
            cdp = _request(
                "POST",
                "/browsers/session/cdp_close",
                api_base=api_base,
                token=token,
                payload={"session_id": sid},
                timeout_s=timeout_s,
            )
            if not bool(payload.get("drop_session", True)):
                return cdp
        closed = _request(
            "POST",
            "/browsers/session/close",
            api_base=api_base,
            token=token,
            payload={"session_id": sid},
            timeout_s=timeout_s,
        )
        if "cdp" in locals():
            closed["cdp_close"] = cdp
        return closed

    if op == "action":
        action = str(payload.get("action") or "").strip()
        if not action:
            return {"ok": False, "error": "action is required"}
        legacy = _normalise_legacy_action_payload(action, payload)
        if legacy is not None:
            legacy_op, legacy_payload = legacy
            return run(
                operation=legacy_op,
                api_base=api_base,
                token=token,
                timeout_s=timeout_s,
                **legacy_payload,
            )
        return _cdp_action(action, payload, api_base=api_base, token=token, timeout_s=timeout_s)

    if op == "click":
        action = "click_selector" if payload.get("selector") else "click_xy"
        result = _cdp_action(action, payload, api_base=api_base, token=token, timeout_s=timeout_s)
        selector = str(payload.get("selector") or "").strip()
        if result.get("ok") or not selector:
            return result
        fallback_payload = dict(payload)
        fallback_payload["expression"] = _click_fallback_expression(selector)
        fallback = _cdp_action(
            "eval",
            fallback_payload,
            api_base=api_base,
            token=token,
            timeout_s=timeout_s,
        )
        if fallback.get("ok"):
            fallback["fallback_for"] = {"action": action, "selector": selector}
            return fallback
        return result
    if op == "console":
        return _cdp_action("get_console", payload, api_base=api_base, token=token, timeout_s=timeout_s)
    if op == "network":
        return _cdp_action("get_network", payload, api_base=api_base, token=token, timeout_s=timeout_s)
    if op == "api_requests":
        return _cdp_action("get_api_requests", payload, api_base=api_base, token=token, timeout_s=timeout_s)

    if op in _INTERACTIVE_ACTIONS:
        action = {
            "eval": "eval",
            "clear_events": "clear_events",
        }.get(op, op)
        return _cdp_action(action, payload, api_base=api_base, token=token, timeout_s=timeout_s)

    return {"ok": False, "error": f"unknown_operation: {op}"}


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
    parser.add_argument("--operation", "--op", dest="operation", default=None)
    parser.add_argument("--api-base", dest="api_base", default=None)
    parser.add_argument("--token", dest="token", default=None)
    args = parser.parse_args()

    payload = _load_payload(args)
    operation = args.operation or payload.pop("operation", "status")
    api_base = args.api_base or payload.pop("api_base", None)
    token = args.token or payload.pop("token", None)
    timeout_s = float(payload.pop("timeout_s", 60.0) or 60.0)
    result = run(
        operation=operation,
        api_base=api_base,
        token=token,
        timeout_s=timeout_s,
        **payload,
    )
    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")
    if isinstance(result, dict) and result.get("ok") is False:
        sys.exit(1)


if __name__ == "__main__":
    main()
