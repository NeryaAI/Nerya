"""Evidence document schema.

Canonical shape lives in :class:`EvidenceDoc`. Persistence uses Markdown +
JSONL so the artifacts stay human-inspectable in the workspace tree.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


VALID_SOURCE_TYPES: frozenset[str] = frozenset({
    "backtest", "trade", "risk", "account", "gateway", "research",
    "memory", "model", "strategy",
})


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_path_segment() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Provenance:
    route: str = ""
    strategy_id: str = ""
    session_id: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    created_by: str = "runtime"  # "agent" | "operator" | "runtime"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SecurityInfo:
    contains_secret: bool = False
    redaction_applied: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceDoc:
    evidence_id: str
    source_type: str
    source_id: str
    title: str
    summary: str = ""
    workspace_path: str = ""
    provenance: Provenance = field(default_factory=Provenance)
    security: SecurityInfo = field(default_factory=SecurityInfo)
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    body: str = ""
    scope: str = "shared"  # "shared" | "strategy" | "session"
    strategy_id: Optional[str] = None
    session_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.source_type not in VALID_SOURCE_TYPES:
            raise ValueError(
                f"invalid source_type={self.source_type!r}; "
                f"expected one of {sorted(VALID_SOURCE_TYPES)}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "title": self.title,
            "summary": self.summary,
            "workspace_path": self.workspace_path,
            "provenance": self.provenance.as_dict(),
            "security": self.security.as_dict(),
            "tags": list(self.tags),
            "created_at": self.created_at,
            "scope": self.scope,
            "strategy_id": self.strategy_id,
            "session_id": self.session_id,
        }
