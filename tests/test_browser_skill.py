from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from nerya.api import route_scopes
from nerya.api import routes_browsers_session as session_routes
from nerya.integrations import browser_engines
from nerya.skills.builtin.browser.scripts import browser_session
from nerya.skills.manifest import SkillManifest


pytestmark = pytest.mark.smoke


def _handler(path: str):
    for method, route_path, handler in session_routes.routes():
        if route_path == path:
            return method, handler
    raise AssertionError(f"missing route: {path}")


def test_browser_skill_manifest_and_route_scopes() -> None:
    manifest = SkillManifest.from_skill_md(
        Path(browser_session.__file__).parents[1] / "SKILL.md"
    )

    assert manifest.id == "browser"
    assert "console" in manifest.instructions
    assert "api_requests" in manifest.instructions
    assert route_scopes.required_scope("GET", "/browsers/status") == "read:runtime"
    assert route_scopes.required_scope("GET", "/browsers/session/list") == "read:runtime"
    assert route_scopes.required_scope("POST", "/browsers/session/cdp_action") == "write:tools"


def test_browser_engines_include_camofox_in_recommended_order(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        browser_engines,
        "_service_health",
        lambda *_args, **_kwargs: {
            "ok": False,
            "service_url": "http://127.0.0.1:9377",
            "error": "offline",
        },
    )

    specs = browser_engines.list_specs()
    status = browser_engines.status(tmp_path)

    assert [row["name"] for row in specs[:4]] == [
        "camofox",
        "cloakbrowser",
        "lightpanda",
        "obscura",
    ]
    assert [row["name"] for row in status["engines"][:4]] == [
        "camofox",
        "cloakbrowser",
        "lightpanda",
        "obscura",
    ]
    camofox = status["engines"][0]
    assert camofox["kind"] == "node_service"
    assert camofox["recommended_rank"] == 1
    assert camofox["service_url"] == "http://127.0.0.1:9377"


def test_browser_session_script_maps_agent_operations(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method, path, *, payload=None, **_kwargs):
        calls.append((method, path, payload))
        return {"ok": True, "path": path, "payload": payload}

    monkeypatch.setattr(browser_session, "_request", fake_request)

    browser_session.run(operation="open", url="https://example.com", interactive=True)
    browser_session.run(operation="click", session_id="bs_1", selector="#go")
    browser_session.run(operation="drag", session_id="bs_1", x=1, y=2, to_x=3, to_y=4)
    browser_session.run(operation="console", session_id="bs_1", limit=10)
    browser_session.run(operation="api_requests", session_id="bs_1", limit=5)
    browser_session.run(operation="api_fetch", session_id="bs_1", url="/api/me")

    assert calls[0][1] == "/browsers/session/cdp_open"
    assert calls[1][2] == {
        "session_id": "bs_1",
        "action": "click_selector",
        "payload": {"selector": "#go"},
    }
    assert calls[2][2]["action"] == "drag"
    assert calls[2][2]["payload"] == {"x": 1, "y": 2, "to_x": 3, "to_y": 4}
    assert calls[3][2]["action"] == "get_console"
    assert calls[4][2]["action"] == "get_api_requests"
    assert calls[5][2]["action"] == "api_fetch"


def test_browser_session_script_accepts_legacy_action_payloads(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method, path, *, payload=None, **_kwargs):
        calls.append((method, path, payload))
        return {"ok": True, "path": path, "payload": payload}

    monkeypatch.setattr(browser_session, "_request", fake_request)

    browser_session.run(
        operation="action",
        session_id="bs_1",
        action="click",
        selector="#go",
    )
    browser_session.run(
        operation="action",
        session_id="bs_1",
        action="eval",
        script="document.title",
    )
    browser_session.run(operation="action", session_id="bs_1", action="screenshot")
    browser_session.run(operation="action", session_id="bs_1", action="wait", seconds=2)

    assert calls[0][2] == {
        "session_id": "bs_1",
        "action": "click_selector",
        "payload": {"selector": "#go"},
    }
    assert calls[1][2] == {
        "session_id": "bs_1",
        "action": "eval",
        "payload": {"expression": "document.title", "script": "document.title"},
    }
    assert calls[2][1] == "/browsers/session/cdp_screenshot"
    assert calls[3][2] == {
        "session_id": "bs_1",
        "action": "wait",
        "payload": {"seconds": 2, "ms": 2000},
    }


def test_browser_session_script_uses_latest_session_when_missing(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method, path, *, payload=None, **_kwargs):
        calls.append((method, path, payload))
        if path == "/browsers/session/list":
            return {"ok": True, "sessions": [{"session_id": "bs_latest", "cdp": True}]}
        return {"ok": True, "path": path, "payload": payload}

    monkeypatch.setattr(browser_session, "_request", fake_request)

    browser_session.run(operation="snapshot")
    browser_session.run(operation="close")

    assert calls[0][1] == "/browsers/session/list"
    assert calls[1][2] == {
        "session_id": "bs_latest",
        "action": "snapshot",
        "payload": {},
    }
    assert calls[2][1] == "/browsers/session/list"
    assert calls[3][1] == "/browsers/session/cdp_close"
    assert calls[3][2] == {"session_id": "bs_latest"}


def test_browser_session_script_falls_back_when_selector_click_fails(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method, path, *, payload=None, **_kwargs):
        calls.append((method, path, payload))
        if payload and payload.get("action") == "click_selector":
            return {"ok": False, "error": "action_failed"}
        return {"ok": True, "path": path, "payload": payload}

    monkeypatch.setattr(browser_session, "_request", fake_request)

    result = browser_session.run(
        operation="click",
        session_id="bs_1",
        selector="text=C",
    )

    assert result["ok"] is True
    assert result["fallback_for"] == {"action": "click_selector", "selector": "text=C"}
    assert calls[0][2]["action"] == "click_selector"
    assert calls[1][2]["action"] == "eval"
    assert "clicked:true" in calls[1][2]["payload"]["expression"]


def test_browser_session_screenshot_omits_data_uri_by_default(monkeypatch) -> None:
    def fake_request(_method, _path, *, payload=None, **_kwargs):
        return {
            "ok": True,
            "path": "/tmp/shot.png",
            "bytes": 123,
            "data_uri": "data:image/png;base64," + ("a" * 200),
            "payload": payload,
        }

    monkeypatch.setattr(browser_session, "_request", fake_request)

    default_result = browser_session.run(operation="screenshot", session_id="bs_1")
    explicit_result = browser_session.run(
        operation="screenshot",
        session_id="bs_1",
        include_data_uri=True,
    )

    assert default_result["data_uri_omitted"] is True
    assert default_result["data_uri_length"] > 200
    assert "data_uri" not in default_result
    assert explicit_result["data_uri"].startswith("data:image/png;base64,")


def test_browser_session_cli_exits_nonzero_when_script_result_is_not_ok(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["browser_session.py", "--json", '{"operation":"status"}'],
    )
    monkeypatch.setattr(
        browser_session,
        "run",
        lambda **_kwargs: {"ok": False, "error": "boom"},
    )

    with pytest.raises(SystemExit) as exc:
        browser_session.main()

    assert exc.value.code == 1
    assert '"ok": false' in capsys.readouterr().out


def test_camofox_interactive_routes_use_service_backend(monkeypatch, tmp_path) -> None:
    _method, cdp_open = _handler("/browsers/session/cdp_open")
    _method, cdp_action = _handler("/browsers/session/cdp_action")
    _method, cdp_screenshot = _handler("/browsers/session/cdp_screenshot")
    _method, cdp_close = _handler("/browsers/session/cdp_close")
    calls: list[tuple[str, dict]] = []

    def fake_open(_root, *, session_id, url, **_kwargs):
        return {
            "ok": True,
            "name": "camofox",
            "service_url": "http://127.0.0.1:9377",
            "tab_id": "tab_1",
            "current_url": url,
            "runtime": {
                "service_url": "http://127.0.0.1:9377",
                "user_id": "nerya_test",
                "session_key": "task_test",
                "tab_id": "tab_1",
                "current_url": url,
            },
        }

    def fake_action(runtime, action, params, **_kwargs):
        calls.append((action, dict(params)))
        return {
            "ok": True,
            "action": action,
            "current_url": runtime.get("current_url"),
        }

    def fake_screenshot(_runtime, *, out_path, **_kwargs):
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return {
            "ok": True,
            "name": "camofox",
            "path": str(path),
            "bytes": path.stat().st_size,
            "fetch_method": "camofox_screenshot",
        }

    closed: list[dict] = []

    def fake_close(runtime, **_kwargs):
        closed.append(dict(runtime))
        return {"ok": True, "closed": True}

    monkeypatch.setattr(browser_engines, "camofox_open_tab", fake_open)
    monkeypatch.setattr(browser_engines, "camofox_action_runtime", fake_action)
    monkeypatch.setattr(browser_engines, "camofox_screenshot_runtime", fake_screenshot)
    monkeypatch.setattr(browser_engines, "camofox_close_runtime", fake_close)
    session_routes._RUNTIME.clear()
    session_routes._SESSIONS.clear()
    client = SimpleNamespace(config=SimpleNamespace(paths=SimpleNamespace(root=str(tmp_path))))

    opened = cdp_open(
        client,
        {
            "engine": "camofox",
            "session_id": "bs_camo",
            "url": "https://example.com",
        },
    )
    clicked = cdp_action(
        client,
        {
            "session_id": "bs_camo",
            "action": "click_selector",
            "payload": {"selector": "button.submit"},
        },
    )
    shot = cdp_screenshot(client, {"session_id": "bs_camo"})
    closed_result = cdp_close(client, {"session_id": "bs_camo"})

    assert opened["ok"] is True
    assert opened["engine"] == "camofox"
    assert clicked["ok"] is True
    assert calls == [("click_selector", {"selector": "button.submit"})]
    assert shot["ok"] is True
    assert shot["fetch_method"] == "camofox_screenshot"
    assert closed_result["ok"] is True
    assert closed and closed[0]["tab_id"] == "tab_1"


def test_camofox_drag_uses_openclaw_act_endpoint(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_http(method, url, *, payload=None, **_kwargs):
        calls.append((method, url, payload))
        return {"ok": True}

    monkeypatch.setattr(browser_engines, "_http_json", fake_http)

    result = browser_engines.camofox_action_runtime(
        {
            "service_url": "http://127.0.0.1:9377",
            "user_id": "nerya_test",
            "tab_id": "tab_1",
        },
        "drag",
        {"source_ref": "@e1", "target_ref": "@e2"},
    )

    assert result["ok"] is True
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:9377/act",
            {
                "userId": "nerya_test",
                "kind": "drag",
                "ref": "e1",
                "selector": None,
                "targetId": "e2",
            },
        )
    ]


class _FakeMouse:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def click(self, x, y) -> None:
        self.calls.append(("click", x, y))

    def wheel(self, dx, dy) -> None:
        self.calls.append(("wheel", dx, dy))

    def move(self, x, y, steps=None) -> None:
        self.calls.append(("move", x, y, steps))

    def down(self) -> None:
        self.calls.append(("down",))

    def up(self) -> None:
        self.calls.append(("up",))


class _FakeKeyboard:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def type(self, text, delay=0) -> None:
        self.calls.append(("type", text, delay))

    def press(self, key) -> None:
        self.calls.append(("press", key))


class _FakePage:
    url = "https://example.com/app"

    def __init__(self) -> None:
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()

    def title(self) -> str:
        return "Example App"

    def content(self) -> str:
        return "<html><body>Example App</body></html>"

    def evaluate(self, script, arg=None):
        if isinstance(script, str) and "fetch(url" in script:
            return {
                "ok": True,
                "status": 200,
                "url": "https://example.com/api/me?token=secret123",
                "text": "token=secret123",
            }
        if script == "document.body ? document.body.innerText : ''":
            return "Rendered text"
        return {"value": arg}

    def wait_for_selector(self, selector, timeout=0) -> None:
        self.last_wait = (selector, timeout)

    def wait_for_timeout(self, ms) -> None:
        self.last_timeout = ms


def test_cdp_action_supports_drag_snapshot_events_and_api_fetch() -> None:
    _method, cdp_action = _handler("/browsers/session/cdp_action")
    page = _FakePage()
    session_routes._RUNTIME.clear()
    session_routes._SESSIONS.clear()
    session_routes._RUNTIME["bs_test"] = {
        "engine": "cloakbrowser",
        "page": page,
        "lock": threading.RLock(),
        "events_lock": threading.RLock(),
        "console_events": [
            {
                "ts": "now",
                "kind": "console",
                "type": "error",
                "text": "failed with token=secret123",
            }
        ],
        "network_events": [
            {
                "ts": "now",
                "kind": "request",
                "method": "GET",
                "url": "https://example.com/api/me?token=[redacted]",
                "resource_type": "fetch",
            }
        ],
    }
    session_routes._SESSIONS["bs_test"] = {
        "session_id": "bs_test",
        "engine": "cloakbrowser",
        "created_at": "now",
        "updated_at": "now",
        "history": [],
        "current_url": page.url,
    }
    client = SimpleNamespace(config=SimpleNamespace(paths=SimpleNamespace(root=".")))

    drag = cdp_action(
        client,
        {
            "session_id": "bs_test",
            "action": "drag",
            "payload": {"x": 1, "y": 2, "to_x": 9, "to_y": 10, "steps": 3},
        },
    )
    snapshot = cdp_action(
        client,
        {"session_id": "bs_test", "action": "snapshot", "payload": {"max_chars": 100}},
    )
    console = cdp_action(
        client,
        {"session_id": "bs_test", "action": "get_console", "payload": {"limit": 5}},
    )
    api_requests = cdp_action(
        client,
        {"session_id": "bs_test", "action": "get_api_requests", "payload": {"limit": 5}},
    )
    api_fetch = cdp_action(
        client,
        {"session_id": "bs_test", "action": "api_fetch", "payload": {"url": "/api/me"}},
    )

    assert drag["ok"] is True
    assert ("down",) in page.mouse.calls
    assert drag["drag"]["to"] == {"x": 9.0, "y": 10.0}
    assert snapshot["snapshot"]["title"] == "Example App"
    assert snapshot["snapshot"]["text"] == "Rendered text"
    assert console["count"] == 1
    assert "token=secret123" in console["console"][0]["text"]
    assert api_requests["count"] == 1
    assert api_requests["events"][0]["url"].endswith("token=[redacted]")
    assert api_fetch["response"]["url"].endswith("token=%5Bredacted%5D")
    assert "secret123" not in api_fetch["response"]["text"]
