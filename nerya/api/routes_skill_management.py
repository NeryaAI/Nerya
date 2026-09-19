"""Scoped Skill catalog and proposal management shared with MCP and CLI."""
from ..skills import management
from ..mcp.catalog import public_result


def _handler(fn):
    def handle(client, payload):
        try:
            # HTTP query values are strings; preserve the same bounded public contract.
            args = dict(payload or {})
            for key in ("offset", "limit"):
                if key in args:
                    args[key] = int(args[key])
            if "include_unassigned" in args:
                args["include_unassigned"] = str(args["include_unassigned"]).lower() == "true"
            return public_result(fn(client.config, **args))
        except (ValueError, TypeError, OSError):
            return {"ok": False, "error": "Invalid Skill request, missing file or stale revision; refresh and retry", "_status": 400}
    return handle


def routes():
    return [("GET", "/skills/catalog", _handler(management.catalog)),
            ("GET", "/skills/read", _handler(management.read)),
            ("POST", "/skills/manage", _handler(management.manage))]
