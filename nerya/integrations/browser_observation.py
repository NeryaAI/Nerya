"""Small explicit observation budgets; never cache an action as evidence."""
from __future__ import annotations

from .managed_browser import BrowserError


def bounded_int(body, name, default, low, high):
    value = body.get(name, default)
    if type(value) is not int or not low <= value <= high:
        raise BrowserError('invalid_observation_' + name)
    return value


def snapshot_options(body):
    mode = body.get('observation', 'compact')
    if not isinstance(mode, str) or mode not in {'compact', 'full', 'none'}:
        raise BrowserError('invalid_observation_mode')
    full = mode == 'full'
    scope = body.get('scope', '')
    if not isinstance(scope, str) or len(scope) > 1000:
        raise BrowserError('invalid_snapshot_scope')
    return {'limit': bounded_int(body, 'max_elements', 150 if full else 60, 1, 150),
            'maxChars': bounded_int(body, 'max_chars', 10000 if full else 1600, 200, 16000),
            'offset': bounded_int(body, 'element_offset', 0, 0, 12000),
            'textOffset': bounded_int(body, 'text_offset', 0, 0, 1000000),
            'scope': scope, 'compact': not full}
