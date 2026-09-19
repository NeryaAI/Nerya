"""Machine-facing catalog CLI, independent of MCP installation/server exposure."""
from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from .._common import _add_ws
from ...mcp.catalog import build_catalog, failed, is_error
from ...mcp.tools import NeryaTools


MAX_INPUT_BYTES = 1_048_576


def _arguments(args):
    if getattr(args, "input_file", None):
        if args.input_file == "-":
            text = sys.stdin.read(MAX_INPUT_BYTES + 1)
        else:
            with Path(args.input_file).open(encoding="utf-8") as stream:
                text = stream.read(MAX_INPUT_BYTES + 1)
    else:
        text = getattr(args, "input_json", None)
        if text is None:
            text = "{}"
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("input too large")
    def reject_constant(value):
        raise ValueError("Non-finite JSON numbers are not supported")

    value = json.loads(text, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("input must be an object")
    return value


def cmd_tools(args) -> int:
    try:
        arguments = _arguments(args) if args.tool_action == "call" else None
        if getattr(args, "role_name", None) is not None:
            arguments["name"] = args.role_name
        if getattr(args, "config_target", None) is not None:
            arguments["target"] = args.config_target
    except (OSError, ValueError, UnicodeError, RecursionError):
        print(json.dumps(failed("invalid_arguments", "Supply a JSON object up to 1 MiB via --input or --input-file")))
        return 2
    try:
        with redirect_stdout(sys.stderr):
            tools = NeryaTools.boot(args.workspace, profile=getattr(args, "profile", None))
            catalog = build_catalog(tools)
            if args.tool_action == "list":
                result = {"tools": catalog.list_tools()}
            elif args.tool_action == "describe":
                result = catalog.describe(args.tool_name)
            else:
                result = catalog.call(args.tool_name, arguments)
    except Exception:
        result = failed("initialization_failed", "Tool catalog initialization failed; check workspace configuration")
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 1 if is_error(result) else 0


def add_call_arguments(parser) -> None:
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--input", dest="input_json", help="Tool arguments as one JSON object")
    inputs.add_argument("--input-file", help="Read UTF-8 JSON from a file; '-' reads stdin")


def register(sub) -> None:
    tools = sub.add_parser("tools", help="Discover and call the same tools as MCP (JSON output)")
    actions = tools.add_subparsers(dest="tool_action", required=True)
    for action in ("list", "describe", "call"):
        parser = actions.add_parser(action)
        _add_ws(parser)
        if action != "list":
            parser.add_argument("tool_name")
        if action == "call":
            add_call_arguments(parser)
        parser.set_defaults(func=cmd_tools)

    agents = sub.add_parser("agent", help="Manage persistent agent roles using the shared tool policy")
    agent_actions = agents.add_subparsers(dest="agent_action", required=True)
    for action, tool in (("list", "role_list"), ("show", "role_get"),
                         ("save", "role_save"), ("delete", "role_delete")):
        parser = agent_actions.add_parser(action)
        _add_ws(parser)
        if action in {"show", "delete"}:
            parser.add_argument("role_name")
        if action == "save":
            add_call_arguments(parser)
        parser.set_defaults(func=cmd_tools, tool_action="call", tool_name="nerya_native_" + tool)

    configs = sub.add_parser("config", help="Read configuration or propose a reviewed change")
    config_actions = configs.add_subparsers(dest="config_action", required=True)
    parser = config_actions.add_parser("get")
    _add_ws(parser)
    parser.add_argument("config_target", nargs="?", default="nerya.yml")
    parser.set_defaults(func=cmd_tools, tool_action="call", tool_name="nerya_config_get")
    parser = config_actions.add_parser("propose")
    _add_ws(parser)
    add_call_arguments(parser)
    parser.set_defaults(func=cmd_tools, tool_action="call", tool_name="nerya_native_evolve_core_config_patch")
