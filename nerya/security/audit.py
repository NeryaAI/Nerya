"""Security audit journal. Writes to journals/security.jsonl."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core import jsonl
from ..core.redaction import redact_dict


def record(journal_path: Path, *, kind: str, caller: str, payload: dict[str, Any]) -> None:
    jsonl.append(journal_path, {
        "kind": kind,
        "caller": caller,
        "payload": redact_dict(payload),
    })
