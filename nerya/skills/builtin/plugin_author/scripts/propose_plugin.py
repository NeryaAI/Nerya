"""Stage a workspace-plugin proposal.

Proposal-first, like every executable-code lane in Nerya: this script
never installs anything. It statically validates the drafted
``plugin.py`` (syntax + plugin shape, **without executing it**), then
stages a ``plugin_proposal`` under
``evolution/proposals/<pid>/after/plugins/<id>/plugin.py`` in state
``pending_review``. Promotion (operator approval → ``apply_proposal``)
lands the file in ``plugins/<id>/`` where the kernel loads it at next
boot.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .....core.config import load_config
from .....evolution.patch_proposal import create_proposal
from .....skills.manifest import cli_main

_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

# Module-level imports that execute heavy/external machinery at boot
# time. Importing them *inside* setup()/handlers is fine; at module
# level they run at kernel boot before any operator sees the plugin.
_MODULE_LEVEL_IMPORT_DENYLIST = {
    "subprocess",
    "socket",
    "socketserver",
    "http.client",
    "urllib.request",
    "urllib2",
    "requests",
    "httpx",
    "aiohttp",
}

_MODULE_LEVEL_CALL_DENYLIST = {
    "exec",
    "eval",
    "compile",
    "__import__",
    "system",
    "popen",
    "run",
}


def _static_check(code: str) -> list[str]:
    """Validate plugin code shape without executing a single line of it."""

    errors: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"syntax error: line {exc.lineno}: {exc.msg}"]

    def _targets(node: ast.stmt) -> list[str]:
        names: list[str] = []
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.target, ast.Name
        ):
            names.append(node.target.id)
        return names

    has_plugin = any(
        "PLUGIN" in _targets(node) for node in tree.body
    )
    has_setup = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "setup"
        for node in tree.body
    )
    if not (has_plugin or has_setup):
        errors.append(
            "module must expose PLUGIN = <Plugin instance> or a "
            "module-level setup(ctx) function"
        )

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if alias.name in _MODULE_LEVEL_IMPORT_DENYLIST or root in {
                    "subprocess", "socket", "requests", "httpx", "aiohttp",
                }:
                    errors.append(
                        f"module-level import {alias.name!r} is not allowed "
                        "(move it inside setup()/handlers)"
                    )
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module in _MODULE_LEVEL_IMPORT_DENYLIST or node.module.split(".")[0] in {
                "subprocess", "socket", "requests", "httpx", "aiohttp",
            }:
                errors.append(
                    f"module-level import from {node.module!r} is not allowed "
                    "(move it inside setup()/handlers)"
                )
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            fn = node.value.func
            name = getattr(fn, "id", "") or ""
            if name in _MODULE_LEVEL_CALL_DENYLIST:
                errors.append(
                    f"module-level {name}() call is not allowed"
                )
    return errors


def run(_ctx=None, **payload: Any) -> dict[str, Any]:
    workspace = payload.pop("workspace", None)
    plugin_id = str(payload.pop("plugin_id", "") or "").strip()
    code = payload.pop("code", None)
    summary = str(
        payload.pop("summary", "") or f"Install workspace plugin {plugin_id!r}"
    ).strip()

    if not _PLUGIN_ID_RE.match(plugin_id):
        return {
            "ok": False,
            "staged": False,
            "errors": [
                "plugin_id must match ^[a-z][a-z0-9_]{1,63}$ "
                f"(got {plugin_id!r})"
            ],
        }
    if not isinstance(code, str) or not code.strip():
        return {
            "ok": False,
            "staged": False,
            "errors": ["code (plugin.py contents) is required"],
        }

    errors = _static_check(code)
    if errors:
        return {"ok": False, "staged": False, "errors": errors}

    config = load_config(Path(workspace).expanduser() if workspace else None)
    paths = config.paths

    already_installed = (paths.plugins / plugin_id / "plugin.py").exists()
    proposal = create_proposal(
        paths,
        kind="plugin_proposal",
        summary=summary,
        rationale=(
            f"Workspace plugin authored by the agent via the plugin_author "
            f"skill. Installs plugins/{plugin_id}/plugin.py "
            f"({'replaces existing plugin' if already_installed else 'new'}). "
            "Static shape check passed; executes at next kernel boot as "
            "user:%s." % plugin_id
        ),
        target=f"plugins/{plugin_id}/plugin.py",
        test_plan=(
            "1. python -m pytest tests/test_harness_extensions.py\n"
            "2. After apply: run plugin_author validate_plugin with "
            f"plugin_id={plugin_id!r} and require ok=true\n"
            "3. Confirm the plugin's boot record in journals/plugins.jsonl"
        ),
        rollback=(
            f"Delete plugins/{plugin_id}/ (via proposal) or set "
            f"plugins.disabled: [{plugin_id!r}] in nerya.yml; takes effect "
            "at next kernel boot."
        ),
        extra_files={f"after/plugins/{plugin_id}/plugin.py": code},
        initial_state="pending_review",
    )
    return {
        "ok": True,
        "staged": True,
        "proposal_id": proposal.id,
        "kind": proposal.kind,
        "state": proposal.state,
        "plugin_id": plugin_id,
        "target": f"plugins/{plugin_id}/plugin.py",
        "replaces_existing": already_installed,
    }


if __name__ == "__main__":
    cli_main(run)
