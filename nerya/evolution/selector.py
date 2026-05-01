"""Select reusable evolution assets for current signals."""

from __future__ import annotations

from typing import Any

from ..core.paths import WorkspacePaths
from .assets import list_capsules, list_genes


def select_assets_for_signals(
    paths: WorkspacePaths,
    signals: list[dict[str, Any]],
    *,
    strategy_id: str | None = None,
    limit: int = 8,
) -> dict[str, list[dict[str, Any]]]:
    kinds = {str(s.get("kind") or "") for s in signals}
    genes: list[dict[str, Any]] = []
    for gene in list_genes(paths):
        matches = set(str(x) for x in (gene.get("signals_match") or []))
        if kinds & matches:
            genes.append(gene)
    genes.sort(key=lambda g: float(g.get("confidence") or 0.0), reverse=True)
    capsules = list_capsules(paths, strategy_id=strategy_id, limit=limit)
    return {"genes": genes[:limit], "capsules": capsules[:limit]}


__all__ = ["select_assets_for_signals"]
