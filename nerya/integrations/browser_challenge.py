"""Recognize challenge responses and wait for normal navigation, never solve them."""
from __future__ import annotations
import time
from urllib.parse import urlsplit
from .managed_browser import BrowserError
from .browser_observation import bounded_int


def status(controller):
    w = controller.worker
    if not getattr(w, 'network', None):
        return {'state':'none'}
    result = w.network.challenge_state(w.page)
    if result['state'] == 'none' and w.page:
        widget = any('challenges.cloudflare.com' == urlsplit(f.url).hostname
                     for f in w.page.frames)
        if widget:
            return {'state':'widget_present', 'provider':'cloudflare', 'blocking':False}
    return result


def wait(controller, body):
    w = controller.worker
    budget = bounded_int(body, 'challenge_timeout_ms', 8000, 0, 15000)
    end = min(controller.deadline, time.monotonic()+budget/1000)
    initial = status(controller)
    if initial['state'] == 'handoff_required':
        raise BrowserError('challenge_requires_handoff')
    if initial['state'] != 'waiting':
        return initial
    recorder = getattr(controller, 'recorder', None)
    if recorder:
        recorder.trace.emit('challenge', phase='waiting', provider='cloudflare')
        recorder.follow()
    while time.monotonic() < end:
        controller.guard()
        controller.publish_surface()
        w.page.wait_for_timeout(min(100, max(1, (end-time.monotonic())*1000)))
        current = status(controller)
        if current['state'] == 'cleared':
            controller.clear_refs()
            if recorder:
                recorder.trace.emit('challenge', phase='cleared', provider='cloudflare')
            return {**current, 'needs_task_verification':True}
    w.network.mark_handoff(w.page)
    controller.publish_surface()
    if recorder:
        recorder.trace.emit('challenge', phase='handoff_required', provider='cloudflare')
    raise BrowserError('challenge_requires_handoff')
