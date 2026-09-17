"""Strategy-owned execution policy, not a strategy generator or another loop."""
from __future__ import annotations
from copy import deepcopy
import math
from typing import Any
from ..core.config import Config
from ..tools.capability_policy import normalise_tool_policy

BUDGET_KEYS = {"max_iterations": "max_iterations", "max_tool_calls": "max_total_tool_calls", "max_wall_seconds": "max_wall_seconds"}


def validate_agent_configuration(raw: dict[str, Any]) -> None:
    execution = raw.get("agent_execution", {})
    context = raw.get("agent_context", {})
    if not isinstance(execution, dict) or not isinstance(context, dict):
        raise ValueError("agent_execution and agent_context must be mappings")
    for key in BUDGET_KEYS:
        value = execution.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"agent_execution.{key} must be a positive finite number or null (inherit)")
        if key != "max_wall_seconds" and not isinstance(value, int):
            raise ValueError(f"agent_execution.{key} must be an integer")
    if execution.get("tier") not in {None, "light", "medium", "high"}:
        raise ValueError("agent_execution.tier must be light, medium, high or null (inherit)")
    mode = execution.get("capabilities", "custom" if raw.get("agent_profile", {}).get("allowed_tools") else "inherit")
    if mode not in {"inherit", "custom"}:
        raise ValueError("agent_execution.capabilities must be inherit or custom")
    if mode == "inherit" and raw.get("agent_profile", {}).get("allowed_tools"):
        raise ValueError("To inherit capabilities, explicitly remove the custom allowed_tools list")
    normalise_tool_policy({"allow": raw.get("agent_profile", {}).get("allowed_tools") if mode == "custom" else None,
                           "deny": execution.get("denied_tools", [])})
    team = execution.get("team", {})
    if not isinstance(team, dict):
        raise ValueError("agent_execution.team must be a mapping")
    if "enabled" in team and not isinstance(team["enabled"], bool):
        raise ValueError("team.enabled must be boolean")
    roles = team.get("roles", [])
    if not isinstance(roles, list) or any(not isinstance(r, str) or not r.strip() for r in roles) or len(roles) != len(set(roles)):
        raise ValueError("team.roles must be unique role identifiers")
    if any(r not in raw.get("subagents", []) for r in roles):
        raise ValueError("team.roles must reference declared subagents")
    if team.get("enabled") and not (roles if "roles" in team else raw.get("subagents")):
        raise ValueError("An enabled parallel team needs at least one declared role")
    n = team.get("max_parallel")
    if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 1):
        raise ValueError("team.max_parallel must be a positive integer or null")
    overrides = team.get("role_policies", {})
    if not isinstance(overrides, dict) or any(name not in raw.get("subagents", []) for name in overrides):
        raise ValueError("team.role_policies must refer to declared subagents")
    from ..subagents.registry import SubAgentExecutionPolicy
    for policy in overrides.values():
        if not isinstance(policy, dict):
            raise ValueError("role execution policy must be an object")
        for key in ("max_iterations", "max_skill_calls", "max_wall_seconds"):
            number = policy.get(key)
            if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0 or key != "max_wall_seconds" and not isinstance(number, int)):
                raise ValueError(f"role policy {key} must be positive or null (inherit)")
        SubAgentExecutionPolicy.from_dict(policy)
    sources = context.get("sources", [])
    if not isinstance(sources, list) or any(not isinstance(s, str) for s in sources) or len(sources) != len(set(sources)):
        raise ValueError("agent_context.sources must be unique data source IDs")
    from .input_context import configured_sources
    entries = configured_sources(raw.get("data_sources"))
    from .source_config import validate_source
    for entry in entries:
        validate_source(entry)
    available = {str(source.get("id")) for source in entries if isinstance(source, dict)}
    if any(s not in available for s in sources):
        raise ValueError("agent_context.sources references an unknown data source")
    for key in ("include_script_outputs", "include_trigger"):
        if key in context and not isinstance(context[key], bool):
            raise ValueError(f"agent_context.{key} must be boolean")
    if context.get("on_error", "stop") not in {"stop", "continue"}:
        raise ValueError("agent_context.on_error must be stop or continue")
    cap = context.get("max_chars", 64000)
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1000 <= cap <= 1000000:
        raise ValueError("agent_context.max_chars must be between 1000 and 1000000")


def execution_config(config: Config, manifest: Any) -> Config:
    """Private config copy. Never mutate Workspace settings or another run."""
    raw = {**manifest.extras, "agent_profile": manifest.agent_profile.asdict(), "subagents": list(manifest.subagents)}
    validate_agent_configuration(raw)
    execution = raw.get("agent_execution", {})
    data = deepcopy(config.data)
    native = data.setdefault("agent", {}).setdefault("native", {})
    if execution.get("tier"):
        native["tier"] = execution["tier"]
    for key, target in BUDGET_KEYS.items():
        if execution.get(key) is not None:
            native[target] = execution[key]
    # Strategy-owned child loops inherit the main run's capacity unless an
    # operator explicitly supplied a child policy or a role has its own cap.
    child = data["agent"].setdefault("subagents", {})
    for target, source in (("max_iterations", "max_iterations"), ("max_skill_calls", "max_total_tool_calls"), ("max_wall_seconds", "max_wall_seconds")):
        if target not in child and source in native:
            child[target] = native[source]
    team = data["agent"].setdefault("team_run", {})
    wall = native.get("max_wall_seconds")
    if wall is not None:
        team.setdefault("timeout_s", wall)
        team.setdefault("max_timeout_s", wall)
    requested_parallel = execution.get("team", {}).get("max_parallel")
    if requested_parallel is not None:
        team.setdefault("max_parallel", requested_parallel)
    allowed = list(manifest.agent_profile.allowed_tools)
    mode = execution.get("capabilities", "custom" if allowed else "inherit")
    policy = normalise_tool_policy(native.get("tool_policy"))
    if mode == "custom":
        policy["allow_groups"].append(allowed)
    policy["deny"] = list(dict.fromkeys([*policy["deny"], *execution.get("denied_tools", [])]))
    native["tool_policy"] = policy
    return Config(paths=config.paths, data=data)
