"""Operator-only browser observations, independent of the Chromium job queue.

Action metadata survives reload/restart; authenticated pixels remain in bounded
memory for 30 minutes. No cookie, input value, request body or page text is logged.
The producer is the native script executor, not model-authored call arguments.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
_TRACES: OrderedDict[str, 'Trace'] = OrderedDict()
_MAX_BYTES = 64 * 1024 * 1024
_TTL = 1800


def _key(conversation_id: str, call_id: str) -> str:
    return hashlib.sha256(json.dumps([conversation_id, call_id]).encode()).hexdigest()


def _directory(root: Path) -> Path:
    current = root.resolve()
    for name in ('state', 'browser_traces'):
        current = current / name
        if current.is_symlink():
            raise ValueError('trace_directory_symlink')
        current.mkdir(mode=0o700, exist_ok=True)
    return current


def _prune() -> None:
    now = time.time()
    for token, trace in list(_TRACES.items()):
        if now - trace.updated > _TTL:
            _TRACES.pop(token, None)
    while len(_TRACES) > 64:
        _TRACES.popitem(last=False)
    total = sum(t.frame_bytes for t in _TRACES.values())
    for trace in _TRACES.values():
        if total <= _MAX_BYTES:
            break
        total -= trace.frame_bytes
        trace.frames.clear()
        trace.frame_metadata.clear()
        trace.frame_bytes = 0


class Trace:
    def __init__(self, root: Path, conversation_id: str, call_id: str):
        self.root = root.resolve()
        self.conversation_id = conversation_id
        self.call_id = call_id
        self.token = secrets.token_urlsafe(24)
        self.events: list[dict[str, Any]] = []
        self.frames: OrderedDict[int, str] = OrderedDict()
        self.frame_metadata: dict[int, dict[str, Any]] = {}
        self.frame_bytes = 0
        self.frame_seq = 0
        self.updated = time.time()
        self.status = 'queued'
        self.profile_id = ''
        self.session_id = ''
        self.latest: dict[str, Any] = {}
        self.visual: dict[str, Any] = {}
        self.owner: Any = None
        self.storage_error = False

    def _save(self) -> None:
        try:
            directory = _directory(self.root)
            path = directory / (_key(self.conversation_id, self.call_id) + '.json')
            if path.is_symlink():
                raise ValueError('trace_file_symlink')
            temporary = directory / ('.' + self.token + '.tmp')
            with temporary.open('w', encoding='utf-8') as stream:
                json.dump(self.metadata(), stream, ensure_ascii=False)
            temporary.chmod(0o600)
            temporary.replace(path)
        except (OSError, ValueError):
            self.storage_error = True

    def metadata(self) -> dict[str, Any]:
        return {'call_id': self.call_id, 'conversation_id': self.conversation_id,
                'status': self.status, 'profile_id': self.profile_id,
                'session_id': self.session_id, 'events': list(self.events),
                'updated_at': self.updated, 'storage_error': self.storage_error}

    def emit(self, kind: str, **data: Any) -> int:
        with _LOCK:
            self.updated = time.time()
            seq = len(self.events) + 1
            self.events.append({'seq': seq, 'kind': kind, 'ts': self.updated, **data})
            self._save()
            return seq

    def frame(self, image: str, **metadata: Any) -> int:
        with _LOCK:
            if len(image) > 2 * 1024 * 1024:
                return 0
            self.frame_seq += 1
            fid = self.frame_seq
            self.frames[fid] = image
            self.frame_bytes += len(image)
            # Checkpoint frames have their own copy; continuous frames are latest-only.
            self.latest = {'frame_id': fid, 'ts': time.time(), **metadata}
            self.frame_metadata[fid] = dict(self.latest)
            pinned = {e.get('frame_id') for e in self.events}
            for old in list(self.frames):
                if old != fid and old not in pinned:
                    self.frame_bytes -= len(self.frames.pop(old))
                    self.frame_metadata.pop(old, None)
            _prune()
            return fid

    def finish(self, status: str) -> None:
        with _LOCK:
            if self.status in {'completed', 'failed', 'paused'}:
                return
            self.status = status
            self.emit('run_finished', status=status)


def create(root: Path, conversation_id: str, call_id: str) -> Trace:
    with _LOCK:
        trace = Trace(Path(root), str(conversation_id), str(call_id))
        _TRACES[trace.token] = trace
        _prune()
        trace.emit('run_queued')
        return trace


def resolve(root: Path, token: Any) -> Trace | None:
    with _LOCK:
        trace = _TRACES.get(token) if isinstance(token, str) else None
        return trace if trace and trace.root == Path(root).resolve() else None


def read(root: Path, conversation_id: str, call_id: str, *, after: int = 0,
         frame_id: int | None = None) -> dict[str, Any]:
    """No Playwright calls here: reads stay responsive during a blocking action."""
    root = Path(root).resolve()
    with _LOCK:
        _prune()
        trace = next((t for t in reversed(_TRACES.values()) if t.root == root
                      and t.conversation_id == conversation_id and t.call_id == call_id), None)
        if trace:
            data = trace.metadata()
            worker = trace.owner
            paused = bool(worker and (worker.paused or worker.interrupted.is_set()))
            current = bool(worker and not worker.done.is_set() and worker.agent_controller
                           and worker.agent_controller.session_id == trace.session_id)
            selected = frame_id if frame_id is not None else trace.latest.get('frame_id', 0)
            image = trace.frames.get(selected) if not paused else None
            data.update({'ok': True, 'paused': paused, 'controllable': current,
                         'visual': dict(trace.visual) if not paused else {},
                         'frame': {**trace.frame_metadata.get(selected, {}), 'frame_id': selected, 'image': image} if image else None,
                         'frame_state': 'paused' if paused else 'available' if image else 'unavailable',
                         'cursor': len(trace.events), 'events': [e for e in trace.events if e['seq'] > after]})
            return data
    path = _directory(root) / (_key(conversation_id, call_id) + '.json')
    if not path.is_symlink() and path.is_file() and path.stat().st_size < 1024 * 1024:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('conversation_id') == conversation_id and data.get('call_id') == call_id:
            if data.get('status') in {'queued', 'running'}:
                data['status'] = 'interrupted'
            return {**data, 'ok': True, 'controllable': False, 'frame': None, 'frame_state': 'expired',
                    'cursor': len(data['events']), 'events': [e for e in data['events'] if e['seq'] > after]}
    return {'ok': True, 'status': 'pending', 'events': [], 'cursor': 0, 'frame': None,
            'frame_state': 'unavailable', 'controllable': False}


class Recorder:
    """Owned and called exclusively by the Chromium thread; UI reads only Trace."""
    def __init__(self, controller: Any, trace: Trace, actor: str):
        self.controller, self.trace = controller, trace
        self.actor = actor
        self.permitted = False
        self.worker = controller.worker
        self.channel: Any = None
        self.page: Any = None
        self.last_frame = 0.0
        self.in_frame = False
        trace.profile_id = self.worker.config['id']
        trace.status = 'running'
        trace.emit('run_started')

    def permit(self) -> None:
        self.permitted = True
        self.trace.owner = self.worker
        self.trace.session_id = self.controller.session_id

    def safe(self) -> bool:
        if getattr(self.worker, '_checking_targets', False):
            return False  # Skip pixels until the outer protection check completes.
        previous = self.in_frame
        self.in_frame = True  # Do not recursively inspect targets from screencast callbacks.
        try:
            if not self.permitted or not self.controller.session_id or self.controller.actor != self.actor:
                return False
            self.controller.guard()
            page = self.worker.page
            return page is not None and not page.is_closed() and all(
                f.url in {'about:blank', 'about:srcdoc'} or self.controller.allowed(f.url) for f in page.frames)
        except Exception:
            return False
        finally:
            self.in_frame = previous

    def metadata(self) -> dict[str, Any]:
        from .managed_browser import public_url
        w = self.worker
        if not self.permitted or not self.controller.session_id or self.controller.actor != self.actor:
            return {}
        w._sync_pages()
        return {'visual': dict(self.trace.visual),
                'url': public_url(w.page.url) if w.page and self.controller.allowed(w.page.url) else '',
                'tabs': [{'id': key, 'url': public_url(p.url) if self.controller.allowed(p.url) else '[not authorized]',
                          'selected': p is w.page} for key, p in w.pages.items() if not p.is_closed()]}

    def visualize(self, action: str, *, elements: list | None = None, point: dict | None = None) -> None:
        """Read real viewport geometry. Visual hints never become click authority."""
        if not self.safe():
            return
        try:
            boxes = []
            for element in (elements or [])[:30]:
                box = element.bounding_box()
                if box and box['width'] > 0 and box['height'] > 0:
                    boxes.append(box)
            if point is None and boxes and action != 'dom':
                box = boxes[0]
                point = {'x':box['x'] + box['width']/2, 'y':box['y'] + box['height']/2}
            from .managed_browser import public_url
            if point is None and self.trace.visual.get('url') == public_url(self.worker.page.url):
                point = self.trace.visual.get('cursor')
            visual = {'action':action, 'boxes':boxes, 'cursor':point,
                      'viewport':{'width':1280,'height':800}, 'url':public_url(self.worker.page.url), 'ts':time.time()}
            with _LOCK:
                self.trace.visual = visual
            self.trace.emit('visual', **visual)
        except Exception:
            pass  # Geometry is observational, never a reason to replay an action.

    def follow(self) -> None:
        if self.page is self.worker.page:
            return
        self.stop()
        self.page = self.worker.page
        if self.page is None or self.page.is_closed() or not self.safe():
            return
        try:
            channel = self.worker.context.new_cdp_session(self.page)
            self.channel = channel
            channel.on('Page.screencastFrame', lambda event: self.on_frame(channel, event))
            channel.send('Page.startScreencast', {'format': 'jpeg', 'quality': 55,
                         'maxWidth': 1280, 'maxHeight': 800, 'everyNthFrame': 1})
        except Exception:
            self.trace.emit('preview_mode', mode='step_checkpoints')

    def on_frame(self, channel: Any, event: dict) -> None:
        try:
            if self.channel is channel and not self.in_frame and time.monotonic() - self.last_frame >= .15:
                self.in_frame = True
                try:
                    if self.page is self.worker.page and self.safe():
                        self.trace.frame('data:image/jpeg;base64,' + event['data'], **self.metadata())
                        self.last_frame = time.monotonic()
                finally:
                    self.in_frame = False
        except Exception:
            pass  # Preview transport failure never changes browser action results.
        finally:
            try:
                channel.send('Page.screencastFrameAck', {'sessionId': event['sessionId']})
            except Exception:
                pass

    def checkpoint(self) -> int:
        if not self.safe():
            return 0
        self.in_frame = True
        try:
            page = self.worker.page
            image = page.screenshot(type='jpeg', quality=60, timeout=1500)
            if page is self.worker.page and self.safe():
                return self.trace.frame('data:image/jpeg;base64,' + base64.b64encode(image).decode(), **self.metadata())
        except Exception:
            pass
        finally:
            self.in_frame = False
        return 0

    def step(self, index: int, step: dict, phase: str, error: str = '') -> None:
        try:
            self._step(index, step, phase, error)
        except Exception:
            self.trace.emit('step', index=index, action=str(step.get('action', ''))[:40],
                            phase=phase, error=error[:100], preview_unavailable=True)

    def _step(self, index: int, step: dict, phase: str, error: str = '') -> None:
        from .browser_agent import clean
        self.follow()
        if phase == 'started':
            self.visualize(str(step.get('action', '')))
        target = step.get('target') or {}
        target = target if isinstance(target, dict) else {}
        description = ' '.join(str(target.get(k, '')) for k in ('role', 'name', 'label', 'text', 'test_id', 'ref')).strip()
        # Input values and dialog text are never copied to the operator trace.
        data = {'index': index, 'action': str(step.get('action', ''))[:40],
                'target': clean(description, 180), 'phase': phase, 'error': error[:100], **self.metadata()}
        if phase != 'started':
            data['frame_id'] = self.checkpoint()
        self.trace.emit('step', **data)

    def stop(self) -> None:
        channel, self.channel = self.channel, None
        if channel:
            try:
                channel.send('Page.stopScreencast')
                channel.detach()
            except Exception:
                pass

    def finish(self, result: dict) -> None:
        try:
            frame_id = self.checkpoint()
            self.trace.emit('observation', frame_id=frame_id, **self.metadata())
        except Exception:
            self.trace.emit('preview_unavailable')
        finally:
            self.trace.finish('paused' if self.worker.paused or self.worker.interrupted.is_set()
                              else 'completed' if result.get('ok') else 'failed')
            self.stop()
