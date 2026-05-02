"""Helpers for reading in-flight strategy proposal files."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..core.paths import WorkspacePaths
from ..evolution.patch_proposal import list_proposals


def read_proposal_strategy_files(
    paths: WorkspacePaths,
    proposal_id: str,
) -> tuple[Optional[str], dict[str, str]]:
    """Return ``(strategy_id, files)`` for a strategy package proposal.

    Proposal previews are operator-facing. A single non-UTF-8 auxiliary
    file should not crash the whole strategy page; decode such files with
    replacement characters and let validation decide whether the package
    itself is acceptable.
    """

    for prop in list_proposals(paths):
        if prop.id != proposal_id:
            continue
        after_dir = prop.path / "after" / "strategies"
        if not after_dir.exists():
            return None, {}
        candidates = [d for d in after_dir.iterdir() if d.is_dir()]
        if not candidates:
            return None, {}
        sd = candidates[0]
        files: dict[str, str] = {}
        for p in sd.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(sd).as_posix()
            try:
                files[rel] = _read_text_lossy(p)
            except OSError:
                continue
        return sd.name, files
    return None, {}


def _read_text_lossy(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")
