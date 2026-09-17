"""Resource-backed strategy workflows, not a second execution engine.

Executable truth remains strategy.yml + package code. Solid edges are static
SDK/import evidence; dashed edges are bindings or explicitly authored notes.
workflow.json persists presentation only and cannot grant trading permissions.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
from pathlib import PurePosixPath
from typing import Any

from ..core import yaml_io
from ..core.errors import NeryaError

WORKFLOW_FILE = "workflow.json"
MAX_NODES = 250
MAX_EDGES = 800
MAX_FILE_BYTES = 200_000
NODE_KINDS = {"strategy", "source", "script", "agent", "scheduler", "account", "risk", "evidence", "proposal", "validation", "approval", "apply", "observation"}


class WorkflowError(NeryaError):
    pass


def package_revision(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, text in sorted(files.items()):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(text.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def safe_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or "\\" in value or path.is_absolute() or any(p in {".", "..", ""} for p in value.split("/")):
        raise WorkflowError(f"Invalid package path: {value!r}")
    return path.as_posix()


def read_manifest(files: dict[str, str]) -> dict[str, Any]:
    try:
        manifest = yaml_io.loads(files.get("strategy.yml", ""))
    except Exception as exc:
        raise WorkflowError(f"Invalid strategy.yml: {exc}") from exc
    if not isinstance(manifest, dict):
        raise WorkflowError("strategy.yml must be a mapping")
    return manifest


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _id(kind: str, ref: str) -> str:
    return f"{kind}:{ref}"


def _node(kind: str, ref: str, title: str, *, config: Any = None,
          path: list[str | int] | None = None, file: str | None = None,
          content: str | None = None, x: int = 0, y: int = 0,
          subtitle: str = "", href: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": _id(kind, ref), "kind": kind, "title": title, "subtitle": subtitle or ref,
        "resource": ref, "position": {"x": x, "y": y}, "config": config if config is not None else {},
        "binding": {"file": file, "path": path}, "editable": file is not None or path is not None,
    }
    if content is not None:
        node["content"] = content
    if href:
        node["href"] = href
    return node


def _edge(source: str, target: str, relation: str, *, origin: str = "manifest", label: str = "") -> dict[str, Any]:
    identity = f"{source}|{target}|{relation}" + (f"|{label}" if relation == "conditional_dispatch" else "")
    key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return {"id": f"edge:{key}", "source": source, "target": target,
            "relation": relation, "origin": origin, "label": label or relation}


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return ""


def _script_evidence(files: dict[str, str], scripts: list[str]) -> dict[str, dict[str, Any]]:
    """Conservative AST evidence. Dynamic imports/dispatches stay unclaimed."""
    modules = {p[:-3].replace("/", "."): p for p in scripts}
    evidence: dict[str, dict[str, Any]] = {}
    for path in scripts:
        row: dict[str, Any] = {"imports": set(), "agents": set(), "market": False,
                               "news": False, "dispatch": False, "team": False, "trading": False, "publishes": False, "parallel_agents": set(), "source_ids": set(), "dispatch_choices": [], "can_stop": False, "called_modules": set()}
        try:
            tree = ast.parse(files[path])
        except SyntaxError:
            row["syntax_error"] = True
            evidence[path] = row
            continue
        # Display authored documentation verbatim; never infer business rules
        # or execution success from a function name or an indicator keyword.
        row["description"] = (ast.get_docstring(tree) or "").split("\n\n", 1)[0][:600]
        aliases: dict[str, str] = {}
        for item in ast.walk(tree):
            if isinstance(item, (ast.Import, ast.ImportFrom)):
                if isinstance(item, ast.Import):
                    names = [alias.name for alias in item.names]
                    for alias in item.names:
                        if alias.asname:
                            aliases[alias.asname] = alias.name
                else:
                    base = item.module or ""
                    if item.level:
                        parents = path.split("/")[:-item.level]
                        base = ".".join(parents + ([base] if base else []))
                    names = [base] + [f"{base}.{alias.name}".strip(".") for alias in item.names]
                    for alias in item.names:
                        aliases[alias.asname or alias.name] = f"{base}.{alias.name}".strip(".")
                row["imports"].update(modules[name] for name in names if name in modules and modules[name] != path)
            if not isinstance(item, ast.Call):
                continue
            name = _call_name(item.func)
            root, _, suffix = name.partition(".")
            canonical = f"{aliases.get(root, root)}.{suffix}" if suffix else aliases.get(root, root)
            row["called_modules"].update(module_path for module, module_path in modules.items() if canonical.startswith(module + ".") and module_path != path)
            row["market"] |= name.startswith("ctx.market.") or name.startswith("ctx.data.")
            row["news"] |= name.startswith("ctx.news.")
            row["trading"] |= name.startswith("ctx.trading.")
            row["dispatch"] |= canonical.endswith("StrategyAgentTask.dispatch")
            row["can_stop"] |= canonical.endswith(("StrategyAgentTask.stop", "StrategyAgentTask.skip"))
            if canonical.endswith("StrategyAgentTask.dispatch"):
                choice = {}
                for kw in item.keywords:
                    if kw.arg in {"roles", "sources", "path"}:
                        try:
                            value = ast.literal_eval(kw.value)
                            if value is not None:
                                choice[kw.arg] = value
                        except (ValueError, TypeError, SyntaxError):
                            choice[f"dynamic_{kw.arg}"] = True
                row["dispatch_choices"].append(choice)
            row["team"] |= name == "ctx.team.run"
            row["publishes"] |= name == "ctx.inputs.publish" or (canonical.endswith("StrategyAgentTask.dispatch") and any(kw.arg == "context" for kw in item.keywords))
            if name == "ctx.inputs.source" and item.args and isinstance(item.args[0], ast.Constant):
                row["source_ids"].add(str(item.args[0].value))
            if name == "ctx.subagents.run_many":
                values = list(item.args[:1]) + [kw.value for kw in item.keywords if kw.arg == "names"]
                for value in values:
                    if isinstance(value, (ast.List, ast.Tuple)):
                        row["parallel_agents"].update(str(child.value) for child in value.elts if isinstance(child, ast.Constant) and isinstance(child.value, str))
                row["agents"].update(row["parallel_agents"])
            if name == "ctx.subagents.run":
                values = list(item.args[:1]) + [kw.value for kw in item.keywords if kw.arg == "name"]
                row["agents"].update(str(value.value) for value in values if isinstance(value, ast.Constant) and isinstance(value.value, str))
        evidence[path] = row
    return evidence


def build_workflows(files: dict[str, str]) -> dict[str, Any]:
    manifest = read_manifest(files)
    sid = str(manifest.get("strategy_id") or manifest.get("id") or "strategy")
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    add = nodes.append
    root = _node("strategy", sid, str(manifest.get("title") or sid),
                 config={"title": manifest.get("title", sid), "description": manifest.get("description", "")},
                 path=[], x=30, y=30, subtitle=f"{manifest.get('mode', manifest.get('status', 'draft'))} · {sid}")
    add(root)
    schedule = _object(manifest.get("schedule"))
    if schedule:
        add(_node("scheduler", "trading", "Trading schedule", config=schedule, path=["schedule"],
                  x=30, y=210, subtitle=str(schedule.get("cron") or f"every {schedule.get('every_seconds', '?')}s")))
        edges.append(_edge(root["id"], "scheduler:trading", "configures"))
    sources: list[dict[str, Any]] = []
    for key in ("markets", "news_sources", "data_sources"):
        values = manifest.get(key)
        pairs = list(values.items()) if isinstance(values, dict) else list(enumerate(_items(values)))
        for index, value in pairs:
            raw = _object(value)
            ref = str(raw.get("id") or (index if raw else value))
            title = str(raw.get("title") or raw.get("name") or value)
            node = _node("source", f"{key}/{ref}", title, config=value, path=[key, index],
                         x=30, y=390 + len(sources) * 166,
                         subtitle=str(raw.get("capability") or raw.get("provider") or key))
            add(node)
            sources.append(node)
    scripts = sorted(p for p in files if p.endswith(".py") and p.split("/")[0] not in {"tests", "fixtures"} and not p.endswith("__init__.py"))
    entry = str(manifest.get("entrypoint") or "main.py:run").split(":")[0]
    scripts.sort(key=lambda p: (p != entry, p))
    evidence = _script_evidence(files, scripts)
    for index, path in enumerate(scripts):
        add(_node("script", path, path, file=path, content=files[path],
                  x=340, y=120 + index * 176, subtitle="entrypoint" if path == entry else "Python module"))
        row = evidence[path]
        if row["can_stop"] or row["dispatch_choices"]:
            nodes[-1]["control"] = {"can_stop": row["can_stop"], "paths": [c["path"] for c in row["dispatch_choices"] if isinstance(c.get("path"), str)]}
        if row.get("description"):
            nodes[-1]["description"] = row["description"]
        for imported in sorted(row["imports"]):
            if imported in row["called_modules"] and evidence[imported]["dispatch"]:
                edges.append(_edge(_id("script", path), _id("script", imported), "branch_call", label="脚本分支", origin="static"))
            else:
                edges.append(_edge(_id("script", imported), _id("script", path), "imports", origin="static"))
        for source in sources:
            category = source["binding"]["path"][0]
            declared = path in _items(_object(source["config"]).get("consumers"))
            actual_source = str(_object(source["config"]).get("id") or source["binding"]["path"][1]) in row["source_ids"]
            if (category == "markets" and row["market"]) or (category == "news_sources" and row["news"]):
                edges.append(_edge(source["id"], _id("script", path), "data", origin="static"))
            elif actual_source or declared:
                edges.append(_edge(source["id"], _id("script", path), "data", origin="static" if actual_source else "declared"))
    entry_id = _id("script", entry) if entry in scripts else root["id"]
    edges.append(_edge(root["id"], entry_id, "entrypoint"))
    if schedule:
        edges.append(_edge("scheduler:trading", entry_id, "triggers"))
    execution = str(manifest.get("execution_mode") or manifest.get("driver") or "script")
    has_dispatch = any(r["dispatch"] or r["team"] for r in evidence.values())
    agent_mode = execution in {"agent", "agent_task", "agent_team", "team"}
    agent_y = 120
    if agent_mode or has_dispatch:
        node = _node("agent", "runtime", "Strategy Agent", config={
            "agent_session": _object(manifest.get("agent_session")),
            "agent_profile": _object(manifest.get("agent_profile")),
            "llm_policy": _object(manifest.get("llm_policy")),
            "agent_execution": _object(manifest.get("agent_execution")),
            "agent_context": _object(manifest.get("agent_context")),
        }, path=["$agent"], x=650, y=agent_y, subtitle=execution if agent_mode else "script dispatch")
        add(node)
        agent_y += 176
        for path, row in evidence.items():
            if row["dispatch"] or row["team"]:
                edges.append(_edge(_id("script", path), node["id"], "dispatch", origin="static"))
        if not has_dispatch:
            edges.append(_edge(root["id"], node["id"], "declares", origin="manifest"))
    for name_value in _items(manifest.get("subagents")):
        name = str(name_value)
        path = f"subagents/{name}.agent.md"
        prompt = files.get(path)
        node = _node("agent", f"role/{name}", name, file=path if prompt is not None else None,
                     content=prompt, config={"name": name}, x=650, y=agent_y,
                     subtitle="package prompt" if prompt is not None else "workspace Agent",
                     href="/agents" if prompt is None else None)
        add(node)
        agent_y += 176
        called = False
        for script, row in evidence.items():
            if name in row["agents"] or row["team"]:
                edges.append(_edge(_id("script", script), node["id"], "calls", origin="static"))
                called = True
        if not called:
            edges.append(_edge("agent:runtime" if agent_mode or has_dispatch else root["id"], node["id"], "role", origin="manifest"))
    add(_node("risk", "policy", "Risk & approval gate", config=_object(manifest.get("policy") or manifest.get("limits")),
              path=["policy"] if "policy" in manifest else ["limits"], x=960, y=120, subtitle="SDK trading gate"))
    for path, row in evidence.items():
        if row["trading"]:
            edges.append(_edge(_id("script", path), "risk:policy", "checks", origin="static"))
    if agent_mode or has_dispatch:
        edges.append(_edge("agent:runtime", "risk:policy", "guarded_by"))
    else:
        edges.append(_edge(entry_id, "risk:policy", "guarded_by"))
    accounts = _items(manifest.get("accounts") or manifest.get("account_id"))
    for index, account in enumerate(accounts):
        aid = str(account)
        node = _node("account", aid, aid, config=aid,
                     path=["accounts", index] if "accounts" in manifest else ["account_id"],
                     x=960, y=320 + index * 166, subtitle="bound account · no credentials", href=f"/accounts/{aid}")
        add(node)
        edges.append(_edge("risk:policy", node["id"], "account_binding"))
    if manifest.get("wallet_id"):
        wallet = str(manifest["wallet_id"])
        add(_node("account", f"wallet/{wallet}", wallet, config=wallet, path=["wallet_id"],
                  x=960, y=320 + len(accounts) * 166, subtitle="wallet reference", href="/accounts"))
        edges.append(_edge("risk:policy", f"account:wallet/{wallet}", "wallet_binding"))
    # Project executable input/team configuration. Annotation edges remain
    # display-only, and an unused declared role is never labelled as executed.
    if agent_mode or has_dispatch:
        team = _object(_object(manifest.get("agent_execution")).get("team"))
        team_enabled = team.get("enabled", _object(manifest.get("agent_task")).get("mode") == "agent_team")
        default_roles = (_items(team.get("roles")) or _items(manifest.get("subagents"))) if team_enabled else []
        context = _object(manifest.get("agent_context"))
        plans = []
        declared_roles = set(_items(manifest.get("subagents")))
        for path, proof in evidence.items():
            for choice in proof["dispatch_choices"] or ([{}] if proof["team"] else []):
                chosen = choice.get("roles", default_roles)
                dynamic = bool(choice.get("dynamic_roles")) or not isinstance(chosen, list)
                names = [name for name in chosen if isinstance(name, str) and name in declared_roles] if not dynamic else []
                plans.append({"script": path, "choice": choice, "names": names, "dynamic": dynamic,
                              "targets": [_id("agent", f"role/{name}") for name in names] or ["agent:runtime"]})
        role_names = list(dict.fromkeys(name for plan in plans for name in plan["names"]))
        parallel_ids = [_id("agent", f"role/{name}") for name in role_names]
        dispatched_scripts = {_id("script", plan["script"]) for plan in plans}
        edges = [e for e in edges if not (e["source"] == "agent:runtime" and e["target"] in parallel_ids)
                 and not (e["source"] in dispatched_scripts and e["target"] == "agent:runtime" and e["relation"] == "dispatch")]
        for plan in plans:
            choice = plan["choice"]
            conditional = "roles" in choice or plan["dynamic"] or "path" in choice
            label = str(choice.get("path") or ("由脚本选择" if conditional else "并行执行" if plan["names"] else "派发分析"))
            relation = "conditional_dispatch" if conditional else "parallel_dispatch" if plan["names"] else "dispatch"
            for target in plan["targets"]:
                edges.append(_edge(_id("script", plan["script"]), target, relation, label=label, origin="static" if conditional else "manifest"))
            selected_sources = choice.get("sources", _items(context.get("sources")))
            if not choice.get("dynamic_sources") and isinstance(selected_sources, list):
                for source in sources:
                    source_id = str(_object(source["config"]).get("id") or source["binding"]["path"][1])
                    if source_id in selected_sources:
                        for target in plan["targets"]:
                            edges.append(_edge(source["id"], target, "context", label="输入数据", origin="static" if "sources" in choice else "manifest"))
        for index, role_id in enumerate(parallel_ids):
            role_node = next((n for n in nodes if n["id"] == role_id), None)
            if role_node:
                role_node["position"] = {"x": 650, "y": 120 + index * 190}
                conditional = any(role_id in p["targets"] and ("roles" in p["choice"] or "path" in p["choice"]) for p in plans)
                role_node["execution"] = {"mode": "conditional" if conditional else "parallel", "policy": _object(team.get("role_policies")).get(role_names[index], {})}
            edges.append(_edge(role_id, "agent:runtime", "aggregate", label="结果汇总", origin="manifest"))
        if parallel_ids:
            coordinator = next(n for n in nodes if n["id"] == "agent:runtime")
            coordinator["position"] = {"x": 970, "y": 120 + (len(parallel_ids) - 1) * 95}
            coordinator["subtitle"] = "selected role results → autonomous coordination"
            for n in nodes:
                if n["kind"] in {"risk", "account"}:
                    n["position"]["y"] += len(parallel_ids) * 190
        branch_scripts = list(dict.fromkeys(plan["script"] for plan in plans))
        if len(branch_scripts) > 1 and parallel_ids:
            # Align factory scripts with their exclusively selected roles.
            # apply_metadata below still wins over these default positions.
            for index, script in enumerate(branch_scripts):
                branch_node = next(n for n in nodes if n["id"] == _id("script", script))
                branch_node["position"] = {"x": 650, "y": 120 + index * 220}
                for plan in (p for p in plans if p["script"] == script):
                    for target in plan["targets"]:
                        if target != "agent:runtime" and sum(target in p["targets"] for p in plans) == 1:
                            next(n for n in nodes if n["id"] == target)["position"] = {"x": 970, "y": 120 + index * 220}
            next(n for n in nodes if n["id"] == "agent:runtime")["position"] = {"x": 1290, "y": 120 + (len(branch_scripts) - 1) * 110}
            other_scripts = [n for n in nodes if n["kind"] == "script" and n["id"] not in {_id("script", p) for p in branch_scripts}]
            for index, helper in enumerate(other_scripts):
                helper["position"] = {"x": 340, "y": 120 + index * 220}
        for path, proof in evidence.items():
            if proof["publishes"] and context.get("include_script_outputs", True):
                own = [p for p in plans if p["script"] == path]
                targets = {target for p in own for target in p["targets"]} if own else set(parallel_ids or ["agent:runtime"])
                for target in targets:
                    edges.append(_edge(_id("script", path), target, "context", label="脚本输出", origin="static"))
    # Every declared resource remains discoverable even when dynamic code
    # prevents static call evidence. These bindings are deliberately dashed.
    connected = {e[key] for e in edges for key in ("source", "target")}
    for resource in nodes:
        if resource["id"] in connected or resource["id"] == root["id"]:
            continue
        if resource["kind"] == "source" and (agent_mode or has_dispatch):
            edges.append(_edge(resource["id"], "agent:runtime", "available_to"))
        else:
            edges.append(_edge(root["id"], resource["id"], "declares"))
    ids = [resource["id"] for resource in nodes]
    if len(ids) != len(set(ids)):
        raise WorkflowError("Duplicate resource IDs; give presets, Agents and account bindings unique identities")
    evolution = _evolution_graph(manifest, files)
    graph = {"id": "strategy", "nodes": nodes, "edges": _unique_edges(edges)}
    metadata = parse_metadata(files.get(WORKFLOW_FILE, ""))
    all_ids = {n["id"] for g in (graph, evolution) for n in g["nodes"]}
    validate_metadata(metadata, all_ids)
    for view in (graph, evolution):
        apply_metadata(view, metadata)
    if len(nodes) + len(evolution["nodes"]) > MAX_NODES:
        raise WorkflowError(f"Workflow exceeds {MAX_NODES} nodes")
    return {"strategy": graph, "evolution": evolution, "manifest": manifest,
            "revision": package_revision(files), "legacy": not bool(manifest.get("entrypoint") and schedule)}


def _evolution_graph(manifest: dict[str, Any], files: dict[str, str]) -> dict[str, Any]:
    tuning = _object(manifest.get("tuning"))
    subagent = _object(tuning.get("subagent"))
    prompt_path = str(subagent.get("prompt_file") or "subagents/strategy_tuner.agent.md")
    nodes = [
        _node("evidence", "review", "Run evidence", config=_object(tuning.get("lookback")), path=["tuning", "lookback"], x=30, y=60, subtitle="runs · fills · drawdown"),
        _node("scheduler", "tuning", "Review schedule", config=_object(tuning.get("schedule")), path=["tuning", "schedule"], x=30, y=280, subtitle="independent of trading"),
        _node("agent", "tuner", str(subagent.get("name") or "strategy_tuner"), config=subagent,
              file=prompt_path if prompt_path in files else None, content=files.get(prompt_path),
              path=None if prompt_path in files else ["tuning", "subagent"], x=340, y=160, subtitle="review & propose"),
        _node("proposal", "tuning", "Change proposal", config={"objectives": tuning.get("objectives", []), "proposal_policy": _object(tuning.get("proposal_policy")), "tuning_prompt": tuning.get("tuning_prompt", "")}, path=["$tuning"], x=650, y=60, subtitle="code · configuration · prompt"),
        _node("validation", "tuning", "Validation & replay", config=_object(tuning.get("guardrails")), path=["tuning", "guardrails"], x=960, y=60, subtitle="static scan → backtest → shadow"),
        _node("approval", "operator", "Operator approval", config={"required": True}, x=960, y=300, subtitle="approval cannot be skipped", href="/self-evolution?tab=proposals"),
        _node("apply", "version", "Version & apply", config={"protected_scopes": ["accounts", "secrets", "live_trading_enabled"]}, x=650, y=440, subtitle="reviewed candidate only", href="/self-evolution?tab=proposals"),
        _node("observation", "feedback", "Observe & learn", config={"description": "Post-apply observations feed the next review; a graph is not execution evidence."}, x=340, y=440, subtitle="health · rollback · next review", href="/self-evolution?tab=timeline"),
    ]
    chain = [("evidence:review", "agent:tuner"), ("scheduler:tuning", "agent:tuner"), ("agent:tuner", "proposal:tuning"),
             ("proposal:tuning", "validation:tuning"), ("validation:tuning", "approval:operator"), ("approval:operator", "apply:version"),
             ("apply:version", "observation:feedback"), ("observation:feedback", "evidence:review")]
    return {"id": "evolution", "enabled": bool(tuning.get("enabled")), "nodes": nodes,
            "edges": [_edge(a, b, "feedback" if b == "evidence:review" else "review_stage") for a, b in chain]}


def _unique_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list({e["id"]: e for e in edges if e["source"] != e["target"]}.values())


def parse_metadata(text: str) -> dict[str, Any]:
    if not text.strip():
        return {"version": 1, "nodes": {}, "edges": []}
    try:
        raw = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise WorkflowError(f"Invalid {WORKFLOW_FILE}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise WorkflowError("workflow.json requires version: 1")
    return raw


def validate_metadata(metadata: dict[str, Any], node_ids: set[str]) -> None:
    nodes, edges = metadata.get("nodes", {}), metadata.get("edges", [])
    if not isinstance(nodes, dict) or len(nodes) > MAX_NODES:
        raise WorkflowError("workflow.nodes must be a bounded mapping of resource node IDs")
    if not isinstance(edges, list) or len(edges) > MAX_EDGES:
        raise WorkflowError("workflow.edges must be a bounded array")
    for nid, value in nodes.items():
        if not isinstance(value, dict):
            raise WorkflowError(f"Invalid layout for {nid}")
        position = value.get("position", {})
        if not isinstance(position, dict):
            raise WorkflowError(f"Invalid position for {nid}")
        for coordinate in (position.get("x", 0), position.get("y", 0)):
            if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)) or not math.isfinite(coordinate) or abs(coordinate) > 100_000:
                raise WorkflowError(f"Invalid coordinate for {nid}")
        for key in ("title", "description"):
            if key in value and (not isinstance(value[key], str) or len(value[key]) > 2000):
                raise WorkflowError(f"Invalid {key} for {nid}")
    seen: set[str] = set()
    for edge in edges:
        if not isinstance(edge, dict) or not isinstance(edge.get("id"), str) or not edge["id"] or edge["id"] in seen:
            raise WorkflowError("Annotation edges must have unique IDs")
        seen.add(edge["id"])
        if edge.get("source") not in node_ids or edge.get("target") not in node_ids:
            raise WorkflowError("Annotation edge references a missing resource")
        if edge["source"] == edge["target"]:
            raise WorkflowError("Self-connected annotation is not allowed")
        if edge.get("relation", "annotation") != "annotation":
            raise WorkflowError("Authored edges are annotations; executable edges come from SDK code")
        if not isinstance(edge.get("label", ""), str) or len(edge.get("label", "")) > 160:
            raise WorkflowError("Edge label must be at most 160 characters")


def apply_metadata(graph: dict[str, Any], metadata: dict[str, Any]) -> None:
    for node in graph["nodes"]:
        override = metadata.get("nodes", {}).get(node["id"], {})
        for key in ("title", "description", "position"):
            if key in override:
                node[key] = copy.deepcopy(override[key])
    ids = {node["id"] for node in graph["nodes"]}
    graph["edges"].extend({**edge, "origin": "annotation", "relation": "annotation"}
                          for edge in metadata.get("edges", []) if edge["source"] in ids and edge["target"] in ids)


def ensure_workflow_file(files: dict[str, str]) -> None:
    """All generated packages are workflow-ready, including later file edits."""
    if WORKFLOW_FILE not in files:
        files[WORKFLOW_FILE] = json.dumps({"version": 1, "nodes": {}, "edges": []}, indent=2) + "\n"
