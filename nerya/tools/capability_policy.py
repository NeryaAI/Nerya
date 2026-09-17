"""Generic tool visibility/execution policy. No strategy-generation decisions."""
from __future__ import annotations
from fnmatch import fnmatchcase
from typing import Any


def normalise_tool_policy(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"allow_groups": [], "deny": []}
    if not isinstance(raw, dict):
        raise ValueError("tool_policy must be an object")
    def names(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple)) or any(not isinstance(x, str) or not x.strip() for x in value):
            raise ValueError("tool policy selectors must be non-empty strings")
        return list(dict.fromkeys(x.strip() for x in value))
    groups = raw.get("allow_groups", [])
    if not isinstance(groups, list):
        raise ValueError("tool_policy.allow_groups must be a list")
    allowed = [names(group) for group in groups]
    if raw.get("allow") is not None:
        allowed.append(names(raw["allow"]))
    return {"allow_groups": allowed, "deny": names(raw.get("deny", []))}


def tool_policy_allows(policy: dict[str, Any], name: str) -> bool:
    return (not any(fnmatchcase(name, pattern) for pattern in policy.get("deny", []))
            and all(any(fnmatchcase(name, pattern) for pattern in group)
                    for group in policy.get("allow_groups", [])))
