"""Operator-only control plane, deliberately not exposed by the browser Skill."""
from __future__ import annotations

from ..integrations import managed_browser as browser


def routes():
    def dispatch(client, payload):
        body = payload if isinstance(payload, dict) else {}
        root = client.config.paths.root
        try:
            profile = browser.identifier(body.get("profile_id", "work"))
            operation = body.get("operation", "status")
            if operation in {'network', 'network_detail'}:
                worker = browser.worker_for(root, profile)
                if worker is None:
                    return {'ok':True, 'listening':False, 'requests':[], 'cursor':0}
                if operation == 'network':
                    return worker.network.read(body)
                return worker.call('network_detail', body, timeout=15)
            if operation in {'preferences', 'history', 'history_clear', 'surface', 'human_command', 'extension_apply'}:
                from ..integrations import browser_workspace as workspace
                worker = browser.worker_for(root, profile)
                if operation == 'preferences':
                    prefs = workspace.preferences(root, profile)
                    if 'automatic' in body:
                        prefs = workspace.set_preferences(root, profile, body['automatic'])
                        if worker and not prefs['automatic']:
                            worker.call('agent_revoke', {})
                    return {'ok':True, 'preferences':prefs}
                if operation in {'history', 'history_clear'}:
                    if operation == 'history_clear':
                        workspace.clear_history(root, profile)
                    return {'ok':True, 'history':workspace.history(root, profile)}
                if operation == 'extension_apply':
                    if worker and (not worker.human_control or (worker.agent_controller and worker.agent_controller.in_action)):
                        raise browser.BrowserError('take_over_before_changing_extensions')
                    config = browser.save_profile(root, profile, body.get('extensions', []))
                    if worker:
                        worker.close()
                        if not worker.done.wait(10):
                            raise browser.BrowserError('browser_still_closing')
                        worker = browser.open_browser(root, profile)
                        worker.call('handoff', {})
                    return {'ok':True, 'config':config, 'restarted':worker is not None}
                if operation == 'human_command':
                    if worker is None:
                        raise browser.BrowserError('open_browser_first')
                    worker.call('human_command', body, timeout=35)
                if worker is None:
                    state = {'running':False, 'paused':False, 'human_control':False, 'tabs':[]}
                elif worker.agent_controller and worker.agent_controller.in_action:
                    state = {**worker.surface_state, 'agent_access':worker.agent_controller.access_status(),
                             'paused':worker.paused or worker.interrupted.is_set(), 'human_control':worker.human_control}
                else:
                    state = worker.call('surface', {}, timeout=10)
                return {'ok':True, **state, 'preferences':workspace.preferences(root, profile),
                        'config':browser.load_profile(root, profile)}
            if operation in {'trace', 'trace_control'}:
                from ..integrations import browser_trace
                conversation_id, call_id = body.get('conversation_id', ''), body.get('call_id')
                if not isinstance(conversation_id, str) or len(conversation_id) > 256 or not isinstance(call_id, str) or not 1 <= len(call_id) <= 256:
                    raise browser.BrowserError('invalid_trace_identity')
                after, frame_id = body.get('after', 0), body.get('frame_id')
                if type(after) is not int or after < 0 or (frame_id is not None and (type(frame_id) is not int or frame_id < 1)):
                    raise browser.BrowserError('invalid_trace_cursor')
                state = browser_trace.read(root, conversation_id, call_id, after=after, frame_id=frame_id)
                if operation == 'trace':
                    return state  # Never enqueue observation behind a running click/wait.
                if not state.get('controllable'):
                    raise browser.BrowserError('browser_session_changed')
                worker = browser.worker_for(root, state['profile_id'])
                control = body.get('control')
                if control not in {'handoff', 'resume'} or worker is None:
                    raise browser.BrowserError('unsupported_trace_control')
                if control == 'handoff':
                    worker.interrupted.set()
                worker.call('trace_control', {'session_id':state['session_id'], 'control':control}, timeout=35)
                return browser_trace.read(root, conversation_id, call_id)
            if operation in {"agent_grant", "agent_revoke", "agent_upload"}:
                worker = browser.worker_for(root, profile)
                if worker is None:
                    raise browser.BrowserError("open_browser_first")
                return worker.call(operation, body, timeout=35)
            if operation == "review_extension":
                return {"ok": True, "review": browser.review_extension(body.get("path"))}
            if operation == "configure":
                with browser._LOCK:
                    if browser.worker_for(root, profile) is not None:
                        raise browser.BrowserError("close_browser_before_changing_extensions")
                    config = browser.save_profile(root, profile, body.get("extensions", []))
                return {"ok": True, "config": config}
            if operation == "open":
                url = browser.navigation_url(body.get("url", "about:blank"))
                worker = browser.open_browser(root, profile)
                if url != "about:blank":
                    worker.call("navigate", {"url": url})
            elif operation == "close":
                worker = browser.worker_for(root, profile)
                if worker is not None:
                    worker.close()
                    closed = worker.done.wait(timeout=5)
                    return {"ok": True, "running": not closed, "closing": not closed, "paused": not closed, "tabs": [], "agent_access": {"enabled": False, "occupied": False}}
                return {"ok": True, "running": False, "paused": False, "tabs": [], "agent_access": {"enabled": False, "occupied": False}}
            elif operation not in {"status", "command", "preview"}:
                raise browser.BrowserError("unsupported_browser_operation")
            worker = browser.worker_for(root, profile)
            result = {}
            if operation == "command":
                if worker is None:
                    raise browser.BrowserError("open_browser_first")
                params = body.get("payload", {})
                if not isinstance(params, dict) or not isinstance(body.get("command"), str):
                    raise browser.BrowserError("invalid_browser_command")
                result = worker.call(body["command"], params)
            state = worker.call("status") if worker is not None else {"running": False, "paused": False, "tabs": []}
            # Serialize screenshot and metadata as one job, not separate requests.
            if operation == "preview" and worker is not None:
                state = worker.call("preview", {}, timeout=35)
            if worker is None:
                state["agent_access"] = {"enabled": False, "occupied": False}
            if state.get("paused"):
                result.pop("image", None)
            return {"ok": True, **result, **state, "config": browser.load_profile(root, profile),
                    "capabilities": browser.capabilities()}
        except browser.BrowserError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            # Playwright exceptions can contain page text, arguments and paths.
            return {"ok": False, "error": "managed_browser_failed", "error_type": type(exc).__name__,
                    "hint": "Check the optional browser dependency and Chromium installation; no automatic retry. "
                            "Use the same Python environment: python -m pip install 'playwright>=1.58,<2' "
                            "then python -m playwright install chromium."}

    def status(client, query):
        body = {"profile_id": (query or {}).get("profile_id", "work"), "operation": "status"}
        return dispatch(client, body)

    def agent(client, payload):
        body = payload if isinstance(payload, dict) else {}
        request_id = body.get("request_id")
        try:
            profile = browser.identifier(body.get("profile_id", "work"))
            actor = body.get("_auth_actor_id")
            if not isinstance(actor, str) or not actor:
                raise browser.BrowserError("trusted_actor_required")
            root = client.config.paths.root
            from ..integrations.browser_workspace import preferences
            worker = browser.worker_for(root, profile)
            if body.get('operation') == 'open' and preferences(root, profile)['automatic']:
                worker = worker or browser.open_browser(root, profile)
                worker.call('agent_bootstrap', {}, timeout=35)
            if worker is None:
                raise browser.BrowserError("open_work_browser_first")
            params = {k: v for k, v in body.items() if not k.startswith("_auth_")}
            if (body.get('operation') == 'network' or
                    (body.get('operation') == 'network_detail' and worker.agent_controller and worker.agent_controller.in_action)):
                if worker.agent_controller is None:
                    raise browser.BrowserError('browser_session_owner_mismatch')
                return worker.agent_controller.network_read(params, actor)
            return worker.call("agent", {"body": params, "actor": actor}, timeout=35)
        except browser.BrowserError as exc:
            return {"ok": False, "error": str(exc), "request_id": request_id,
                    "retryable": False, "next_action": "check_session_or_request_operator_grant"}
        except Exception:
            return {"ok": False, "error": "agent_browser_unavailable", "request_id": request_id,
                    "retryable": False, "next_action": "inspect_state_before_retry"}

    return [("GET", "/browsers/desktop", status), ("POST", "/browsers/desktop", dispatch),
            ("POST", "/browsers/agent", agent)]
