"""Prompt formatting helpers exposed as ``ctx.prompt``."""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable

from ..core.atomic_write import atomic_write_text
from ..core.time import now_iso


def _row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if is_dataclass(row):
        return dict(asdict(row))
    if hasattr(row, "_asdict"):
        return dict(row._asdict())
    if hasattr(row, "__dict__"):
        return dict(vars(row))
    return {"value": row}


def _safe_name(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "artifact")).strip("._")
    return base or "artifact"


class StrategyPromptIO:
    def __init__(self, *, strategy_root: Path, run_id: str) -> None:
        self.strategy_root = Path(strategy_root)
        self.run_id = str(run_id or "run")

    def csv(
        self,
        rows: Iterable[Any],
        *,
        columns: list[str] | tuple[str, ...] | None = None,
        max_rows: int | None = None,
    ) -> str:
        data = [_row_to_dict(r) for r in rows]
        if max_rows is not None and max_rows >= 0:
            data = data[-int(max_rows):]
        if columns is None:
            seen: list[str] = []
            for row in data:
                for key in row:
                    if key not in seen:
                        seen.append(str(key))
            columns = seen
        cols = [str(c) for c in (columns or [])]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in data:
            writer.writerow({c: row.get(c, "") for c in cols})
        return buf.getvalue().strip()

    def markdown_table(
        self,
        rows: Iterable[Any],
        *,
        columns: list[str] | tuple[str, ...] | None = None,
        max_rows: int | None = None,
    ) -> str:
        data = [_row_to_dict(r) for r in rows]
        if max_rows is not None and max_rows >= 0:
            data = data[-int(max_rows):]
        if columns is None:
            columns = list(data[0].keys()) if data else []
        cols = [str(c) for c in (columns or [])]
        lines = [
            "| " + " | ".join(cols) + " |",
            "| " + " | ".join("---" for _ in cols) + " |",
        ]
        for row in data:
            vals = [str(row.get(c, "")).replace("|", "\\|") for c in cols]
            lines.append("| " + " | ".join(vals) + " |")
        return "\n".join(lines)

    def json_block(self, obj: Any) -> str:
        return "```json\n" + json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n```"

    def truncate_csv(
        self,
        rows: Iterable[Any],
        *,
        columns: list[str] | tuple[str, ...] | None = None,
        max_rows: int = 120,
    ) -> str:
        return self.csv(rows, columns=columns, max_rows=max_rows)

    def artifact(
        self,
        name: str,
        content: str,
        *,
        content_type: str = "text/plain",
    ) -> dict[str, Any]:
        rel = Path("agent_tasks") / self.run_id / _safe_name(name)
        path = self.strategy_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, str(content))
        return {
            "name": name,
            "path": str(rel).replace("\\", "/"),
            "content_type": content_type,
            "chars": len(str(content)),
            "created_at": now_iso(),
        }


__all__ = ["StrategyPromptIO"]
