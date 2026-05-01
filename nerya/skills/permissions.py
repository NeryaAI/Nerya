"""Per-skill permission caches."""

from __future__ import annotations

from ..security.permissions import PermissionSet


def manifest_permissions(perms: list[str]) -> PermissionSet:
    return PermissionSet.from_list(perms)
