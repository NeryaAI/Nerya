"""Offline tests: synthetic pages and extension packages; no user credentials."""
from __future__ import annotations

import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from nerya.integrations import managed_browser as browser
from nerya.api import route_scopes, routes_browser_desktop, routes_browsers_session

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize("value", ["../personal", "a/b", "a\\b", "", "UPPER", "a" * 49, None])
def test_profile_id_rejects_traversal(value):
    with pytest.raises(browser.BrowserError):
        browser.identifier(value)


@pytest.mark.parametrize("url", ["file:///tmp/demo", "javascript:alert(1)", "data:text/plain,a", "chrome://settings", "https://user:pass@example.com", "https://example.com:bad", "https://example.com\n"])
def test_navigation_rejects_privileged_urls(url):
    with pytest.raises(browser.BrowserError):
        browser.navigation_url(url)


def test_profile_is_dedicated_and_symlinks_fail_closed(tmp_path):
    path = browser.profile_directory(tmp_path, "work")
    assert path == tmp_path / "state/browsers/managed/work"
    assert browser.worker_key(tmp_path, "work") != browser.worker_key(tmp_path / "other", "work")
    target = tmp_path / "elsewhere"
    target.mkdir()
    (path / "chromium").symlink_to(target, target_is_directory=True)
    worker = browser.ChromiumWorker(tmp_path, {"id": "work", "extensions": []}, factory=lambda: pytest.fail("must not launch"))
    with pytest.raises(browser.BrowserError, match="symlink"):
        worker.wait_ready()
    assert worker.done.wait(1)


@pytest.fixture
def extension(tmp_path):
    directory = tmp_path / "test-extension"
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps({
        "manifest_version": 3, "name": "Synthetic test extension", "version": "1.0",
        "key": base64.b64encode(b"public-test-fixture-only").decode(),
        "permissions": ["storage"], "content_scripts": [{"matches": ["https://example.test/*"], "js": ["content.js"]}],
        "action": {"default_popup": "popup.html"},
    }))
    (directory / "content.js").write_text("// synthetic fixture\n")
    (directory / "popup.html").write_text("<!doctype html><title>Test extension</title>")
    return directory


def test_extension_permissions_and_changed_code_require_review(tmp_path, extension):
    review = browser.review_extension(str(extension))
    assert "https://example.test/*" in review["permissions"]
    assert len(review["extension_id"]) == 32
    config = browser.save_profile(tmp_path, "work", [review])
    assert config == browser.load_profile(tmp_path, "work")
    assert config["extensions"][0]["control_ui"] is False
    (extension / "content.js").write_text("// changed code\n")
    with pytest.raises(browser.BrowserError, match="review_again"):
        browser.save_profile(tmp_path, "work", [review])


def test_extension_rejects_symlinks(extension, tmp_path):
    (extension / "link").symlink_to(tmp_path / "unrelated")
    with pytest.raises(browser.BrowserError, match="symlink"):
        browser.review_extension(str(extension))


def test_non_extension_directory_is_not_enumerated(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("ordinary directories must not be scanned")
    monkeypatch.setattr(type(tmp_path), "rglob", forbidden)
    with pytest.raises(browser.BrowserError, match="manifest"):
        browser.review_extension(str(tmp_path))


class FakePage:
    def __init__(self, driver, url="about:blank"):
        self.driver, self.url, self.closed = driver, url, False
        self.mouse = SimpleNamespace(click=self.click, wheel=lambda *args: driver.check())
        self.keyboard = SimpleNamespace(insert_text=lambda *args: driver.check(), press=lambda *args: driver.check())

    def on(self, event, callback):
        self.driver.check()

    def is_closed(self):
        return self.closed

    def goto(self, url, **kwargs):
        self.driver.check()
        if self.driver.block is not None:
            self.driver.entered.set()
            self.driver.block.wait(2)
        self.url = url

    def click(self, x, y):
        self.driver.check()
        self.driver.clicks += 1

    def screenshot(self, **kwargs):
        self.driver.check()
        return b"test-image-only"

    def wait_for_timeout(self, value):
        self.driver.check()
        time.sleep(0.001)

    def bring_to_front(self):
        self.driver.check()

    def close(self):
        self.driver.check()
        self.closed = True


class FakeDriver:
    def __init__(self):
        self.owner = threading.get_ident()
        self.thread_ids = set()
        self.clicks = 0
        self.stopped = False
        self.closed = False
        self.block = None
        self.entered = threading.Event()
        self.extra_targets = []
        self.page = FakePage(self)
        self.context = SimpleNamespace(
            pages=[self.page], new_page=self.new_page, on=lambda *args: self.check(),
            new_cdp_session=lambda page: SimpleNamespace(on=lambda *args: self.check(),
                send=lambda method, params=None: {'frameTree': {'frame': {'id':'main'}}} if method=='Page.getFrameTree' else {},
                detach=self.check),
            set_default_timeout=lambda _: self.check(), close=self.close,
            browser=SimpleNamespace(new_browser_cdp_session=lambda: SimpleNamespace(send=self.targets)),
        )
        self.chromium = SimpleNamespace(launch_persistent_context=self.launch)

    def check(self):
        assert threading.get_ident() == self.owner
        self.thread_ids.add(threading.get_ident())

    def launch(self, directory, **kwargs):
        self.check()
        assert kwargs["headless"] is False
        assert kwargs["ignore_https_errors"] is False
        assert kwargs["chromium_sandbox"] is True
        assert not any("remote-debugging-port" in a for a in kwargs["args"])
        return self.context

    def targets(self, method):
        self.check()
        assert method == "Target.getTargets"
        return {"targetInfos": self.extra_targets}

    def new_page(self):
        self.check()
        page = FakePage(self)
        self.context.pages.append(page)
        return page

    def close(self):
        self.check()
        self.closed = True

    def stop(self):
        self.check()
        self.stopped = True


@pytest.fixture
def running_worker(tmp_path):
    drivers = []
    def factory():
        driver = FakeDriver()
        drivers.append(driver)
        return driver
    worker = browser.ChromiumWorker(tmp_path, {"id": "work", "extensions": []}, factory=factory)
    worker.wait_ready()
    yield worker, drivers[0]
    worker.close()
    assert worker.done.wait(3)
    assert drivers[0].closed and drivers[0].stopped


def test_empty_provisional_target_does_not_pause_committed_web_page(running_worker):
    worker, driver = running_worker
    driver.extra_targets = [{'type':'page','url':''}]
    result = worker.call('status', {})
    assert not result['paused'] and not result['sensitive']


def test_all_requests_stay_on_owner_thread(running_worker):
    worker, driver = running_worker
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: worker.call("status"), range(30)))
    assert all(result["ok"] for result in results)
    assert driver.thread_ids == {driver.owner}
    assert not worker.is_owner_thread()


def test_native_toolbar_popup_pauses_entire_profile(running_worker):
    worker, driver = running_worker
    driver.extra_targets = [{"type": "other", "url": "chrome-extension://" + "a" * 32 + "/popup.html"}]
    assert worker.call("status")["paused"]
    for action in ("screenshot", "click", "navigate", "type"):
        with pytest.raises(browser.BrowserError, match="handoff"):
            worker.call(action, {})
    with pytest.raises(browser.BrowserError, match="protected"):
        worker.call("resume")
    driver.extra_targets = []  # Simulate the user finishing in the native UI.
    assert worker.call("resume")["paused"] is False


def test_internal_browser_pages_and_file_urls_are_protected(running_worker):
    worker, driver = running_worker
    driver.extra_targets = [{"type": "page", "url": "chrome://password-manager/passwords"}]
    assert worker.call("status")["paused"]


def test_queued_timeout_never_clicks_later(running_worker):
    worker, driver = running_worker
    driver.block = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(worker.call, "navigate", {"url": "https://example.test/"})
        assert driver.entered.wait(1)
        with pytest.raises(browser.BrowserError, match="do_not_retry"):
            worker.call("click", {"x": 5, "y": 5}, timeout=0.01)
        driver.block.set()
        assert first.result()["ok"]
    worker.call("status")  # Drain the cancelled click before checking.
    assert driver.clicks == 0


def test_late_startup_is_closed_after_timeout(tmp_path):
    gate = threading.Event()
    drivers = []
    def factory():
        gate.wait(2)
        driver = FakeDriver()
        drivers.append(driver)
        return driver
    worker = browser.ChromiumWorker(tmp_path, {"id": "work", "extensions": []}, factory=factory)
    with pytest.raises(TimeoutError):
        worker.wait_ready(timeout=0.01)
    gate.set()
    assert worker.done.wait(2)
    assert drivers[0].closed and drivers[0].stopped


def test_no_arbitrary_eval_or_identity_endpoints(running_worker):
    worker, _ = running_worker
    for command in ("eval", "import_credentials", "wallet_sign", "virtual_authenticator"):
        with pytest.raises(browser.BrowserError, match="unsupported"):
            worker.call(command)
    assert browser.capabilities()["credential_import"] is False
    assert browser.capabilities()["local_wallet_injection"] is False


def test_operator_scope_and_error_redaction(tmp_path, monkeypatch):
    for method in ("GET", "POST"):
        assert not route_scopes.authorize(["write:tools", "read:runtime"], method, "/browsers/desktop")[0]
        assert route_scopes.authorize(["admin:ops"], method, "/browsers/desktop")[0]
    def failure(*args):
        raise RuntimeError("DO_NOT_LEAK_PAGE_CONTENT")
    monkeypatch.setattr(browser, "open_browser", failure)
    handler = next(h for method, path, h in routes_browser_desktop.routes() if method == "POST")
    client = SimpleNamespace(config=SimpleNamespace(paths=SimpleNamespace(root=tmp_path)))
    result = handler(client, {"operation": "open"})
    assert not result["ok"]
    assert "DO_NOT_LEAK" not in json.dumps(result)


@pytest.mark.parametrize("endpoint", ["cdp_action", "cdp_screenshot"])
def test_legacy_shared_flag_cannot_bypass_worker(endpoint, monkeypatch):
    worker = SimpleNamespace(is_owner_thread=lambda: False, call=lambda fn, **kw: {"ok": True, "dispatched": True})
    monkeypatch.setitem(routes_browsers_session._RUNTIME, "test-thread", {
        "kind": "cloakbrowser_worker", "worker": worker, "_inside_worker": True,
    })
    assert not any(path.endswith('/' + endpoint) for _, path, _ in routes_browsers_session.routes())
