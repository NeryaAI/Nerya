"""Durable storage for web-research evidence captures.

Research captures are operational evidence records, analogous to journals and
oversized tool-result records.  They are not agent-authored code, skills,
strategies, or runtime configuration, so they do not enter the proposal flow.
Keeping persistence behind this store also prevents agent-callable handlers
from writing workspace paths directly.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.atomic_write import atomic_write_text


def _slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(text or "")).strip("-").lower()
    return slug[:max_len] or "capture"


@dataclass(frozen=True)
class ResearchCaptureStore:
    """Write complete source payloads below ``state/research_data``."""

    workspace_root: Path

    def store(
        self,
        *,
        kind: str,
        subject: str,
        data: dict[str, Any],
    ) -> str:
        root = Path(self.workspace_root)
        day = time.strftime("%Y-%m-%d", time.gmtime())
        out_dir = root / "state" / "research_data" / day
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H%M%S", time.gmtime())
        suffix = uuid.uuid4().hex[:6]
        path = out_dir / f"{stamp}_{suffix}_{_slugify(kind)}_{_slugify(subject)}.json"
        record = {
            "kind": kind,
            "subject": subject,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "data": data,
        }
        atomic_write_text(
            path,
            json.dumps(record, ensure_ascii=False, indent=2, default=str),
        )
        return path.relative_to(root).as_posix()


__all__ = ["ResearchCaptureStore"]
