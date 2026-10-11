"""Immutable collection evidence: unavailable is never a zero position."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import time
from typing import Literal

from .instruments import number
from ..core.errors import TradingError


@dataclass(frozen=True)
class CollectionEvidence:
    name: str
    status: Literal["ok", "unknown", "unsupported", "error", "stale"]
    as_of: float | None
    source: str
    coverage: tuple[str, ...] = ()
    error: str | None = None

    def __post_init__(self):
        if self.status not in {"ok", "unknown", "unsupported", "error", "stale"}:
            raise TradingError("invalid_collection_evidence")
        if self.status == "ok" and (not self.source or self.as_of is None or number(self.as_of) <= 0):
            raise TradingError("collection_provenance_required")

    def fresh(self, max_age: float, *, now: float | None = None) -> bool:
        instant = time.time() if now is None else now
        return self.status == "ok" and self.as_of is not None and 0 <= instant - self.as_of <= max_age


@dataclass(frozen=True)
class AssetBalance:
    asset: str
    free: Decimal
    locked: Decimal
    total: Decimal

    def __post_init__(self):
        if not self.asset:
            raise TradingError("asset_identity_required")
        for key in ("free", "locked", "total"):
            object.__setattr__(self, key, number(getattr(self, key)))


@dataclass(frozen=True)
class AccountState:
    account_id: str
    source: str
    as_of: float
    nav_usd: Decimal
    balances: tuple[AssetBalance, ...]
    collections: tuple[CollectionEvidence, ...]
    positions: tuple[dict, ...] = ()
    open_orders: tuple[dict, ...] = ()

    @classmethod
    def from_snapshot(cls, snap):
        entries = snap.meta.get("collections") or {}
        evidence = tuple(CollectionEvidence(name, entry.get("status", "unknown"), entry.get("as_of"),
                                           entry.get("source", ""), tuple(entry.get("coverage") or ()), entry.get("error"))
                         for name, entry in entries.items())
        assets = set(snap.cash_by_asset) | set(snap.free_by_asset) | set(snap.locked_by_asset)
        balances = tuple(AssetBalance(asset, snap.free_by_asset.get(asset, 0), snap.locked_by_asset.get(asset, 0),
                                      snap.cash_by_asset.get(asset, 0)) for asset in sorted(assets))
        return cls(snap.account_id, snap.source, snap.ts, number(snap.nav_usd), balances, evidence,
                   tuple(snap.meta.get("positions") or ()), tuple(snap.meta.get("open_orders") or ()))

    def require(self, names: tuple[str, ...], *, max_age: float, now: float | None = None):
        by_name = {item.name: item for item in self.collections}
        for name in names:
            item = by_name.get(name)
            if item is None or not item.fresh(max_age, now=now):
                raise TradingError(f"account_collection_unavailable:{name}")

    def asdict(self):
        data = asdict(self)
        data["nav_usd"] = str(self.nav_usd)
        data["balances"] = [{key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(row).items()}
                            for row in self.balances]
        return data
