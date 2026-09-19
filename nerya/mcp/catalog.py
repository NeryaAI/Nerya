"""One discover/describe/call contract for CLI and MCP; no MCP dependency.

Business actions stay in NeryaTools, SkillRuntime and NativeToolExecutor.
Exposure filtering is applied before dispatch, not just to tools/list.
"""
from __future__ import annotations

import inspect
import json
import re
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, get_type_hints

from jsonschema import Draft202012Validator, ValidationError
from pydantic import ConfigDict, create_model

from ..core.redaction import redact_text
from .dynamic_tools import DynamicMCPRegistry, policy_from_config
from .registry_bridge import build_native_mcp_registry
from .registry_bridge import policy_from_config as native_policy_from_config
from .tools import NeryaTools


LEGACY_WRITES = {"nerya_trigger_emit", "nerya_strategy_generate", "nerya_skill_manage"}
_SECRET_KEYS = {"token", "secret", "password", "passphrase", "api_key", "apikey",
                "private_key", "authorization", "cookie", "set_cookie", "auth_header", "webhook_url", "admin_password_hash", "jwt_secret", "session_secret"}
_TEXT_SECRET = re.compile(
    r"(?i)(\b(?:api[_-]?key|(?:bot_|access_|refresh_)?token|secret|password|passphrase|private_key|webhook_url)"
    r"\b[\"']?\s*[:=]\s*[\"']?)[^\s,\"'\}\]]+"
)


def public_result(value: Any, key: str = "") -> Any:
    """Withhold credentials/tracebacks without corrupting ids or revision hashes."""
    normalized = key.lower().replace("-", "_")
    if normalized in _SECRET_KEYS or any(
        normalized.endswith("_" + name) for name in _SECRET_KEYS
    ):
        return "***REDACTED***"
    if isinstance(value, dict):
        return {str(k): public_result(v, str(k)) for k, v in value.items()
                if k not in {"trace", "traceback"}}
    if isinstance(value, (list, tuple)):
        return [public_result(v) for v in value]
    if isinstance(value, str):
        # Native envelopes can contain a JSON text copy of their structured data.
        if value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(public_result(json.loads(value)), ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        if normalized in {"id", "digest", "sha256"} or normalized.endswith(
            ("_id", "_hash", "_sha256", "_digest")
        ):
            return value
        value = _TEXT_SECRET.sub(r"\1***REDACTED***", value)
        value = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer ***REDACTED***", value)
        return redact_text(value)
    return value


def failed(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def is_error(result: dict[str, Any]) -> bool:
    return bool(result.get("error")) or result.get("ok") is False or (
        result.get("validation_ok") is False
    ) or (isinstance(result.get("status"), str) and
          result["status"] in {"error", "failed", "blocked", "denied"})


def callable_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    hints = get_type_hints(fn)
    fields = {}
    for name, param in inspect.signature(fn).parameters.items():
        if param.kind in {param.VAR_POSITIONAL, param.VAR_KEYWORD}:
            raise ValueError(f"{fn.__name__} must have an explicit input schema")
        fields[name] = (hints.get(name, Any),
                        ... if param.default is param.empty else param.default)
    return create_model("Arguments", __config__=ConfigDict(extra="forbid"),
                        **fields).model_json_schema()


@dataclass
class ExposedTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    fn: Callable[..., Any] = field(repr=False)
    source: str = "legacy"

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name, "description": self.description,
            "inputSchema": deepcopy(self.input_schema),
            "outputSchema": {"type": "object", "additionalProperties": True},
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": not self.read_only,
                "idempotentHint": self.read_only,
                "openWorldHint": True,
            },
        }


@dataclass
class ToolCatalog:
    tools: dict[str, ExposedTool] = field(default_factory=dict)
    # Native executor state and workspace writes are not concurrency-safe.
    _lock: Any = field(default_factory=threading.RLock, repr=False)

    def add(self, tool: ExposedTool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"Duplicate public tool name: {tool.name}")
        Draft202012Validator.check_schema(tool.input_schema)
        self.tools[tool.name] = tool

    def list_tools(self) -> list[dict[str, Any]]:
        return [self.tools[name].descriptor() for name in sorted(self.tools)]

    def describe(self, name: str) -> dict[str, Any]:
        tool = self.tools.get(name)
        return tool.descriptor() if tool else failed("not_found", "Tool is not exposed")

    def call(self, name: str, arguments: Any = None) -> dict[str, Any]:
        tool = self.tools.get(name)
        if tool is None:
            return failed("not_found", "Tool is not exposed; use tools list")
        payload = {} if arguments is None else arguments
        if not isinstance(payload, dict):
            return failed("invalid_arguments", "Arguments must be a JSON object")
        try:
            if len(json.dumps(payload, allow_nan=False).encode()) > 1_048_576:
                return failed("invalid_arguments", "Arguments exceed 1 MiB")
            Draft202012Validator(tool.input_schema).validate(payload)
        except ValidationError as exc:
            # Validation messages may echo user-supplied secrets. Report location only.
            location = ".".join(str(p) for p in exc.absolute_path) or "$"
            return failed("invalid_arguments", f"{location}: {exc.validator} validation failed")
        except (ValueError, TypeError, RecursionError):
            return failed("invalid_arguments", "Arguments must contain finite JSON values")
        try:
            with self._lock:
                result = tool.fn(**payload)
            result = result if isinstance(result, dict) else {"data": result}
            return public_result(result)
        except Exception:
            return failed("internal", "Tool execution failed; inspect local runtime diagnostics")


def validate_exposure_config(config) -> None:
    """Reject truthy strings and malformed allowlists rather than widening access."""
    data = config.get("mcp", {})
    if not isinstance(data, dict):
        raise ValueError("mcp must be a mapping")
    for block, flags, lists in (
        (data, ("enabled", "allow_mutating", "include_legacy"), ("allow_tools", "deny_tools")),
        (data.get("native_tools", {}), ("enabled", "allow_mutating", "allow_exec", "inherit_live_trading"),
         ("allow_tools", "deny_tools")),
        (data.get("dynamic_tools", {}), ("enabled", "allow_mutating", "include_unimplemented"),
         ("allow_skills", "deny_skills", "allow_actions", "deny_actions")),
    ):
        if not isinstance(block, dict):
            raise ValueError("MCP tool settings must be mappings")
        for key in flags:
            if key in block and type(block[key]) is not bool:
                raise ValueError(f"MCP {key} must be a boolean")
        for key in lists:
            value = block.get(key)
            if value is not None and (not isinstance(value, list) or
                                      any(not isinstance(v, str) for v in value)):
                raise ValueError(f"MCP {key} must be a list of tool names or null")


def build_catalog(tools: NeryaTools, *, include_legacy: bool | None = None,
                  include_dynamic: bool | None = None, include_native: bool | None = None,
                  dynamic_policy=None, native_policy=None) -> ToolCatalog:
    cfg = tools.client.config
    validate_exposure_config(cfg)
    catalog = ToolCatalog()
    if include_legacy if include_legacy is not None else cfg.get("mcp.include_legacy", True):
        for entry in tools.registry():
            read_only = entry["name"] not in LEGACY_WRITES
            if not read_only and cfg.get("mcp.allow_mutating", False) is not True:
                continue
            catalog.add(ExposedTool(entry["name"], entry["description"],
                                    callable_schema(entry["fn"]), read_only, entry["fn"]))
    if include_dynamic if include_dynamic is not None else cfg.get("mcp.dynamic_tools.enabled", True):
        view = DynamicMCPRegistry.build(tools.client,
                                       policy=dynamic_policy or policy_from_config(cfg))
        for tool in view.tools:
            catalog.add(ExposedTool(tool.name, tool.description, tool.input_schema,
                                    tool.read_only, tool.fn, "dynamic"))
    if include_native if include_native is not None else cfg.get("mcp.native_tools.enabled", True):
        from ..tools import ToolRegistry
        from ..tools.native import build_native_tool_deps, register_native_tools
        from .. import skills as skills_package

        registry = ToolRegistry()
        roots = [cfg.paths.skills_installed, Path(skills_package.__file__).parent / "builtin"]
        deps = build_native_tool_deps(workspace_root=cfg.paths.root,
                                     skill_roots=[p for p in roots if p.exists()],
                                     paths=cfg.paths, config=cfg, skills=tools.client.skills)
        register_native_tools(registry, deps)
        view, executor = build_native_mcp_registry(
            registry=registry, config=cfg,
            policy=native_policy or native_policy_from_config(cfg),
        )
        # Preserve delegation through the same parent's permission pipeline.
        deps.executor = executor
        for tool in view.tools:
            schema = deepcopy(tool.input_schema)
            schema.setdefault("additionalProperties", False)
            catalog.add(ExposedTool(tool.name, tool.description, schema,
                                    tool.read_only, tool.fn, "native"))
    allowed = cfg.get("mcp.allow_tools")
    denied = set(cfg.get("mcp.deny_tools", []) or [])
    catalog.tools = {name: tool for name, tool in catalog.tools.items()
                     if (allowed is None or name in allowed) and name not in denied}
    return catalog
