"""Permission checks. Matches a declared permission string ('trading.submit')
against a caller's allow-list."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.errors import SkillPermissionError


@dataclass(frozen=True)
class PermissionSet:
    granted: frozenset[str]

    @classmethod
    def from_list(cls, perms: list[str] | None) -> "PermissionSet":
        return cls(granted=frozenset(perms or []))

    def has(self, perm: str) -> bool:
        if perm in self.granted:
            return True
        # `trading.*` matches `trading.submit`, etc.
        parts = perm.split(".")
        for i in range(len(parts), 0, -1):
            wildcard = ".".join(parts[:i - 1] + ["*"])
            if wildcard in self.granted:
                return True
        return "*" in self.granted

    def require(self, perm: str) -> None:
        if not self.has(perm):
            raise SkillPermissionError(f"missing permission: {perm}")


def check(caller: PermissionSet, *required: str) -> None:
    for p in required:
        caller.require(p)
