"""``nerya anet`` subcommands.

Subcommands:

* ``nerya anet doctor``  — config + connectivity self-check.
* ``nerya anet status``  — print the current integration snapshot.
* ``nerya anet enable``  — flip ``integrations.anet.enabled`` on.
* ``nerya anet disable`` — flip it off (and clear outbound.skill_enabled).
* ``nerya anet register`` — run the register loop in the foreground.

The CLI module intentionally avoids importing the optional ``anet``
SDK at module load. Heavy work happens inside command functions so a
user who has not installed ``nerya[anet]`` can still run
``nerya anet doctor`` and get a clear install hint.
"""

from __future__ import annotations

from typing import Any

from .._common import _add_ws, _client, _print
from ...core import yaml_io


def cmd_anet_status(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    cfg = client.config
    block = (cfg.data.get("integrations") or {}).get("anet") or {}
    _print({
        "enabled": cfg.integration_enabled("anet"),
        "daemon_url": block.get("daemon_url") or "",
        "service_name": block.get("service_name") or "",
        "token_ref": block.get("token_ref") or "",
        "tags": list(block.get("tags") or []),
        "modes": list(block.get("modes") or []),
        "outbound": dict(block.get("outbound") or {}),
        "cost_model": dict(block.get("cost_model") or {}),
        "heartbeat_seconds": int(block.get("heartbeat_seconds") or 60),
    })
    return 0


def cmd_anet_doctor(args) -> int:
    # Local import so the CLI stays responsive even if the integration
    # package has a bug — doctor is precisely the tool you'd reach for
    # to diagnose that.
    from ...integrations.anet import doctor
    client = _client(args.workspace, getattr(args, "profile", None))
    report = doctor.run_doctor(client.config)
    _print(report)
    return 0 if report.get("ready") else 1


def _write_anet_patch(conf_path, patch: dict[str, Any]) -> None:
    existing = yaml_io.load(conf_path, default={}) or {}
    integrations = existing.get("integrations") or {}
    anet = integrations.get("anet") or {}

    def merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = v

    merge(anet, patch)
    integrations["anet"] = anet
    existing["integrations"] = integrations
    yaml_io.dump(conf_path, existing)


def cmd_anet_enable(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    patch: dict[str, Any] = {"enabled": True}
    if args.token_ref:
        patch["token_ref"] = args.token_ref
    if args.service_name:
        patch["service_name"] = args.service_name
    if args.outbound:
        patch["outbound"] = {"skill_enabled": True}
    _write_anet_patch(client.config.paths.config, patch)
    _print({"ok": True, "enabled": True,
            "outbound_skill_enabled": bool(args.outbound),
            "config": str(client.config.paths.config)})
    return 0


def cmd_anet_disable(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    _write_anet_patch(client.config.paths.config, {
        "enabled": False,
        "outbound": {"skill_enabled": False},
    })
    _print({"ok": True, "enabled": False,
            "config": str(client.config.paths.config)})
    return 0


def cmd_anet_register(args) -> int:
    # Foreground register loop. Exits 0 when disabled so composed
    # service units don't flap.
    from ...integrations.anet import service
    client = _client(args.workspace, getattr(args, "profile", None))
    return service.main(client.config)


def register(sub) -> None:
    anet = sub.add_parser("anet",
                          help="AgentNetwork P2P integration (opt-in)"
                          ).add_subparsers(dest="acmd", required=True)

    p = anet.add_parser("status", help="Show integration status")
    _add_ws(p)
    p.set_defaults(func=cmd_anet_status)

    p = anet.add_parser("doctor", help="Run config + connectivity self-check")
    _add_ws(p)
    p.set_defaults(func=cmd_anet_doctor)

    p = anet.add_parser("enable", help="Turn the integration on in workspace yaml")
    p.add_argument("--token-ref", default=None,
                   help="secret: ref (e.g. secret:anet/api_token) "
                        "or raw token for local dev")
    p.add_argument("--service-name", default=None,
                   help="Override integrations.anet.service_name")
    p.add_argument("--outbound", action="store_true",
                   help="Also enable outbound (agent-visible anet skill)")
    _add_ws(p)
    p.set_defaults(func=cmd_anet_enable)

    p = anet.add_parser("disable",
                        help="Turn the integration off in workspace yaml")
    _add_ws(p)
    p.set_defaults(func=cmd_anet_disable)

    p = anet.add_parser("register",
                        help="Run the register loop (foreground; Ctrl-C to unregister)")
    _add_ws(p)
    p.set_defaults(func=cmd_anet_register)


__all__ = [
    "cmd_anet_status", "cmd_anet_doctor",
    "cmd_anet_enable", "cmd_anet_disable", "cmd_anet_register",
    "register",
]
