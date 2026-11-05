"""Goal controls are narrow host contracts, not model-authored booleans."""
from __future__ import annotations

from ...agent.command_store import CommandStore, CommandError
from ...agent.goals import active_context, model_update
from ..types import ToolResult, ToolError, ToolErrorKind, RiskLevel, PermissionScope


def register_goal_tools(registry, deps, *, replace=False):
    from .bootstrap import make_native_descriptor
    if deps.config is None or active_context(deps.config) is None:
        return

    def handler(call):
        try:
            if call.name == "get_goal":
                goal=active_context(deps.config)
            else:
                goal=model_update(CommandStore(deps.paths,"goal-tool"),deps.active_session_id,
                    str(deps.config.get("runtime.command_id")),status=call.arguments.get("status"),
                    reason=str(call.arguments.get("reason") or ""))
            return ToolResult.from_json(tool_use_id=call.id,name=call.name,data={"ok":True,"goal":goal})
        except CommandError as exc:
            return ToolResult.from_error(tool_use_id=call.id,name=call.name,
                error=ToolError(kind=ToolErrorKind.CONFLICT,message=exc.code,retryable=False))

    for name, schema, description in (
        ("get_goal",{"type":"object","properties":{},"additionalProperties":False},
         "Read this conversation goal, lifecycle and cumulative usage. Turn completion is not goal completion."),
        ("update_goal",{"type":"object","properties":{"status":{"type":"string","enum":["complete","blocked"]},
            "reason":{"type":"string","maxLength":1000}},"required":["status"],"additionalProperties":False},
         "Request goal completion only after verifying every requested outcome, or report a concrete blocker. A completion request is pending until the host checks the terminal turn and delivery receipts. Repeated unchanged blockers stop continuation. User controls own pause/resume; this tool grants no new authority."),
    ):
        registry.register(make_native_descriptor(name=name,description=description,input_schema=schema,handler=handler,
            read_only=name=="get_goal",risk=RiskLevel.READ if name=="get_goal" else RiskLevel.WRITE,
            permission_scope=PermissionScope.WORKSPACE,auto_approve=True,tags=("goal","task")),replace=replace)
