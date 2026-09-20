"""Agent operations on an explicitly delegated managed Chromium profile.

All methods run on the Chromium owner thread. Grants are in-memory and expire;
no browser credentials or profile paths are exposed to the model. Page content
is evidence, never permission to expand the grant.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .managed_browser import BrowserError, navigation_url, public_url
from .browser_observation import bounded_int, snapshot_options


_SENSITIVE = re.compile(r"password|passphrase|private.?key|secret|seed.?phrase|mnemonic|one.?time|otp|cc-number|cc-csc|密码|助记词|私钥|验证码", re.I)
_REDACT = re.compile(r"(?i)(bearer\s+)[\w.~/+=-]+|((?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s,;]+")
_READS = {"status", "tabs", "snapshot", "screenshot", "events", "downloads", "wait_for", "extensions", "read", "network", "network_detail", "challenge_status", "wait_for_challenge"}
_ACTIONS = _READS | {"navigate", "new_tab", "select_tab", "close_tab", "back", "forward", "reload", "click", "fill", "press", "select", "check", "hover", "drag", "scroll", "handoff", "save_download", "upload", "click_xy", "extension_open", "select_text", "move"}
_SNAPSHOT = r"""({limit, maxChars, scope, offset, textOffset, compact}) => {
  const roots = scope ? document.querySelectorAll(scope) : [document];
  if (roots.length !== 1) return {error:'snapshot_scope_must_match_one_root',nodes:[],metadata:[],text:'',truncated:false};
  const root = roots[0];
  const nodes = [], metadata = [];
  const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
  const describe = e => {
    const tag = e.tagName.toLowerCase(), type = (e.getAttribute('type') || '').toLowerCase();
    const role = e.getAttribute('role') || ({button:'button', a:'link', select:'combobox', textarea:'textbox', summary:'button'}[tag]) || (tag === 'input' ? ({checkbox:'checkbox', radio:'radio', button:'button', submit:'button'}[type] || 'textbox') : 'generic');
    const labelled = (e.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => e.ownerDocument.getElementById(id)?.textContent || '').join(' ').trim();
    const labels = e.labels ? Array.from(e.labels).map(l => l.textContent || '').join(' ') : '';
    const name = (e.getAttribute('aria-label') || labelled || labels || e.getAttribute('placeholder') || (['input','textarea'].includes(tag) ? e.getAttribute('name') : e.innerText) || e.getAttribute('title') || '').trim().replace(/\s+/g,' ').slice(0,240);
    return {tag, type, role, name, autocomplete:e.getAttribute('autocomplete') || '', disabled:!!e.disabled || e.getAttribute('aria-disabled') === 'true', checked:!!e.checked, href:e.getAttribute('href') || '', test_id:e.getAttribute('data-testid') || ''};
  };
  let visited = 0, eligible = 0, truncated = false;
  const selector = compact
    ? 'a[href],button,input:not([type=hidden]),textarea,select,summary,[role=button],[role=link],[role=checkbox],[role=combobox],[role=textbox],[role=searchbox],[role=radio],[role=switch],[role=slider],[role=spinbutton],[role=tab],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=option],[contenteditable=true],[tabindex]:not([tabindex="-1"])'
    : 'a[href],button,input:not([type=hidden]),textarea,select,summary,[role],[contenteditable=true],[tabindex]:not([tabindex="-1"])';
  function walk(root) {
    for (const e of root.querySelectorAll('*')) {
      if (++visited > 12000) { truncated = true; return; }
      if (e.matches(selector) && visible(e)) {
        if (eligible++ >= offset) {
          if (nodes.length < limit) { nodes.push(e); metadata.push(describe(e)); }
          else truncated = true;
        }
      }
      if (e.shadowRoot) walk(e.shadowRoot);
    }
  }
  walk(root);
  const text = (root === document ? document.body : root)?.innerText || '';
  return {nodes, metadata, text:text.slice(textOffset,textOffset+maxChars), truncated,
    textTruncated:text.length>textOffset+maxChars};
}"""
_FINGERPRINT = r"""e => ({connected:e.isConnected, tag:e.tagName.toLowerCase(), type:(e.getAttribute('type')||'').toLowerCase(), name:e.getAttribute('name')||'', autocomplete:e.getAttribute('autocomplete')||'', label:e.getAttribute('aria-label')||'', href:e.getAttribute('href')||'', text:((['INPUT','TEXTAREA'].includes(e.tagName) ? '' : e.innerText)||'').trim().slice(0,240)})"""


def clean(value: Any, limit: int = 12000) -> str:
    return _REDACT.sub(lambda m: (m.group(1) or m.group(2) or '') + '[redacted]', str(value or ''))[:limit]


def origin(value: Any) -> str:
    navigation_url(value)
    p = urlsplit(value)
    if p.scheme not in {'http', 'https'}:
        raise BrowserError('http_origin_required')
    host = p.hostname.encode('idna').decode('ascii').lower()
    if ':' in host:
        host = '[' + host + ']'
    port = p.port
    return f'{p.scheme}://{host}' + (f':{port}' if port and port != {'http':80, 'https':443}[p.scheme] else '')


def finite_number(value: Any, default: float, low: float, high: float) -> float:
    if value is None:
        return default
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise BrowserError('invalid_numeric_option')
    return float(value)


class AgentController:
    def __init__(self, worker):
        self.worker = worker
        self.grant: dict[str, Any] = {}
        self.session_id = ''
        self.actor = ''
        self.refs: dict[str, tuple] = {}
        self.frames: dict[str, Any] = {}
        self.frame_page = None
        self.last_shot: tuple | None = None
        self.revision = 0
        self.receipts: OrderedDict = OrderedDict()
        self.events: deque = deque(maxlen=80)
        self.downloads: OrderedDict = OrderedDict()
        self.uploads: dict[str, dict] = {}
        self.attached: set = set()
        self.dialog_rule: dict | None = None
        self.deadline = 0.0
        self.in_action = False
        worker.context.on('page', self.attach_page)
        worker.context.route('**/*', self.navigation_policy)
        for page in worker.context.pages:
            self.attach_page(page)

    def navigation_policy(self, route):
        req = route.request
        delegated = self.session_id and not self.worker.paused and not self.worker.interrupted.is_set()
        if delegated and req.is_navigation_request() and (time.time() >= self.grant.get('expires_at', 0) or not self.allowed(req.url)):
            self.events.append({'kind':'navigation_blocked', 'url':'[not authorized]'})
            route.abort('blockedbyclient')
        else:
            route.continue_()

    def record_event(self, page, event):
        if self.session_id and self.allowed(page.url) and not self.worker.paused and not self.worker.interrupted.is_set():
            self.events.append({**event, 'url':public_url(page.url)})

    def attach_page(self, page):
        if page in self.attached:
            return
        self.attached.add(page)
        page.on('dialog', lambda dialog: self.on_dialog(page, dialog))
        page.on('download', lambda download: self.on_download(page, download))
        page.on('pageerror', lambda _error: self.record_event(page, {'kind':'pageerror'}))
        page.on('console', lambda msg: self.record_event(page, {'kind':'console', 'level':msg.type}))
        page.on('requestfailed', lambda req: self.record_event(page, {'kind':'requestfailed', 'method':req.method}))

    def on_dialog(self, page, dialog):
        rule, self.dialog_rule = self.dialog_rule, None
        accepted = bool(self.in_action and not self.worker.interrupted.is_set() and rule and rule.get('type') == dialog.type and rule.get('message') == dialog.message and rule.get('accept') is True)
        self.record_event(page, {'kind':'dialog', 'type':dialog.type, 'message':clean(dialog.message, 500), 'accepted':accepted})
        if accepted:
            dialog.accept(str(rule.get('prompt_text', ''))[:2000])
        else:
            dialog.dismiss()  # Never leave the sync worker blocked on a dialog.

    def on_download(self, page, download):
        if not self.in_action or not self.allowed(page.url) or not self.grant.get('downloads'):
            return
        key = secrets.token_hex(8)
        self.downloads[key] = (download, origin(page.url))
        while len(self.downloads) > 30:
            self.downloads.popitem(last=False)
        self.events.append({'kind':'download', 'id':key, 'name':clean(download.suggested_filename, 200), 'url':public_url(page.url)})

    def grant_access(self, body):
        all_web = body.get('all_web') is True
        origins = body.get('origins', [])
        if not isinstance(origins, list) or (not all_web and not 1 <= len(origins) <= 40):
            raise BrowserError('choose_1_to_40_site_origins')
        sites = sorted(set(origin(url) for url in origins))
        ttl = finite_number(body.get('ttl_s'), 3600, 60, 86400)
        self.release()
        self.events.clear()
        self.downloads.clear()
        self.uploads.clear()
        self.grant = {'origins':sites, 'all_web':all_web, 'expires_at':time.time()+ttl,
                      'screenshots':body.get('screenshots') is True,
                      'downloads':body.get('downloads') is True}
        self.worker.interrupted.clear()
        return self.access_status()

    def access_status(self):
        return {'enabled':bool(self.grant and time.time() < self.grant['expires_at']),
                **self.grant, 'occupied':bool(self.session_id), 'session_id':self.session_id,
                'executing':self.in_action, 'revision':self.revision,
                'uploads':[{'id':k, 'name':v['name']} for k,v in self.uploads.items()],
                'last_actions':list(self.events)[-8:]}

    def revoke(self):
        self.grant = {}
        self.release()
        self.worker.paused = True
        self.worker.interrupted.set()
        return self.access_status()

    def release(self):
        self.session_id = self.actor = ''
        self.clear_refs()
        self.receipts.clear()

    def clear_refs(self):
        self.last_shot = None
        for handle, _, _ in self.refs.values():
            try:
                handle.dispose()
            except Exception:
                pass
        self.refs.clear()

    def allowed(self, url):
        if url == 'about:blank':
            return True
        if url.startswith('chrome-extension://'):
            return not self.worker._protected_url(url)
        try:
            site = origin(url)
            return self.grant.get('all_web') is True or site in self.grant.get('origins', [])
        except BrowserError:
            return False

    def guard(self, *, page=None, check_page=True):
        if not self.grant or time.time() >= self.grant['expires_at']:
            raise BrowserError('browser_grant_required_or_expired')
        if self.worker.interrupted.is_set() or self.worker.paused:
            raise BrowserError('human_handoff_active')
        if self.worker._has_protected_target():
            self.worker.paused = True
            raise BrowserError('human_handoff_active')
        page = page or self.worker.page
        if check_page and page is not None and not self.allowed(page.url):
            raise BrowserError('site_not_authorized')

    def network_read(self, body, actor):
        # No Playwright access here: metadata can be read during a running batch.
        def validate():
            if not self.session_id or body.get('session_id') != self.session_id or actor != self.actor:
                raise BrowserError('browser_session_owner_mismatch')
            if not self.grant or time.time() >= self.grant.get('expires_at', 0):
                raise BrowserError('browser_grant_required_or_expired')
            if self.worker.paused or self.worker.interrupted.is_set():
                raise BrowserError('human_handoff_active')
        validate()
        result = (self.worker.network.detail(body, self, allow_cdp=False) if body.get('operation')=='network_detail'
                  else self.worker.network.read(body, self))
        validate()
        return {**result, 'session_id':self.session_id, 'request_id':body.get('request_id')}

    def publish_surface(self):
        try:
            self.worker.surface_state = {**self.state(), 'running':True,
                'human_control':getattr(self.worker, 'human_control', False),
                'agent_access':self.access_status(),
                'challenge':self.worker.network.challenge_state(self.worker.page) if getattr(self.worker,'network',None) else {'state':'none'}}
        except Exception:
            pass  # Observation must not change an executed action's result.

    def timeout_ms(self, body):
        requested = finite_number(body.get('timeout_ms'), 5000, 100, 15000)
        remaining = (self.deadline - time.monotonic()) * 1000
        if remaining <= 0:
            raise BrowserError('batch_deadline_reached')
        return max(1, min(requested, remaining))

    def state(self):
        w = self.worker
        w._sync_pages()
        return {'session_id':self.session_id, 'profile_id':w.config['id'], 'paused':w.paused or w.interrupted.is_set(),
                'tabs':[{'id':k, 'selected':p is w.page, 'url':public_url(p.url) if self.allowed(p.url) else '[not authorized]', 'authorized':self.allowed(p.url)} for k,p in w.pages.items() if not p.is_closed()],
                'revision':self.revision}

    def frame(self, frame_id=None):
        page = self.worker.page
        if page is None or page.is_closed():
            raise BrowserError('no_open_tab')
        if self.frame_page is not page:
            self.frames = {'0':page.main_frame}
            self.frame_page = page
        for item in page.frames:
            if item not in self.frames.values():
                self.frames[str(len(self.frames))] = item
        frame = self.frames.get(str(frame_id or '0'))
        if frame is None or frame.is_detached():
            raise BrowserError('frame_not_found_refresh_snapshot')
        # about:srcdoc inherits its parent; opaque data frames are not delegated.
        url = frame.url
        ancestor = frame
        while url in {'about:blank', 'about:srcdoc'} and ancestor.parent_frame:
            ancestor = ancestor.parent_frame
            url = ancestor.url
        if not self.allowed(url):
            raise BrowserError('frame_origin_not_authorized')
        return frame

    def snapshot(self, body):
        self.guard()
        options = snapshot_options(body)
        self.clear_refs()
        self.revision += 1
        frame = self.frame(body.get('frame_id'))
        handle = frame.evaluate_handle(_SNAPSHOT, options)
        props = handle.get_properties()
        if 'error' in props:
            code = props['error'].json_value()
            for prop in props.values():
                prop.dispose()
            handle.dispose()
            raise BrowserError(code)
        metadata = props['metadata'].json_value()
        nodes = props['nodes'].get_properties()
        rows = []
        for i, item in enumerate(metadata):
            node = nodes.get(str(i))
            element = node.as_element() if node else None
            sensitive = bool(_SENSITIVE.search(' '.join(str(item.get(k,'')) for k in ('name','type','autocomplete'))))
            if element is None:
                continue
            if sensitive:
                element.dispose()
                rows.append({'role':item['role'], 'name':'[sensitive input: native handoff]', 'protected':True})
                continue
            ref = f'r{self.revision}_{i}'
            # The element handle, not a DOM attribute, is the reference authority.
            fingerprint = element.evaluate(_FINGERPRINT)
            self.refs[ref] = (element, frame, fingerprint)
            row = {'ref':ref, 'role':item['role'], 'name':clean(item['name'], 120 if options['compact'] else 240)}
            for flag in ('disabled','checked'):
                if item[flag] or not options['compact']:
                    row[flag] = item[flag]
            rows.append(row)
        text = clean(props['text'].json_value(), options['maxChars'])
        truncated = bool(props['truncated'].json_value())
        text_truncated = bool(props['textTruncated'].json_value())
        for prop in props.values():
            prop.dispose()
        handle.dispose()
        self.guard()
        recorder = getattr(self, 'recorder', None)
        if recorder:
            recorder.visualize('dom', elements=[v[0] for v in list(self.refs.values())[:30]])
        text_hash = hashlib.sha256(text.encode()).hexdigest()[:20]
        text_data = {'text_unchanged':True} if body.get('known_text_hash') == text_hash else {'text':text}
        return {**text_data, 'text_hash':text_hash, 'elements':rows, 'truncated':truncated or text_truncated,
                'mode':'compact' if options['compact'] else 'full',
                'next_element_offset':options['offset']+options['limit'] if truncated else None,
                'next_text_offset':options['textOffset']+options['maxChars'] if text_truncated else None,
                'frame_id':next(k for k,v in self.frames.items() if v is frame),
                'frames':[{'id':k, 'url':public_url(f.url) if self.allowed(f.url) else '[not authorized]'} for k,f in self.frames.items() if not f.is_detached() and f in self.worker.page.frames],
                'content_trust':'untrusted_page_content', 'revision':self.revision}

    def target(self, body, key='target'):
        spec = body.get(key)
        if spec is None:
            spec = {k:body[k] for k in ('ref','role','name','label','text','test_id','selector','frame_id') if k in body}
        if not isinstance(spec, dict) or not spec:
            raise BrowserError('target_required_use_snapshot_ref_or_role')
        if spec.get('ref'):
            entry = self.refs.get(spec['ref'])
            if entry is None:
                raise BrowserError('stale_reference_refresh_snapshot')
            element, frame, fingerprint = entry
            if frame not in self.worker.page.frames or frame.is_detached():
                raise BrowserError('stale_reference_refresh_snapshot')
            if frame.url not in {'about:blank', 'about:srcdoc'} and not self.allowed(frame.url):
                raise BrowserError('frame_origin_not_authorized')
            if frame.url in {'about:blank', 'about:srcdoc'}:
                self.frame(next((k for k,v in self.frames.items() if v is frame), '__missing__'))
            current = element.evaluate(_FINGERPRINT)
            if not current['connected'] or current != fingerprint:
                raise BrowserError('stale_reference_refresh_snapshot')
            return element
        frame = self.frame(spec.get('frame_id', body.get('frame_id')))
        exact = spec.get('exact', True)
        if type(exact) is not bool:
            raise BrowserError('invalid_target')
        if spec.get('role'):
            locator = frame.get_by_role(str(spec['role']), name=str(spec.get('name','')), exact=exact)
        elif spec.get('label'):
            locator = frame.get_by_label(str(spec['label']), exact=exact)
        elif spec.get('text'):
            locator = frame.get_by_text(str(spec['text']), exact=exact)
        elif spec.get('test_id'):
            locator = frame.get_by_test_id(str(spec['test_id']))
        elif spec.get('selector'):
            selector = str(spec['selector'])
            if len(selector) > 1000:
                raise BrowserError('invalid_target')
            locator = frame.locator(selector)
        else:
            raise BrowserError('invalid_target')
        locator.wait_for(state='attached', timeout=self.timeout_ms(body))
        # Locators re-resolve during actionability waits on re-rendering pages.
        # Never .first() or force-click a duplicate; references remain exact handles.
        if locator.count() != 1:
            raise BrowserError('ambiguous_target_refine_locator')
        self.check_sensitive(locator)
        return locator

    def check_sensitive(self, element):
        meta = element.evaluate(_FINGERPRINT)
        if _SENSITIVE.search(' '.join(str(meta[k]) for k in ('type','name','autocomplete','label'))):
            self.worker.paused = True
            raise BrowserError('sensitive_input_requires_native_handoff')

    def execute_step(self, body):
        action = body.get('action')
        if action not in _ACTIONS:
            raise BrowserError('unsupported_agent_action')
        self.guard(check_page=action not in {'navigate','new_tab','select_tab','status','tabs','handoff'})
        w, page = self.worker, self.worker.page
        if action == 'network':
            return w.network.read(body, self)
        if action == 'network_detail':
            result = w.network.detail(body, self)
            self.guard()
            return result
        if action in {'challenge_status','wait_for_challenge'}:
            from . import browser_challenge
            return {'challenge': browser_challenge.wait(self, body) if action=='wait_for_challenge' else browser_challenge.status(self)}
        if action not in _READS and action not in {'navigate','new_tab','select_tab','handoff','back','forward','reload','close_tab'}:
            from .browser_challenge import wait
            wait(self, body)
        if action in {'status','tabs'}:
            return {}
        if action == 'snapshot':
            return {'snapshot':self.snapshot(body)}
        if action == 'read':
            element = self.target(body)
            self.check_sensitive(element)
            offset = bounded_int(body, 'offset', 0, 0, 1000000)
            limit = bounded_int(body, 'max_chars', 2000, 200, 16000)
            data = element.evaluate('(e, o) => { const t=e.innerText||e.textContent||""; return {text:t.slice(o.offset,o.offset+o.limit),more:t.length>o.offset+o.limit}; }', {'offset':offset,'limit':limit})
            self.guard()
            if getattr(self,'recorder',None):
                self.recorder.visualize('read', elements=[element])
            return {'text':clean(data['text'],limit), 'offset':offset, 'next_offset':offset+limit if data['more'] else None,
                    'content_trust':'untrusted_page_content'}
        if action == 'handoff':
            w.interrupted.set()
            w.paused = True
            if page is not None:
                page.bring_to_front()
            return {'handoff':True}
        if action == 'extensions':
            return {'extensions':[{'id':e['extension_id'], 'name':clean(e['name'],200), 'ui_control':bool(e.get('control_ui'))} for e in w.config['extensions'] if e.get('enabled', True)]}
        if action == 'extension_open':
            entry = next((e for e in w.config['extensions'] if e.get('extension_id') == body.get('extension_id') and e.get('control_ui') and e.get('enabled', True)), None)
            if not entry:
                raise BrowserError('operator_extension_ui_grant_required')
            w._command('extension_open', {'extension_id':entry['extension_id']})
            self.clear_refs()
            self.guard()
            return {}
        if action == 'events':
            return {'events':list(self.events)}
        if action == 'downloads':
            return {'downloads':[{'id':k,'name':clean(v[0].suggested_filename,200)} for k,v in self.downloads.items() if self.allowed(v[1])]}
        if action == 'screenshot':
            if not self.grant.get('screenshots'):
                raise BrowserError('screenshot_permission_required')
            for index, frame in enumerate(page.frames):
                if frame.url not in {'about:blank', 'about:srcdoc'} and not self.allowed(frame.url):
                    raise BrowserError('screenshot_contains_unauthorized_frame')
            image = w._command('screenshot', {})['image']
            self.guard()
            shot_id = secrets.token_hex(12)
            self.last_shot = (shot_id,page,page.url,self.revision,time.monotonic())
            return {'image':image,'screenshot_id':shot_id,'viewport':{'width':1280,'height':800}}
        if action == 'select_tab':
            w._sync_pages()
            target = w.pages.get(str(body.get('tab_id')))
            if target is None or target.is_closed():
                raise BrowserError('tab_not_found')
            self.guard(page=target)
            w.page = target
            self.clear_refs()
            return {}
        if action in {'navigate','new_tab'}:
            url = navigation_url(body.get('url', 'about:blank'))
            if not self.allowed(url):
                raise BrowserError('site_not_authorized')
            if action == 'new_tab' or page is None or page.is_closed():
                w.page = page = w.context.new_page()
            page.goto(url, wait_until='domcontentloaded', timeout=self.timeout_ms(body))
            self.clear_refs()
        elif action in {'back','forward','reload'}:
            getattr(page, {'back':'go_back','forward':'go_forward','reload':'reload'}[action])(wait_until='domcontentloaded', timeout=self.timeout_ms(body))
            self.clear_refs()
        elif action == 'close_tab':
            page.close()
            w._sync_pages()
            self.clear_refs()
        elif action == 'wait_for':
            from playwright.sync_api import expect
            if body.get('url'):
                if not self.allowed(body['url']):
                    raise BrowserError('site_not_authorized')
                page.wait_for_url(body['url'], timeout=self.timeout_ms(body), wait_until='domcontentloaded')
            else:
                spec = body.get('target', {})
                if not isinstance(spec, dict) or type(spec.get('exact', True)) is not bool:
                    raise BrowserError('invalid_target')
                frame = self.frame(spec.get('frame_id', body.get('frame_id')))
                if spec.get('text'):
                    locator = frame.get_by_text(str(spec['text']), exact=spec.get('exact', True))
                elif spec.get('role'):
                    locator = frame.get_by_role(str(spec['role']), name=str(spec.get('name','')), exact=spec.get('exact', True))
                elif spec.get('label'):
                    locator = frame.get_by_label(str(spec['label']), exact=spec.get('exact', True))
                elif spec.get('test_id'):
                    locator = frame.get_by_test_id(str(spec['test_id']))
                elif spec.get('selector'):
                    locator = frame.locator(str(spec['selector']))
                else:
                    raise BrowserError('wait_target_required')
                state = body.get('state','visible')
                if state in {'visible','hidden','attached','detached'}:
                    locator.wait_for(state=state, timeout=self.timeout_ms(body))
                elif state == 'enabled':
                    expect(locator).to_be_enabled(timeout=self.timeout_ms(body))
                else:
                    raise BrowserError('invalid_wait_state')
        elif action == 'click_xy':
            shot = self.last_shot
            if not shot or body.get('screenshot_id') != shot[0] or page is not shot[1] or page.url != shot[2] or self.revision != shot[3] or time.monotonic()-shot[4]>60:
                raise BrowserError('fresh_screenshot_required_for_coordinates')
            x=finite_number(body.get('x'),-1,0,1279)
            y=finite_number(body.get('y'),-1,0,799)
            if x<0 or y<0:
                raise BrowserError('coordinates_required')
            element=page.evaluate_handle('({x,y}) => { let e=document.elementFromPoint(x,y); while(e?.shadowRoot?.elementFromPoint(x,y)) { const n=e.shadowRoot.elementFromPoint(x,y); if(n===e) break; e=n; } return e; }', {'x':x,'y':y}).as_element()
            if element is None:
                raise BrowserError('no_element_at_coordinates')
            try:
                if element.evaluate("e => e.tagName === 'IFRAME'"):
                    raise BrowserError('use_frame_locator_for_iframe')
                self.check_sensitive(element)
                self.guard()
                if getattr(self, 'recorder', None):
                    self.recorder.visualize('click', elements=[element], point={'x':x,'y':y})
                page.mouse.click(x,y)
            finally:
                element.dispose()
                self.last_shot=None
        elif action == 'move':
            x = finite_number(body.get('x'), -1, 0, 1279)
            y = finite_number(body.get('y'), -1, 0, 799)
            if x < 0 or y < 0:
                raise BrowserError('coordinates_required')
            page.mouse.move(x, y, steps=8)
            if getattr(self, 'recorder', None):
                self.recorder.visualize('move', point={'x':x,'y':y})
        elif action == 'scroll':
            if getattr(self, 'recorder', None):
                self.recorder.visualize('scroll')
            dy = finite_number(body.get('dy'), 500, -3000, 3000)
            page.mouse.wheel(0,dy)
        elif action == 'save_download':
            if not self.grant.get('downloads'):
                raise BrowserError('download_permission_required')
            entry = self.downloads.get(str(body.get('download_id')))
            if not entry or not self.allowed(entry[1]):
                raise BrowserError('download_not_found')
            directory = w.directory / 'downloads'
            if directory.is_symlink():
                raise BrowserError('download_directory_symlink')
            directory.mkdir(mode=0o700, exist_ok=True)
            name = re.sub(r'[^\w. -]', '_', Path(entry[0].suggested_filename).name)[:150] or 'download.bin'
            path = directory / (secrets.token_hex(8)+'-'+name)
            entry[0].save_as(str(path))
            return {'download':{'path':str(path), 'name':name, 'bytes':path.stat().st_size}, 'untrusted_file':True}
        else:
            element = self.target(body)
            self.check_sensitive(element)
            self.guard()
            if getattr(self, 'recorder', None):
                self.recorder.visualize(action, elements=[element])
            if action == 'select_text':
                element.select_text(timeout=self.timeout_ms(body))
            elif action == 'fill':
                text = body.get('text', '')
                if not isinstance(text,str) or len(text)>10000:
                    raise BrowserError('invalid_text')
                element.fill(text, timeout=self.timeout_ms(body))
            elif action == 'select':
                value = body.get('value')
                if not isinstance(value,(str,list)):
                    raise BrowserError('select_value_required')
                element.select_option(value=value, timeout=self.timeout_ms(body))
            elif action == 'check':
                checked = body.get('checked',True)
                if type(checked) is not bool:
                    raise BrowserError('invalid_checked')
                element.set_checked(checked, timeout=self.timeout_ms(body))
            elif action == 'press':
                key = body.get('key')
                if not isinstance(key,str) or len(key)>60 or any(x in key for x in ('Meta','Control','F12')):
                    raise BrowserError('unsupported_key')
                element.press(key, timeout=self.timeout_ms(body))
            elif action == 'hover':
                element.hover(timeout=self.timeout_ms(body))
            elif action == 'drag':
                target = self.target(body,'to')
                a,b = element.bounding_box(),target.bounding_box()
                if not a or not b:
                    raise BrowserError('drag_target_not_visible')
                element.hover(timeout=self.timeout_ms(body))
                page.mouse.down()
                try:
                    page.mouse.move(b['x']+b['width']/2,b['y']+b['height']/2,steps=12)
                finally:
                    page.mouse.up()
            elif action == 'upload':
                ids = body.get('upload_ids',[])
                if not isinstance(ids,list) or not 1<=len(ids)<=8 or any(i not in self.uploads for i in ids):
                    raise BrowserError('operator_staged_upload_required')
                paths = []
                for key in ids:
                    item = self.uploads[key]
                    path = Path(item['path'])
                    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
                        raise BrowserError('staged_upload_changed')
                    paths.append(str(path))
                element.set_input_files(paths, timeout=self.timeout_ms(body))
            elif action == 'click':
                rule = body.get('dialog')
                if rule is not None and (not isinstance(rule,dict) or not isinstance(rule.get('message'),str)):
                    raise BrowserError('exact_dialog_message_required')
                self.dialog_rule = rule
                if body.get('expect_popup') is True:
                    with page.expect_popup(timeout=self.timeout_ms(body)) as popup:
                        element.click(timeout=self.timeout_ms(body))
                    w.page = popup.value
                    w.page.wait_for_load_state('domcontentloaded',timeout=self.timeout_ms(body))
                elif body.get('expect_download') is True:
                    with page.expect_download(timeout=self.timeout_ms(body)):
                        element.click(timeout=self.timeout_ms(body))
                else:
                    element.click(timeout=self.timeout_ms(body))
        self.guard()
        if action in {'navigate','new_tab','back','forward','reload'}:
            from .browser_challenge import wait
            state = wait(self, body)
            if state['state'] != 'none':
                return {'challenge':state}
        return {}

    def run(self, body, actor):
        from . import browser_trace
        body = dict(body)
        trace = browser_trace.resolve(getattr(self.worker, 'root', self.worker.directory), body.pop('_trace_token', None))
        self.recorder = browser_trace.Recorder(self, trace, actor) if trace else None
        result = {'ok': False}
        try:
            result = self._run(body, actor)
            return result
        except Exception as exc:
            if trace:
                trace.emit('request_failed', action=str(body.get('operation', 'status'))[:40],
                           error=str(exc) if isinstance(exc, BrowserError) else 'browser_operation_failed')
            raise
        finally:
            if self.recorder:
                self.recorder.finish(result)
            self.recorder = None

    def _run(self, body, actor):
        assert self.worker.is_owner_thread()
        self.worker._sync_pages()
        op = body.get('operation','status')
        snapshot_options(body)  # Validate observation budgets before any side effect.
        bounded_int(body, 'challenge_timeout_ms', 8000, 0, 15000)
        if not isinstance(actor,str) or not actor:
            raise BrowserError('trusted_actor_required')
        if op == 'list':
            return {'ok':True, 'sessions':[self.state()] if self.session_id and actor==self.actor else [], 'grant_enabled':self.access_status()['enabled']}
        if op == 'status' and not self.session_id:
            return {'ok':True, 'grant_enabled':self.access_status()['enabled'], 'sessions':[]}
        if op != 'open' and (body.get('session_id') != self.session_id or actor!=self.actor or not self.session_id):
            raise BrowserError('browser_session_owner_mismatch')
        request_id = body.get('request_id')
        if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{8,100}',request_id):
            raise BrowserError('request_id_required')
        if self.recorder and op != 'open':
            self.recorder.permit()
        if op == 'close':
            self.release()
            return {'ok':True,'released':True,'browser_kept_open':True}
        if op in {'status','tabs'}:
            return {'ok':True, **self.state(), 'request_id':request_id, 'grant_enabled':self.access_status()['enabled']}
        self.guard(check_page=op not in {'open','navigate','new_tab','select_tab','handoff'})
        digest = hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        cached = self.receipts.get((actor,request_id))
        if cached:
            if cached[0]!=digest:
                raise BrowserError('request_id_payload_conflict')
            if self.recorder:
                self.recorder.trace.emit('replayed', action=op)
            return {**cached[1], 'replayed':True}
        if op == 'open':
            if self.session_id:
                raise BrowserError('browser_in_use_use_existing_session_or_release')
            self.session_id, self.actor = 'mb_'+secrets.token_hex(12), actor
            if self.recorder:
                self.recorder.permit()
            steps = [{'action':'navigate','url':body['url']}] if body.get('url') else []
        elif op == 'close':
            self.release()
            return {'ok':True,'released':True,'browser_kept_open':True}
        elif op == 'batch':
            steps = body.get('steps')
            if not isinstance(steps,list) or not 1<=len(steps)<=12 or any(not isinstance(s,dict) for s in steps):
                raise BrowserError('batch_requires_1_to_12_steps')
        elif op in _ACTIONS:
            steps = [{**body,'action':op}]
        else:
            raise BrowserError('unsupported_agent_operation')
        started = time.monotonic()
        self.deadline = started + 25
        completed, result = [], {'ok':True}
        self.in_action = True
        self.publish_surface()
        try:
            for index, step in enumerate(steps):
                if self.worker.interrupted.is_set():
                    raise BrowserError('human_handoff_active')
                self.timeout_ms(step)
                if self.recorder:
                    self.recorder.step(index, step, 'started')
                if getattr(self.worker, 'network', None):
                    self.worker.network.drain()
                result.update(self.execute_step(step))
                if getattr(self.worker, 'network', None):
                    self.worker.network.drain()
                if self.recorder:
                    self.recorder.step(index, step, 'completed')
                self.publish_surface()
                completed.append({'index':index,'action':step.get('action'),'ok':True})
                self.events.append({'kind':'action','action':step.get('action'),'ok':True,'time':time.time()})
                if self.worker.paused:
                    if self.recorder:
                        for skipped in range(index + 1, len(steps)):
                            self.recorder.trace.emit('step', index=skipped, action=steps[skipped].get('action'), phase='skipped')
                    break
            if (not self.worker.paused and self.worker.page is not None
                    and op not in {'snapshot','screenshot','events','downloads','status','tabs','handoff','extensions','read','network','network_detail','challenge_status'}
                    and body.get('observation') != 'none'):
                try:
                    result['snapshot'] = self.snapshot(body)
                except Exception as exc:
                    result['observation_error'] = str(exc) if isinstance(exc,BrowserError) else 'snapshot_unavailable'
        except Exception as exc:
            code = str(exc) if isinstance(exc,BrowserError) else ('action_timeout' if type(exc).__name__=='TimeoutError' else 'browser_action_failed')
            if 'strict mode violation' in str(exc):
                code = 'ambiguous_target_refine_locator'
            if self.recorder and len(completed) < len(steps):
                self.recorder.step(len(completed), steps[len(completed)], 'failed', code)
                for skipped in range(len(completed) + 1, len(steps)):
                    self.recorder.trace.emit('step', index=skipped, action=steps[skipped].get('action'), phase='skipped')
            result.update({'ok':False,'error':code,
                           'failed_step':len(completed),'outcome':'inspect_before_retry', 'retryable':False})
        finally:
            self.in_action = False
            self.dialog_rule = None
        if not result['ok'] and time.monotonic() < self.deadline:
            try:
                result['snapshot'] = self.snapshot(body)
            except Exception:
                pass
        if body.get('observation') == 'none' and result['ok'] and op not in _READS:
            result['needs_observation'] = True
        result.update(self.state())
        result.update({'request_id':request_id, 'completed_steps':completed, 'elapsed_ms':round((time.monotonic()-started)*1000),
                       'next_action':('snapshot_or_handoff' if not result['ok'] else 'observe_before_next_action' if result.get('needs_observation') else 'use_returned_snapshot' if 'snapshot' in result else 'use_returned_observation'),
                       'events':list(self.events)[-8:]})
        if result.get('error') == 'challenge_requires_handoff':
            result['next_action'] = 'request_operator_takeover'
            result['challenge'] = self.worker.network.challenge_state(self.worker.page)
        # Cache metadata only: screenshots must not survive in replay receipts.
        self.receipts[(actor,request_id)] = (digest,{k:v for k,v in result.items() if k!='image'})
        while len(self.receipts)>128:
            self.receipts.popitem(last=False)
        return result

    def stage_upload(self, body):
        name, raw = body.get('name'), body.get('data')
        if not isinstance(name,str) or not name or len(name)>200 or not isinstance(raw,str) or len(raw)>14*1024*1024:
            raise BrowserError('invalid_upload_max_10mb')
        try:
            data=base64.b64decode(raw,validate=True)
        except ValueError:
            raise BrowserError('invalid_upload_encoding') from None
        if len(data)>10*1024*1024 or len(self.uploads)>=20:
            raise BrowserError('upload_limit')
        directory=self.worker.directory/'uploads'
        if directory.is_symlink():
            raise BrowserError('upload_directory_symlink')
        directory.mkdir(mode=0o700,exist_ok=True)
        key=secrets.token_hex(12)
        safe_name = re.sub(r'[^\w. -]', '_', Path(name).name)[:150] or 'upload.bin'
        path=directory/(key+'-'+safe_name)
        with path.open('xb') as stream:
            stream.write(data)
        path.chmod(0o600)
        self.uploads[key]={'path':str(path),'name':Path(name).name,'sha256':hashlib.sha256(data).hexdigest()}
        return {'id':key,'name':Path(name).name,'bytes':len(data)}
