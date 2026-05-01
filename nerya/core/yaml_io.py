"""Thin yaml wrapper with safe defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .atomic_write import atomic_write_text


def load(path: Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if data is not None else default


def dump(path: Path, data: Any) -> None:
    atomic_write_text(Path(path), dumps(data))


def dumps(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def loads(text: str, default: Any = None) -> Any:
    data = yaml.safe_load(text) if text else None
    return data if data is not None else default
