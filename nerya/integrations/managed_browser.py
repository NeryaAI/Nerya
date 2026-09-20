"""The dedicated, headed Chromium work browser shared by Agent and operator.

No personal browser discovery, credential import, signing or virtual WebAuthn
endpoints live here. Playwright communicates over its private pipe, not a public
CDP port. All handles and page commands belong to the worker's creating thread.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import math
import os
import queue
import re
import secrets
import tempfile
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit


class BrowserError(ValueError):
    """Fixed public error codes, without page content or request values."""


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", value):
        raise BrowserError("invalid_profile_id")
    return value


def navigation_url(value: Any) -> str:
    if value == "about:blank":
        return value
    if not isinstance(value, str) or len(value) > 8192 or any(c in value for c in "\x00\r\n"):
        raise BrowserError("invalid_url")
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError()
        _ = parts.port
    except ValueError:
        raise BrowserError("only_http_https_or_about_blank_allowed") from None
    return value


def public_url(value: str) -> str:
    try:
        p = urlsplit(value)
        return urlunsplit((p.scheme, p.netloc.rsplit("@", 1)[-1], p.path, "", ""))
    except ValueError:
        return ""


def profile_directory(root: Path | str, profile: str) -> Path:
    current = Path(root).resolve()
    for part in ("state", "browsers", "managed", identifier(profile)):
        current = current / part
        if current.is_symlink():
            raise BrowserError("profile_symlink_not_allowed")
        current.mkdir(mode=0o700, exist_ok=True)
        if os.name != "nt":
            current.chmod(0o700)
    return current


def load_profile(root: Path | str, profile: str) -> dict[str, Any]:
    path = profile_directory(root, profile) / "settings.json"
    if path.is_symlink():
        raise BrowserError("profile_symlink_not_allowed")
    if not path.exists():
        return {"id": profile, "version": 1, "extensions": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or data.get("id") != profile or not isinstance(data.get("extensions"), list):
            raise ValueError()
        return data
    except (ValueError, AttributeError, OSError):
        raise BrowserError("invalid_profile_settings") from None


def review_extension(value: Any) -> dict[str, Any]:
    """Inspect only a package explicitly chosen by the operator, never auto-scan."""
    if not isinstance(value, str):
        raise BrowserError("extension_directory_required")
    directory = Path(value).expanduser()
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise BrowserError("choose_absolute_unpacked_extension_directory")
    directory = directory.resolve()
    if "," in str(directory):
        raise BrowserError("extension_path_cannot_contain_comma")
    # Reject ordinary directories before enumerating any of their children.
    manifest = directory / "manifest.json"
    try:
        if manifest.is_symlink() or manifest.stat().st_size > 128 * 1024:
            raise ValueError()
        header = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(header, dict) or header.get("manifest_version") != 3:
            raise BrowserError("only_manifest_v3_supported")
    except BrowserError:
        raise
    except (OSError, ValueError):
        raise BrowserError("invalid_extension_manifest") from None
    digest = hashlib.sha256()
    total = 0
    files = []
    for path in directory.rglob("*"):
        files.append(path)
        if len(files) > 10000:
            raise BrowserError("extension_file_limit")
    files.sort()
    for path in files:
        if path.is_symlink():
            raise BrowserError("extension_symlink_not_allowed")
        if path.is_dir():
            continue
        if not path.is_file():
            raise BrowserError("extension_special_file_not_allowed")
        total += path.stat().st_size
        if total > 256 * 1024 * 1024:
            raise BrowserError("extension_size_limit")
        digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    manifest = directory / "manifest.json"
    try:
        if manifest.stat().st_size > 128 * 1024:
            raise ValueError()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data.get("manifest_version") != 3:
            raise BrowserError("only_manifest_v3_supported")
        if not all(isinstance(data.get(k), str) and data[k] for k in ("name", "version")):
            raise ValueError()
        permissions: list[str] = []
        for field in ("permissions", "host_permissions", "optional_permissions", "optional_host_permissions"):
            values = data.get(field, [])
            if not isinstance(values, list):
                raise ValueError()
            permissions.extend(values)
        for script in data.get("content_scripts", []):
            permissions.extend(script.get("matches", []))
        if not all(isinstance(p, str) for p in permissions):
            raise ValueError()
        extension_id = ""
        if data.get("key"):
            key_hash = hashlib.sha256(base64.b64decode(data["key"], validate=True)).hexdigest()[:32]
            extension_id = "".join(chr(ord("a") + int(c, 16)) for c in key_hash)
    except BrowserError:
        raise
    except (ValueError, OSError, AttributeError, TypeError):
        raise BrowserError("invalid_extension_manifest") from None
    return {"path": str(directory), "digest": digest.hexdigest(), "name": data["name"][:200],
            "version": data["version"][:80], "permissions": sorted(set(permissions)),
            "extension_id": extension_id, "control_ui": False}


def save_profile(root: Path | str, profile: str, extensions: Any) -> dict[str, Any]:
    if not isinstance(extensions, list) or len(extensions) > 12:
        raise BrowserError("extension_count_limit")
    reviewed = []
    for requested in extensions:
        if not isinstance(requested, dict):
            raise BrowserError("invalid_extension_review")
        entry = review_extension(requested.get("path"))
        if entry["digest"] != requested.get("digest"):
            raise BrowserError("extension_changed_review_again")
        if type(requested.get("control_ui", False)) is not bool:
            raise BrowserError("invalid_extension_control_flag")
        entry["control_ui"] = requested.get("control_ui", False)
        if type(requested.get("enabled", True)) is not bool:
            raise BrowserError("invalid_extension_enabled_flag")
        entry["enabled"] = requested.get("enabled", True)
        # A stable identity is required for an explicit UI-control grant.
        # Packages without a manifest key still load, but remain human-only.
        if entry["control_ui"] and not entry["extension_id"]:
            raise BrowserError("extension_ui_control_requires_stable_manifest_key")
        reviewed.append(entry)
    config = {"id": identifier(profile), "version": 1, "extensions": reviewed}
    directory = profile_directory(root, profile)
    fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "settings.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return config


def _playwright():
    from playwright.sync_api import sync_playwright
    return sync_playwright().start()


class ChromiumWorker:
    def __init__(self, root: Path | str, config: dict[str, Any], *, factory: Callable = _playwright) -> None:
        self.config = config
        self.root = Path(root).resolve()
        self.directory = profile_directory(root, config["id"])
        self.factory = factory
        self.jobs: queue.Queue = queue.Queue(maxsize=32)
        self.ready: Future = Future()
        self.stopping = threading.Event()
        self.done = threading.Event()
        self.paused = False
        self.human_control = False
        self.control_id = ''
        self._manual_dispatch = False
        self.surface_state: dict[str, Any] = {}
        self._history_pages: set = set()
        self.interrupted = threading.Event()
        self.agent_controller: Any = None
        self.network: Any = None
        self.context: Any = None
        self.cdp: Any = None
        self.page: Any = None
        self.pages: dict[str, Any] = {}
        self.thread = threading.Thread(target=self._run, name="nerya-managed-chromium", daemon=True)
        self.thread.start()

    def is_owner_thread(self) -> bool:
        return threading.current_thread() is self.thread

    def _run(self) -> None:
        driver = None
        try:
            paths = []
            for entry in self.config["extensions"]:
                if not entry.get("enabled", True):
                    continue
                current = review_extension(entry["path"])
                if current["digest"] != entry["digest"]:
                    raise BrowserError("extension_changed_review_again")
                paths.append(entry["path"])
            data_directory = self.directory / "chromium"
            if data_directory.is_symlink():
                raise BrowserError("profile_symlink_not_allowed")
            data_directory.mkdir(mode=0o700, exist_ok=True)
            driver = self.factory()
            args = ["--disable-extensions-except=" + ",".join(paths), "--load-extension=" + ",".join(paths)] if paths else []
            self.context = driver.chromium.launch_persistent_context(
                str(data_directory), channel="chromium", headless=False,
                args=args, chromium_sandbox=True, ignore_https_errors=False,
                viewport={"width": 1280, "height": 800}, timeout=30000,
            )
            self.context.set_default_timeout(10000)
            self.cdp = self.context.browser.new_browser_cdp_session()
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            from .browser_network import NetworkLog
            self._sync_pages()
            self.network = NetworkLog(self)
            self.ready.set_result(True)
            while not self.stopping.is_set():
                try:
                    future, command, payload = self.jobs.get(timeout=0.05)
                except queue.Empty:
                    # Sync Playwright needs its event loop pumped to receive popup events.
                    live = [p for p in self.context.pages if not p.is_closed()]
                    if live:
                        live[0].wait_for_timeout(25)
                    self.network.drain()
                    continue
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = self._command(command, payload)
                    self.network.drain()
                    future.set_result(result)
                except Exception as exc:
                    future.set_exception(exc)
        except Exception as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
        finally:
            self.stopping.set()
            if self.context is not None:
                try:
                    self.context.close()
                except Exception:
                    pass
            if driver is not None:
                try:
                    driver.stop()
                except Exception:
                    pass
            while True:
                try:
                    future, _, _ = self.jobs.get_nowait()
                except queue.Empty:
                    break
                if not future.done():
                    future.set_exception(BrowserError("browser_closed"))
            self.done.set()

    def wait_ready(self, timeout: float = 35) -> None:
        try:
            self.ready.result(timeout=timeout)
        except Exception:
            self.close()
            raise

    def call(self, command: str, payload: dict[str, Any] | None = None, *, timeout: float = 20) -> dict[str, Any]:
        if command == 'trace_control' and (payload or {}).get('control') == 'handoff':
            if self.agent_controller is None or self.agent_controller.session_id != (payload or {}).get('session_id'):
                raise BrowserError('browser_session_changed')
            self.interrupted.set()
        if command in {"handoff", "agent_revoke"}:
            self.interrupted.set()  # Visible to an executing batch before its next step.
        if self.stopping.is_set():
            raise BrowserError("browser_closing_or_closed")
        future: Future = Future()
        try:
            self.jobs.put_nowait((future, command, payload or {}))
        except queue.Full:
            raise BrowserError("browser_busy") from None
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            # Cancel queued work; never execute a timed-out click later. A running
            # command has an unknown outcome: close and require explicit recovery.
            if not future.cancel() and command not in {"status", "screenshot", "preview", "surface"}:
                self.close()
            raise BrowserError("operation_timed_out_do_not_retry_automatically") from None

    def close(self) -> None:
        self.interrupted.set()
        self.stopping.set()

    def _sync_pages(self) -> None:
        from .browser_workspace import record_visit
        for page in self.context.pages:
            if page not in self._history_pages:
                self._history_pages.add(page)
                page.on('framenavigated', lambda frame, p=page: record_visit(self.root, self.config['id'], frame.url) if frame is p.main_frame else None)
                record_visit(self.root, self.config['id'], page.url)
            if not page.is_closed() and page not in self.pages.values():
                self.pages[str(len(self.pages) + 1)] = page
        if self.page is None or self.page.is_closed():
            self.page = next((p for p in self.pages.values() if not p.is_closed()), None)

    def _protected_url(self, value: str) -> bool:
        if value == "about:blank":
            return False
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"}:
            return False
        if parsed.scheme != "chrome-extension":
            return True  # Never expose file://, browser settings or password UI.
        allowed = {e["extension_id"] for e in self.config["extensions"] if e.get("control_ui") and e.get("enabled", True)}
        return parsed.hostname not in allowed

    def _protected(self, page: Any) -> bool:
        return self._protected_url(page.url)

    def _has_protected_target(self) -> bool:
        if any(self._protected(p) for p in self.pages.values() if not p.is_closed()):
            self.protection_reason = 'protected_page'
            return True
        self._checking_targets = True
        try:
            # Do not allow frame callbacks to recursively send Target.getTargets.
            # Empty URLs are provisional targets, not committed sensitive documents.
            targets = self.cdp.send("Target.getTargets").get("targetInfos", [])
            protected = next((t for t in targets if t.get('type') in {'page','other','webview','iframe'}
                              and t.get('url') and self._protected_url(t['url'])), None)
            self.protection_reason = ('protected_target:' + str(protected.get('type')) + ':' + urlsplit(protected.get('url','')).scheme) if protected else ''
            return protected is not None
        except Exception as exc:
            self.protection_reason = 'inspection_failed:' + type(exc).__name__
            return True  # Failed target inspection is a human-only state.
        finally:
            self._checking_targets = False

    def _command(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self.is_owner_thread(), "Playwright thread affinity"
        self._sync_pages()
        if command == 'network_detail':
            if self._has_protected_target():
                raise BrowserError('protected_browser_ui')
            return self.network.detail(payload)
        if command == 'human_command':
            if not self.human_control or self._has_protected_target():
                raise BrowserError('take_over_before_manual_input')
            if not self.control_id or payload.get('control_id') != self.control_id:
                raise BrowserError('human_control_changed')
            expected = payload.get('session_id')
            actual = self.agent_controller.session_id if self.agent_controller else ''
            if expected is not None and expected != actual:
                raise BrowserError('browser_session_changed')
            operation = payload.get('command')
            if operation not in {'navigate','new_tab','select_tab','close_tab','back','forward','reload','click','type','press','scroll','extension_open'}:
                raise BrowserError('unsupported_manual_command')
            self._manual_dispatch = True
            try:
                return self._command(operation, payload.get('payload', {}))
            finally:
                self._manual_dispatch = False
        if command == 'surface':
            state = self._command('status', {})
            if not state['tabs'] or state.get('sensitive') or (state['paused'] and not self.human_control):
                return state
            if state['agent_access'].get('executing') and not self.human_control:
                return state  # In-flight actions use the independent trace stream.
            self._manual_dispatch = True
            try:
                image = self._command('screenshot', {}).get('image')
            finally:
                self._manual_dispatch = False
            state = self._command('status', {})
            return {**state, **({'image': image} if image and not state.get('sensitive') else {})}
        if command == "trace_control":
            controller = self.agent_controller
            if controller is None or controller.session_id != payload.get('session_id'):
                raise BrowserError('browser_session_changed')
            if payload.get('control') not in {'handoff', 'resume'}:
                raise BrowserError('unsupported_trace_control')
            return self._command(payload['control'], {})
        if command == "preview":
            # One worker job: another action cannot run between pixels and metadata.
            state = self._command("status", {})
            if state["paused"] or not state["tabs"]:
                return state
            try:
                image = self._command("screenshot", {}).get("image")
            except BrowserError:
                return self._command("status", {})
            state = self._command("status", {})
            return {**state, **({"image": image} if image and not state["paused"] else {})}
        if command in {"agent", "agent_grant", "agent_revoke", "agent_upload", "agent_bootstrap"}:
            if self.agent_controller is None:
                from .browser_agent import AgentController
                self.agent_controller = AgentController(self)
            controller = self.agent_controller
            if command == 'agent_bootstrap':
                from .browser_workspace import preferences
                if not preferences(self.root, self.config['id'])['automatic']:
                    raise BrowserError('automatic_browser_control_disabled')
                if self.paused or self.interrupted.is_set():
                    raise BrowserError('human_handoff_active')
                if not controller.access_status()['enabled']:
                    controller.grant_access({'all_web': True, 'screenshots': True, 'downloads': True, 'ttl_s': 86400})
                return self._command('status', {})
            if command == "agent_grant":
                return {"ok": True, "agent_access": controller.grant_access(payload)}
            if command == "agent_revoke":
                return {"ok": True, "agent_access": controller.revoke()}
            if command == "agent_upload":
                return {"ok": True, "upload": controller.stage_upload(payload)}
            return controller.run(payload["body"], payload["actor"])
        # Seeing a protected extension UI pauses the WHOLE profile, not just the
        # selected dapp. The operator must finish signing/unlocking natively.
        protected = self._has_protected_target()
        if protected:
            self.paused = True
        if command == "status":
            self.surface_state = {"ok": True, "profile_id": self.config["id"], "running": True,
                    "human_control": self.human_control, "control_id": self.control_id if self.human_control else '', "sensitive": protected,
                    "challenge": self.network.challenge_state(self.page) if self.network else {'state':'none'},
                    "paused": self.paused or self.interrupted.is_set(),
                    "agent_access": self.agent_controller.access_status() if self.agent_controller else {"enabled": False, "occupied": False},
                    "tabs": [{"id": k, "url": public_url(p.url), "protected": self._protected(p),
                              "selected": p is self.page} for k, p in self.pages.items() if not p.is_closed()]}
            return dict(self.surface_state)
        if command == "handoff":
            self.paused = True
            self.human_control = True
            self.control_id = secrets.token_urlsafe(18)
            if self.agent_controller:
                self.agent_controller.clear_refs()
            if self.page is not None:
                self.page.bring_to_front()
            return {"ok": True, "paused": True}
        if command == "resume":
            if protected:
                raise BrowserError("finish_and_close_protected_extension_ui_first")
            self.paused = False
            self.human_control = False
            self.control_id = ''
            self.interrupted.clear()
            if self.agent_controller:
                self.agent_controller.clear_refs()
            return {"ok": True, "paused": False}
        if (self.agent_controller and self.agent_controller.session_id
                and not self._manual_dispatch and not self.agent_controller.in_action and not self.paused
                and command not in {"status", "screenshot"}):
            raise BrowserError("agent_owns_browser_take_over_first")
        if command == "select_tab":
            page = self.pages.get(str(payload.get("tab_id")))
            if page is None or page.is_closed():
                raise BrowserError("tab_not_found")
            self.page = page
            page.bring_to_front()
            return {"ok": True}
        if self.paused and not self._manual_dispatch:
            raise BrowserError("human_handoff_active")
        if command == "new_tab":
            self.page = self.context.new_page()
            self.page.goto(navigation_url(payload.get("url", "about:blank")), wait_until="domcontentloaded")
        elif self.page is None:
            raise BrowserError("no_open_tab")
        elif command == "navigate":
            self.page.goto(navigation_url(payload.get("url")), wait_until="domcontentloaded")
        elif command == "back":
            self.page.go_back(wait_until="domcontentloaded")
        elif command == "forward":
            self.page.go_forward(wait_until="domcontentloaded")
        elif command == "reload":
            self.page.reload(wait_until="domcontentloaded")
        elif command == "close_tab":
            target = self.pages.get(str(payload.get('tab_id'))) if payload.get('tab_id') else self.page
            if target is None or target.is_closed():
                raise BrowserError('tab_not_found')
            target.close()
        elif command == "screenshot":
            # Only an in-memory viewport. Never add authenticated images to the
            # legacy screenshot journal or tool-result history.
            image = self.page.screenshot(full_page=False, animations="disabled")
            self._sync_pages()
            if self._has_protected_target():
                self.paused = True
                raise BrowserError("human_handoff_active")
            return {"ok": True, "image": "data:image/png;base64," + base64.b64encode(image).decode("ascii")}
        elif command == "click":
            x, y = payload.get("x"), payload.get("y")
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in (x, y)) or not (0 <= x <= 1280 and 0 <= y <= 800):
                raise BrowserError("invalid_viewport_coordinates")
            self.page.mouse.click(x, y)
        elif command == "type":
            text = payload.get("text")
            if not isinstance(text, str) or len(text) > 10000:
                raise BrowserError("invalid_text")
            self.page.keyboard.insert_text(text)
        elif command == "press":
            key = payload.get("key")
            if key not in {"Enter", "Tab", "Shift+Tab", "Escape", "Backspace", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"}:
                raise BrowserError("unsupported_key")
            self.page.keyboard.press(key)
        elif command == "scroll":
            dy = payload.get("dy", 500)
            if type(dy) not in (int, float) or not math.isfinite(dy) or abs(dy) > 3000:
                raise BrowserError("invalid_scroll")
            self.page.mouse.wheel(0, dy)
        elif command == "extension_open":
            extension_id = payload.get("extension_id")
            entry = next((e for e in self.config["extensions"] if e.get("extension_id") == extension_id and extension_id and e.get('enabled', True)), None)
            if entry is None:
                raise BrowserError("extension_id_unavailable_use_native_toolbar")
            manifest = json.loads((Path(entry["path"]) / "manifest.json").read_text(encoding="utf-8"))
            popup = (manifest.get("action") or {}).get("default_popup")
            if not isinstance(popup, str) or not popup or urlsplit(popup).scheme or popup.startswith(("/", "\\")) or ".." in popup.split("/"):
                raise BrowserError("extension_has_no_safe_default_popup")
            self.page = self.context.new_page()
            self.page.goto("chrome-extension://" + extension_id + "/" + popup)
            self.page.bring_to_front()
            if not entry.get("control_ui"):
                self.paused = True
        else:
            raise BrowserError("unsupported_browser_command")
        return {"ok": True}


_LOCK = threading.RLock()
_WORKERS: dict[tuple[str, str], ChromiumWorker] = {}


def worker_key(root: Path | str, profile: str) -> tuple[str, str]:
    return str(Path(root).resolve()), identifier(profile)


def worker_for(root: Path | str, profile: str) -> ChromiumWorker | None:
    with _LOCK:
        key = worker_key(root, profile)
        worker = _WORKERS.get(key)
        if worker and worker.done.is_set():
            _WORKERS.pop(key, None)
            return None
        return worker


def open_browser(root: Path | str, profile: str) -> ChromiumWorker:
    with _LOCK:
        existing = worker_for(root, profile)
        if existing is not None:
            if existing.stopping.is_set():
                raise BrowserError("browser_still_closing")
            worker = existing
        else:
            worker = ChromiumWorker(root, load_profile(root, profile))
            _WORKERS[worker_key(root, profile)] = worker
    worker.wait_ready()
    return worker


def capabilities() -> dict[str, Any]:
    return {"engine": "chromium", "playwright_installed": importlib.util.find_spec("playwright") is not None,
            "persistent_profile": True, "native_window": True, "unpacked_mv3_extensions": True,
            "chrome_web_store_install": False, "credential_import": False,
            "local_wallet_injection": False, "passkey_provider": "native_browser_or_os_requires_device_validation",
            "virtual_authenticator": False, "agent_tools_exposed": True, "agent_requires_site_grant": False, "automatic_mode": True}
