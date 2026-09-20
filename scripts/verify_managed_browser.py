"""Opt-in real Chromium smoke test. Uses only a temporary synthetic profile.

Run with the project environment after installing its browser extra and binary:
    .venv/bin/python scripts/verify_managed_browser.py
No personal browser, wallet, account, external website or passkey is accessed.
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nerya.integrations.managed_browser import BrowserError, ChromiumWorker, review_extension


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        html = b"""<!doctype html><html><head><title>Nerya isolated browser test</title></head>
<body><h1>Nerya isolated browser test</h1><input aria-label="Test input">
<script>
const previous = localStorage.getItem('nerya-synthetic-marker');
localStorage.setItem('nerya-synthetic-marker', 'present');
history.replaceState(null, '', previous ? '/persisted' : '/first-visit');
</script></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, *args):
        pass


class TestWorker(ChromiumWorker):
    """Test-only inspection, never registered on the production command API."""
    def _command(self, command, payload):
        if command == "test_extension_id":
            assert self.is_owner_thread()
            self.page.wait_for_timeout(150)
            workers = self.context.service_workers
            return {"id": next((w.url.split('/')[2] for w in workers if w.url.startswith('chrome-extension://')), '')}
        if command == "test_popup":
            assert self.is_owner_thread()
            self.page = self.context.new_page()
            self.page.goto("chrome-extension://" + payload["id"] + "/popup.html")
            return {"ok": True}
        if command == "test_popup_result":
            assert self.is_owner_thread()
            return {"text": self.page.locator("button").inner_text()}
        if command == "test_allow_fixture_ui":
            assert self.is_owner_thread()
            entry = self.config["extensions"][0]
            entry["extension_id"] = payload["id"]
            entry["control_ui"] = payload["allowed"]
            return {"ok": True}
        return super()._command(command, payload)


def main():
    report = {"ok": False, "checks": []}
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    worker = None
    with tempfile.TemporaryDirectory(prefix="nerya-managed-browser-test-") as temporary:
        root = Path(temporary)
        package = root / "synthetic-extension"
        package.mkdir()
        (package / "manifest.json").write_text(json.dumps({
            "manifest_version": 3, "name": "Nerya synthetic UI test", "version": "1.0",
            "background": {"service_worker": "worker.js"},
            "action": {"default_popup": "popup.html"},
        }), encoding="utf-8")
        (package / "worker.js").write_text("chrome.runtime.onInstalled.addListener(() => {});", encoding="utf-8")
        (package / "popup.html").write_text('<!doctype html><html><body style="margin:0"><button style="width:200px;height:100px">Ready</button><script src="popup.js"></script></body></html>', encoding="utf-8")
        (package / "popup.js").write_text('document.querySelector("button").addEventListener("click", e => { e.target.textContent = "Clicked"; });', encoding="utf-8")
        config = {"id": "integration-test", "extensions": [review_extension(str(package))]}
        origin = "http://127.0.0.1:" + str(server.server_address[1])
        try:
            worker = TestWorker(root, config)
            worker.wait_ready()
            worker.call("navigate", {"url": origin})
            status = worker.call("status")
            assert not status["paused"], status
            assert any(t["url"].endswith("/first-visit") for t in status["tabs"]), status
            report["checks"].append("headed_chromium_native_window_and_navigation")
            image = worker.call("screenshot")["image"]
            assert base64.b64decode(image.split(",", 1)[1]).startswith(b"\x89PNG\r\n\x1a\n")
            report["checks"].append("in_memory_png_preview")
            worker.call("new_tab", {"url": origin})
            assert len(worker.call("status")["tabs"]) >= 2
            report["checks"].append("multiple_tabs")
            extension_id = ""
            for _ in range(30):
                extension_id = worker.call("test_extension_id")["id"]
                if extension_id:
                    break
                time.sleep(0.1)
            assert extension_id, "synthetic MV3 service worker did not start"
            report["checks"].append("real_manifest_v3_extension_loaded")
            worker.call("test_popup", {"id": extension_id})
            assert worker.call("status")["paused"] is True
            try:
                worker.call("screenshot")
                raise AssertionError("protected extension was captured")
            except BrowserError as exc:
                assert "handoff" in str(exc)
            report["checks"].append("protected_extension_pauses_profile_and_preview")
            worker.call("test_allow_fixture_ui", {"id": extension_id, "allowed": True})
            worker.call("resume")
            worker.call("click", {"x": 50, "y": 40})
            assert worker.call("test_popup_result")["text"] == "Clicked"
            report["checks"].append("ordinary_extension_page_ui_click")
            worker.call("close_tab")
            worker.call("test_allow_fixture_ui", {"id": extension_id, "allowed": False})
            worker.close()
            assert worker.done.wait(10), "browser did not close"
            worker = TestWorker(root, config)
            worker.wait_ready()
            worker.call("navigate", {"url": origin})
            status = worker.call("status")
            assert any(t["url"].endswith("/persisted") for t in status["tabs"]), status
            report["checks"].append("synthetic_local_storage_survives_browser_restart")
            worker.close()
            assert worker.done.wait(10)
            report["checks"].append("context_and_driver_close_cleanly")
            report["ok"] = True
        except Exception as exc:
            report["error_type"] = type(exc).__name__
            # All content in this script is synthetic; still avoid dumping browser logs.
            report["error"] = str(exc)[:1000]
        finally:
            if worker is not None:
                worker.close()
                worker.done.wait(10)
            server.shutdown()
            server.server_close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
