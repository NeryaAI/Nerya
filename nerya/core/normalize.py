"""Small string-normalisation helpers shared across subsystems."""

from __future__ import annotations


def norm_key(s: str) -> str:
    """Normalise an identifier-style key: trim, lowercase, dashes to underscores."""
    return s.strip().lower().replace("-", "_")


__all__ = ["norm_key"]
