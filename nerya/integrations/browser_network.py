"""Bounded passive DevTools Network observation for the work browser.

CDP handles stay on the browser thread. Metadata reads copy the ring under a
lock and never queue behind a click. No interception, request replay or secret
export. Eligible completed bodies are sanitized into a bounded RAM cache;
only explicitly requested body slices are returned to the Agent.
"""
from __future__ import annotations

import base64
import copy
import json
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .managed_browser import BrowserError
from .browser_observation import bounded_int

_SECRET = re.compile(r'authorization|cookie|token|secret|password|passwd|session|api.?key|credential|signature|private.?key|mnemonic|seed|csrf|otp|code|cc.number|cc.csc', re.I)
_HEADERS = {'accept', 'accept-language', 'content-type', 'content-length', 'cache-control',
            'content-encoding', 'date', 'server', 'cf-mitigated', 'cf-ray', 'retry-after',
            'access-control-allow-origin', 'vary', 'location'}
_SUMMARY = ('id', 'seq', 'tab_id', 'url', 'method', 'type', 'status', 'state', 'duration_ms',
            'bytes', 'cached', 'service_worker', 'challenge', 'redirect_from', 'error', 'at')


def scrub_text(value: str, limit: int = 65536) -> str:
    value = re.sub(r'(?i)bearer\s+[\w.~/+=-]+', 'Bearer [redacted]', str(value))
    value = re.sub(r'(?i)((?:token|password|secret|api[_-]?key|authorization|cookie)\s*[=:]\s*)[^\s,;<>]+', r'\1[redacted]', value)
    return value[:limit]


def safe_url(value: str) -> str:
    try:
        u = urlsplit(value)
        if u.scheme not in {'http', 'https', 'ws', 'wss'} or not u.hostname:
            return '[non-web address]'
        # Query key names aid debugging; values may contain opaque auth data.
        query = urlencode([(k[:80], '[redacted]') for k, _ in parse_qsl(u.query, keep_blank_values=True)[:40]])
        host = u.hostname
        if ':' in host:
            host = '[' + host + ']'
        if u.port:
            host += ':' + str(u.port)
        return scrub_text(urlunsplit((u.scheme, host, u.path, query, '')), 1500)
    except (ValueError, TypeError):
        return '[invalid address]'


def safe_headers(headers: dict) -> dict:
    return {str(k).lower(): (safe_url(str(v)) if str(k).lower() == 'location' else scrub_text(v, 512))
            for k, v in list(headers.items())[:100]
            if str(k).lower() in _HEADERS and not _SECRET.search(str(k))}


def safe_body(text: str, mime: str) -> str:
    if 'json' in mime:
        try:
            def walk(value, depth=0):
                if depth > 20:
                    return '[depth limit]'
                if isinstance(value, dict):
                    return {str(k): '[redacted]' if _SECRET.search(str(k)) else walk(v, depth+1)
                            for k, v in list(value.items())[:200]}
                if isinstance(value, list):
                    return [walk(v, depth+1) for v in value[:200]]
                return scrub_text(value, 8000) if isinstance(value, str) else value
            return json.dumps(walk(json.loads(text)), ensure_ascii=False, separators=(',', ':'))[:65536]
        except (ValueError, RecursionError):
            return '[JSON unavailable or exceeds parser limits]'
    if 'x-www-form-urlencoded' in mime:
        return urlencode([(k, '[redacted]' if _SECRET.search(k) else scrub_text(v, 1000))
                          for k, v in parse_qsl(text, keep_blank_values=True)[:100]])
    return scrub_text(text)


class NetworkLog:
    MAX_ROWS = 500
    BODY_LIMIT = 65536
    MAX_TABS = 16
    MAX_BODY_BYTES = 4*1024*1024

    def __init__(self, worker):
        self.worker = worker
        self.lock = threading.RLock()
        self.rows = OrderedDict()
        self.bodies = OrderedDict()
        self.body_bytes = 0
        self.pending_bodies = deque(maxlen=self.MAX_ROWS)
        self._draining = False
        self.keys = {}
        self.channels = {}
        self.pages = {}
        self.challenges = {}
        self.main_frames = {}
        self.seq = 0
        self.generation = secrets.token_hex(8)
        self.counter = 0
        self.dropped = 0
        self.attach_errors = 0
        worker.context.on('page', self.attach)
        for page in worker.context.pages:
            self.attach(page)

    def attach(self, page):
        if page in self.pages:
            return
        if len(self.channels) >= self.MAX_TABS:
            self.attach_errors += 1
            return
        try:
            channel = self.worker.context.new_cdp_session(page)
            self.counter += 1
            source = str(self.counter)
            self.channels[source] = channel
            self.pages[page] = source
            channel.on('Network.requestWillBeSent', lambda e: self.request(source, page, e))
            channel.on('Network.responseReceived', lambda e: self.response(source, page, e))
            channel.on('Network.loadingFinished', lambda e: self.finish(source, e))
            channel.on('Network.loadingFailed', lambda e: self.finish(source, e, failed=True))
            channel.on('Network.requestServedFromCache', lambda e: self.update(source, e, cached=True))
            page.on('close', lambda: self.detach(page))
            self.main_frames[source] = channel.send('Page.getFrameTree')['frameTree']['frame']['id']
            channel.send('Network.enable', {'maxTotalBufferSize': 2*1024*1024,
                         'maxResourceBufferSize': self.BODY_LIMIT, 'maxPostDataSize': 8192})
        except Exception:
            self.attach_errors += 1
            if page in self.pages:
                self.detach(page)

    def detach(self, page):
        source = self.pages.pop(page, None)
        channel = self.channels.pop(source, None)
        self.challenges.pop(source, None)
        self.main_frames.pop(source, None)
        if channel:
            try:
                channel.detach()
            except Exception:
                pass

    def _owner(self):
        c = self.worker.agent_controller
        if c and c.session_id and not self.worker.paused and not self.worker.interrupted.is_set():
            return c.session_id
        return ''

    def _touch(self, row):
        self.seq += 1
        row['seq'] = self.seq

    def request(self, source, page, event):
        try:
            req = event['request']
            if urlsplit(req['url']).scheme not in {'http', 'https'}:
                return
            if self.worker._protected_url(page.url):
                return
            key = (source, event['requestId'])
            with self.lock:
                prior = self.rows.get(self.keys.get(key))
                if prior and event.get('redirectResponse'):
                    prior.update(status=int(event['redirectResponse']['status']), state='redirected')
                    self._touch(prior)
                self.counter += 1
                identifier = 'net_' + self.generation + '_' + str(self.counter)
                headers = safe_headers(req.get('headers', {}))
                row = {'id':identifier, 'tab_id': next((k for k,p in self.worker.pages.items() if p is page), ''),
                       'url':safe_url(req['url']), 'method':req['method'], 'type':event.get('type', 'Other').lower(),
                       'state':'pending', 'status':None, 'at':event.get('wallTime', time.time()),
                       'duration_ms':0, 'bytes':0, 'request_headers':headers,
                       'initiator':str(event.get('initiator', {}).get('type', 'other'))[:40],
                       '_source':source, '_request':event['requestId'], '_start':event.get('timestamp', 0),
                       '_session':self._owner(), '_origin':safe_url(event.get('documentURL', page.url))}
                if prior and event.get('redirectResponse'):
                    row['redirect_from'] = prior['id']
                post = req.get('postData')
                mime = headers.get('content-type', '')
                if isinstance(post, str):
                    row['request_body'] = safe_body(post, mime) if len(post.encode()) <= 8192 and ('json' in mime or 'x-www-form-urlencoded' in mime) else '[payload omitted]'
                self._touch(row)
                self.rows[identifier] = row
                self.keys[key] = identifier
                while len(self.rows) > self.MAX_ROWS:
                    old_id, old = self.rows.popitem(last=False)
                    old_key = (old['_source'], old['_request'])
                    if self.keys.get(old_key) == old_id:
                        self.keys.pop(old_key, None)
                    self.dropped += 1
                    old_body = self.bodies.pop(old_id, None)
                    if old_body is not None:
                        self.body_bytes -= len(old_body.encode())
        except Exception:
            pass  # An observer must not fail or replay a browser action.

    def update(self, source, event, **values):
        with self.lock:
            row = self.rows.get(self.keys.get((source, event.get('requestId'))))
            if row:
                row.update(values)
                self._touch(row)
            return row

    def response(self, source, page, event):
        data = event.get('response', {})
        headers = safe_headers(data.get('headers', {}))
        challenged = headers.get('cf-mitigated', '').lower() == 'challenge'
        self.update(source, event, status=int(data.get('status', 0)), state='receiving',
                    response_headers=headers, mime_type=data.get('mimeType', ''),
                    cached=bool(data.get('fromDiskCache')), service_worker=bool(data.get('fromServiceWorker')),
                    challenge=challenged)
        # A challenged XHR is not a top-level browser challenge.
        if event.get('type') == 'Document' and event.get('frameId') == self.main_frames.get(source):
            with self.lock:
                previous = self.challenges.get(source, {})
                if challenged:
                    self.challenges[source] = {'state':'waiting', 'provider':'cloudflare',
                        'url':safe_url(data.get('url', '')), 'since':time.time(), 'evidence':'cf-mitigated'}
                elif previous.get('state') in {'waiting', 'handoff_required'} and 200 <= data.get('status',0) < 400:
                    self.challenges[source] = {**previous, 'state':'cleared', 'cleared_at':time.time()}

    def finish(self, source, event, failed=False):
        with self.lock:
            row = self.rows.get(self.keys.get((source, event.get('requestId'))))
            if not row:
                return
            row.update(state='failed' if failed else 'finished',
                       duration_ms=round(max(0, event.get('timestamp',row['_start'])-row['_start'])*1000, 1),
                       bytes=int(event.get('encodedDataLength', 0)))
            if failed:
                row['error'] = scrub_text(event.get('errorText', 'network_failed'), 160)
            self._touch(row)
            if not failed:
                self.pending_bodies.append(row['id'])

    def _body(self, row, allow_cdp=True):
        with self.lock:
            text = self.bodies.get(row['id'])
        if text is not None:
            return 'available', text
        mime = row.get('mime_type', '').lower()
        if row['state'] != 'finished':
            return 'not_finished_or_redirected', None
        if not (mime.startswith('text/') or 'json' in mime or 'xml' in mime):
            return 'non_text_body_omitted', None
        if row.get('bytes', 0) > self.BODY_LIMIT:
            return 'body_exceeds_capture_limit', None
        if not allow_cdp:
            return 'capture_pending_or_evicted', None
        try:
            data = self.channels[row['_source']].send('Network.getResponseBody', {'requestId':row['_request']})
            raw = data['body']
            if data.get('base64Encoded'):
                raw = base64.b64decode(raw).decode('utf-8', errors='replace')
            if len(raw.encode()) > self.BODY_LIMIT:
                return 'body_exceeds_capture_limit', None
            text = safe_body(raw, mime)
            with self.lock:
                if row['id'] in self.rows and row['id'] not in self.bodies:
                    self.bodies[row['id']] = text
                    self.body_bytes += len(text.encode())
                    while self.body_bytes > self.MAX_BODY_BYTES and self.bodies:
                        _, removed = self.bodies.popitem(last=False)
                        self.body_bytes -= len(removed.encode())
            return 'available', text
        except Exception:
            return 'buffer_evicted_or_target_closed', None

    def drain(self, limit=4):
        # Called only by the owning thread, outside the CDP event callback.
        if self._draining:
            return
        self._draining = True
        try:
            for _ in range(limit):
                if not self.pending_bodies:
                    break
                identifier = self.pending_bodies.popleft()
                with self.lock:
                    row = copy.deepcopy(self.rows.get(identifier))
                if row:
                    self._body(row)
        finally:
            self._draining = False

    def challenge_state(self, page):
        with self.lock:
            return dict(self.challenges.get(self.pages.get(page), {'state':'none'}))

    def mark_handoff(self, page):
        with self.lock:
            state = self.challenges.get(self.pages.get(page))
            if state:
                state['state'] = 'handoff_required'

    def _visible(self, row, controller):
        if controller is None:
            return True
        return row['_session'] == controller.session_id and controller.allowed(row['url']) and controller.allowed(row['_origin'])

    def read(self, body, controller=None):
        after = bounded_int(body, 'after', 0, 0, 2**53)
        limit = bounded_int(body, 'limit', 25, 1, 100)
        generation = body.get('generation')
        if generation is not None and (not isinstance(generation, str) or len(generation)>100):
            raise BrowserError('invalid_network_generation')
        reset = (generation is not None and generation != self.generation) or after > self.seq
        if reset:
            after = 0
        for key in ('resource_type','method','tab_id'):
            if key in body and (not isinstance(body[key], str) or len(body[key])>100):
                raise BrowserError('invalid_network_' + key)
        if 'errors_only' in body and type(body['errors_only']) is not bool:
            raise BrowserError('invalid_network_errors_only')
        query = body.get('filter', '')
        if not isinstance(query, str) or len(query)>500:
            raise BrowserError('invalid_network_filter')
        with self.lock:
            matches = sorted((r for r in self.rows.values() if r['seq']>after and self._visible(r,controller)
                and query.lower() in r['url'].lower()
                and (not body.get('resource_type') or r['type']==body['resource_type'])
                and (not body.get('method') or r['method']==body['method'])
                and (not body.get('tab_id') or r['tab_id']==body['tab_id'])
                and (not body.get('errors_only') or r['state']=='failed' or (r.get('status') or 0)>=400)), key=lambda r:r['seq'])
            selected, rows, used = [], [], 0
            for row in matches[:limit]:
                summary = {k:row[k] for k in _SUMMARY if k in row}
                size = len(json.dumps(summary, ensure_ascii=False))
                if rows and used + size > 12000:
                    break
                used += size
                selected.append(row)
                rows.append(summary)
            limit = len(selected)
            return {'ok':True, 'requests':rows, 'cursor':selected[-1]['seq'] if len(matches)>limit else self.seq,
                    'has_more':len(matches)>limit, 'dropped':self.dropped, 'retained':len(self.rows),
                    'generation':self.generation, 'reset':reset,
                    'listening':bool(self.channels), 'attach_errors':self.attach_errors, 'content_trust':'untrusted_network_content'}

    def detail(self, body, controller=None, allow_cdp=True):
        identifier = body.get('network_id')
        if not isinstance(identifier,str) or not 1<=len(identifier)<=100:
            raise BrowserError('invalid_network_id')
        if 'include_body' in body and type(body['include_body']) is not bool:
            raise BrowserError('invalid_network_include_body')
        offset = bounded_int(body, 'offset', 0, 0, self.BODY_LIMIT)
        limit = bounded_int(body, 'max_chars', 2000, 200, 16000)
        with self.lock:
            original = self.rows.get(identifier)
            if original is None or not self._visible(original,controller):
                raise BrowserError('network_request_missing_or_expired')
            row = copy.deepcopy(original)
        detail = {k:v for k,v in row.items() if not k.startswith('_')}
        detail['sanitized'] = True
        if body.get('include_body') is True:
            state, text = self._body(row, allow_cdp=allow_cdp)
            detail['body_state'] = state
            if text is not None:
                detail.update(body=text[offset:offset+limit], next_offset=offset+limit if len(text)>offset+limit else None)
        return {'ok':True, 'request':detail, 'content_trust':'untrusted_network_content'}
