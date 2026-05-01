"""Reload one of Nerya's hot-pluggable subsystems and report the result.

Targets supported: ``providers`` (exchange/DEX provider specs from
``workspace/providers/``), ``skills`` (SkillKernel manifests), and
``models`` (LLM model registry).

Standalone CLI usage::

    python -m nerya.skills.builtin.coding.scripts.reload_subsystem \
        --json '{"target": "providers", "workspace": "/path/to/ws"}'

Output::

    {
      "target": "providers",
      "ok": true,
      "ids": ["user:myexchange", "binance", ...]
    }

The reload is in-process: it only affects callers that share this
Python interpreter. For multi-process deployments, send the reload to
each worker (or restart).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _resolve_workspace(arg: str | None) -> Path:
    return Path(arg).expanduser().resolve() if arg else Path(os.getcwd()).resolve()


def _reload_providers(workspace: Path) -> dict[str, Any]:
    from nerya.connectors.registry import ConnectorRegistry
    from nerya.connectors.provider_spec import get_registry
    ConnectorRegistry(workspace=workspace).reload_providers()
    ids = sorted({s.id for s in get_registry().list_specs()})
    return {"target": "providers", "ok": True, "ids": ids}


def _reload_skills(workspace: Path) -> dict[str, Any]:
    from nerya.core.config import Config
    from nerya.skills.kernel import SkillKernel
    cfg = Config.load(workspace_root=workspace)
    kernel = SkillKernel.boot(cfg)
    kernel.reload()
    ids = sorted(e.manifest.id for e in kernel.registry.list())
    return {"target": "skills", "ok": True, "ids": ids}


def _reload_models() -> dict[str, Any]:
    from nerya.llm.model_registry import ModelRegistry
    reg = ModelRegistry()
    reg.reload()
    ids = sorted(reg.list_models()) if hasattr(reg, "list_models") else []
    return {"target": "models", "ok": True, "ids": ids}


def run(target: str, *, workspace: str | None = None) -> dict[str, Any]:
    ws = _resolve_workspace(workspace)
    if target == "providers":
        return _reload_providers(ws)
    if target == "skills":
        return _reload_skills(ws)
    if target == "models":
        return _reload_models()
    raise ValueError(f"unknown target {target!r}; expected providers|skills|models")


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        with open(args.payload_file, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    if args.payload_json:
        return json.loads(args.payload_json) or {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", dest="payload_json", default=None)
    parser.add_argument("--payload-file", dest="payload_file", default=None)
    parser.add_argument("--target", dest="target", default=None,
                        choices=["providers", "skills", "models"])
    parser.add_argument("--workspace", dest="workspace", default=None)
    args = parser.parse_args()

    payload = _load_payload(args)
    target = args.target or payload.get("target")
    if not target:
        sys.stderr.write("missing target (providers|skills|models)\n")
        raise SystemExit(2)
    workspace = args.workspace or payload.get("workspace")

    try:
        result = run(target, workspace=workspace)
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
