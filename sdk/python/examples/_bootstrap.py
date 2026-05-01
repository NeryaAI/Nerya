"""Runtime path bootstrap for Nerya Python SDK examples.

Background: the repository-level ``pyproject.toml`` registers
``nerya_sdk`` as a top-level package sourced from ``sdk/python/nerya_sdk``.
After ``pip install -e .`` from the repo root, ``import nerya_sdk`` works
out of the box.

However, the docs and quick-start guides also tell first-time operators
to run these example scripts directly from the repository root::

    python sdk/python/examples/direct_order_strategy.py

In that invocation Python only adds the *script* directory
(``sdk/python/examples``) to ``sys.path``, so a freshly-cloned repo
without an editable install cannot resolve ``nerya_sdk`` (or the
runtime ``nerya`` package sitting at the repo root).

Rather than forcing every newcomer to remember ``pip install -e .``
before they can try the SDK, each example imports this helper as the
first module import. The helper is idempotent and makes sure both the
SDK source tree and the repo root are reachable, so the examples stay
honest against the audit requirement that documented SDK commands
must run as written.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SDK_PY_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
_REPO_ROOT = os.path.abspath(os.path.join(_SDK_PY_ROOT, os.pardir, os.pardir))


def _ensure(path: str) -> None:
    if path and os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)


_ensure(_SDK_PY_ROOT)
_ensure(_REPO_ROOT)


__all__ = ["_SDK_PY_ROOT", "_REPO_ROOT"]
