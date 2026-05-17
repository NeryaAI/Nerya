"""Unified data source sync state.

Tracks per-source freshness, last success,
next due, error, and budget so the operator overview can answer
"is the data Nerya is using fresh enough to trust?"
"""

from __future__ import annotations

__all__: list[str] = []
