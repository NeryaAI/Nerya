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
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


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
    configured = str(os.environ.get("NERYA_API") or os.environ.get("NERYA_API_BASE") or _DEFAULT_API_BASE).rstrip("/")
    raw = str(value or configured).rstrip("/")
    parts = urlsplit(raw)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment or any(c in raw for c in "\r\n\x00")):
        raise ValueError("invalid_configured_api_base")
    _ = parts.port
    if raw != configured:
        raise ValueError("api_base_override_requires_operator_configuration")
    return raw


class _NoApiRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "API redirects are disabled", headers, None)


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
        with build_opener(_NoApiRedirect()).open(req, timeout=timeout_s) as resp:
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


def _managed_run(op: str, payload: dict[str, Any], *, api_base, token, timeout_s) -> dict[str, Any]:
    """One request returns the action receipt and fresh observation. No auto retry."""
    nested = payload.get("payload") or {}
    if not isinstance(nested, dict):
        return {"ok":False, "error":"payload_must_be_object", "retryable":False}
    data = {**nested, **payload}
    data.pop("payload", None)
    data.pop("backend", None)
    data.pop("engine", None)
    if op == "action":
        op = str(data.pop("action", ""))
    op = {"type": "fill", "get": "status", "list": "list", "wait": "wait_for",
          "wait_for_selector": "wait_for", "console": "events", "network": "network",
          "api_requests": "network", "go_back": "back", "go_forward": "forward"}.get(op, op)
    if "target" not in data:
        target = {k:data[k] for k in ("ref", "role", "name", "label", "test_id", "selector", "frame_id") if k in data}
        if target:
            data["target"] = target
    request_id = data.setdefault("request_id", uuid.uuid4().hex)
    data.setdefault("profile_id", "work")
    data["operation"] = op
    # Correlation is stamped by the executor, never read from model arguments.
    data.pop('_trace_token', None)
    if os.environ.get('NERYA_BROWSER_TRACE_TOKEN'):
        data['_trace_token'] = os.environ['NERYA_BROWSER_TRACE_TOKEN']
    # Never borrow a different task's most-recent tab/session implicitly.
    if op not in {"open", "list", "status"} and not data.get("session_id"):
        return {"ok":False, "error":"session_id_required_use_open_receipt", "request_id":request_id}
    try:
        result = _request("POST", "/browsers/agent", api_base=api_base, token=token,
                          payload=data, timeout_s=max(40, min(timeout_s, 60)))
        result.setdefault("request_id", request_id)
        if not result.get("ok"):
            result.setdefault("retryable", False)
            result.setdefault("next_action", "inspect_state_or_reuse_exact_request_id")
        return result
    except ValueError:
        return {"ok":False, "error":"invalid_or_unapproved_api_base", "request_id":request_id, "retryable":False}
    except (TimeoutError, OSError):
        return {"ok":False, "error":"transport_outcome_unknown", "request_id":request_id,
                "retryable":False, "next_action":"inspect_state_or_reuse_exact_request_id"}


def run(*, operation: str = 'status', api_base: str | None = None,
        token: str | None = None, timeout_s: float = 60.0, **payload: Any) -> dict[str, Any]:
    """All browser tasks use the same managed Chromium runtime."""
    op = str(operation or 'status').strip().lower()
    if (payload.get('backend', 'managed') != 'managed'
            or payload.get('engine') not in (None, '', 'chromium')
            or str(payload.get('session_id', '')).startswith('bs_')):
        return {'ok':False, 'error':'legacy_browser_removed_use_managed', 'retryable':False}
    if op == 'registry':
        return {'ok':True, 'engine':'chromium', 'managed':True}
    return _managed_run(op, payload, api_base=api_base, token=token, timeout_s=float(timeout_s or 60))


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
