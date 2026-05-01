"""Tiny template renderer — `${field}` substitution, no code execution."""

from __future__ import annotations

import re
from typing import Any

_PAT = re.compile(r"\$\{([a-zA-Z0-9_.]+)\}")


def render(template: str, context: dict[str, Any]) -> str:
    def _get(key: str) -> str:
        node: Any = context
        for part in key.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            else:
                node = getattr(node, part, None)
            if node is None:
                return ""
        return str(node)

    return _PAT.sub(lambda m: _get(m.group(1)), template)
