"""Persist large blobs (reports, charts, generated code previews) outside jsonl."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..core.atomic_write import atomic_write_bytes, atomic_write_text
from ..core.paths import WorkspacePaths


class ArtifactStore:
    def __init__(self, paths: WorkspacePaths):
        self.paths = paths

    def put_text(self, kind: str, name: str, content: str) -> Path:
        target = self.paths.artifacts / kind / name
        atomic_write_text(target, content)
        return target

    def put_bytes(self, kind: str, name: str, data: bytes) -> Path:
        target = self.paths.artifacts / kind / name
        atomic_write_bytes(target, data)
        return target

    def content_hash(self, data: bytes | str) -> str:
        if isinstance(data, str):
            data = data.encode("utf-8")
        return hashlib.sha256(data).hexdigest()
