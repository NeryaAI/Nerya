"""Small persistent preferences and navigation history for the work browser."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .managed_browser import BrowserError, navigation_url, profile_directory, public_url

_LOCK = threading.RLock()


def _read(root: Path, profile: str, name: str, default: Any) -> Any:
    path = profile_directory(root, profile) / name
    if path.is_symlink():
        raise BrowserError('browser_state_symlink')
    if not path.exists():
        return default
    if path.stat().st_size > 1024 * 1024:
        raise BrowserError('browser_state_too_large')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        raise BrowserError('invalid_browser_state') from None


def _write(root: Path, profile: str, name: str, data: Any) -> None:
    directory = profile_directory(root, profile)
    target = directory / name
    if target.is_symlink():
        raise BrowserError('browser_state_symlink')
    fd, temporary = tempfile.mkstemp(prefix='.browser-state-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def preferences(root: Path, profile: str = 'work') -> dict:
    with _LOCK:
        data = _read(root, profile, 'preferences.json', {'automatic': True})
        if not isinstance(data, dict) or type(data.get('automatic')) is not bool:
            raise BrowserError('invalid_browser_preferences')
        return {'automatic': data['automatic']}


def set_preferences(root: Path, profile: str, automatic: Any) -> dict:
    if type(automatic) is not bool:
        raise BrowserError('automatic_must_be_boolean')
    with _LOCK:
        data = {'automatic': automatic}
        _write(root, profile, 'preferences.json', data)
        return data


def history(root: Path, profile: str = 'work') -> list[dict]:
    with _LOCK:
        rows = _read(root, profile, 'history.json', [])
        if not isinstance(rows, list):
            raise BrowserError('invalid_browser_history')
        return rows[-300:]


def record_visit(root: Path, profile: str, url: str) -> None:
    """Only document addresses, never browser settings or authentication query data."""
    try:
        address = public_url(navigation_url(url))
        if address == 'about:blank':
            return
        with _LOCK:
            rows = history(root, profile)
            if rows and rows[-1]['url'] == address:
                return
            rows.append({'id': secrets.token_hex(8), 'url': address, 'at': time.time()})
            _write(root, profile, 'history.json', rows[-300:])
    except (BrowserError, OSError):
        pass  # History failure must not change a navigation's outcome.


def clear_history(root: Path, profile: str) -> None:
    with _LOCK:
        _write(root, profile, 'history.json', [])
