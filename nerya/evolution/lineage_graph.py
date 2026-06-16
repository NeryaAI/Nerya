"""Structured lineage graph reducer for self-evolution proposals."""

from __future__ import annotations

import hashlib
from typing import Any


VERSION = "lineage_graph_v1"
MAX_NODES = 80
MAX_EDGES = 140


def build_lineage_graph(
    proposal: dict[str, Any],
    *,
    validation_plan: dict[str, Any] | None = None,
    backtest_comparison: dict[str, Any] | None = None,
    post_apply_monitor: dict[str, Any] | None = None,
    why_reused: dict[str, Any] | None = None,
    action_gates: dict[str, Any] | None = None,
    file_changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a stable graph contract for proposal lineage UI.

    This intentionally derives from already-redacted proposal/timeline fields.
    The full graph UI should consume this envelope instead of stitching nodes
    from several detail fields in the browser.
    """

    pid = _clean_id(proposal.get("id") or proposal.get("proposal_id"))
    root_id = f"proposal:{pid}" if pid else "proposal:unknown"
    graph = _Graph(root_id=root_id)

    graph.add_node(
        root_id,
        type="proposal",
        label=_label(proposal.get("summary"), fallback=pid or "Proposal"),
        status=_status(proposal.get("state") or proposal.get("status")),
        summary=str(proposal.get("summary") or ""),
        ts=str(proposal.get("ts") or proposal.get("state_ts") or ""),
        evidence_refs=_str_list(proposal.get("evidence_refs")),
        metadata={
            "proposal_id": pid,
            "kind": proposal.get("kind"),
            "strategy_id": _proposal_strategy(proposal),
            "target": proposal.get("target"),
            "source_event_id": proposal.get("source_event_id"),
            "validation_plan_id": proposal.get("validation_plan_id"),
        },
    )

    source_event_id = _clean_id(proposal.get("source_event_id"))
    if source_event_id:
        event_node = f"event:{source_event_id}"
        graph.add_node(
            event_node,
            type="event",
            label="Evolution event",
            status="linked",
            summary="Proposal was created from this evolution event.",
            evidence_refs=_str_list(proposal.get("evidence_refs")),
            metadata={"event_id": source_event_id},
        )
        graph.add_edge(event_node, root_id, type="triggered", label="triggered proposal")

    signal_nodes = _add_signal_nodes(graph, why_reused, root_id)
    _add_reuse_asset_nodes(graph, why_reused, root_id, signal_nodes)
    _add_file_change_nodes(graph, root_id, file_changes, why_reused, action_gates)
    _add_validation_nodes(graph, proposal, root_id, validation_plan, action_gates)
    _add_backtest_node(graph, root_id, backtest_comparison)
    _add_lifecycle_nodes(graph, proposal, root_id)
    _add_post_apply_nodes(graph, root_id, post_apply_monitor)
    _add_action_gate_node(graph, root_id, action_gates)

    if len(graph.nodes) > MAX_NODES:
        graph.warnings.append(f"node_limit_exceeded:{len(graph.nodes)}")
        graph.nodes = graph.nodes[:MAX_NODES]
        graph.truncated = True
    if len(graph.edges) > MAX_EDGES:
        graph.warnings.append(f"edge_limit_exceeded:{len(graph.edges)}")
        graph.edges = graph.edges[:MAX_EDGES]
        graph.truncated = True

    return graph.asdict()


class _Graph:
    def __init__(self, *, root_id: str) -> None:
        self.root_id = root_id
        self.nodes: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.truncated = False
        self._node_index: dict[str, dict[str, Any]] = {}
        self._edge_ids: set[str] = set()

    def add_node(
        self,
        node_id: str,
        *,
        type: str,
        label: str,
        status: str = "",
        summary: str = "",
        ts: str = "",
        evidence_refs: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        node_id = _clean_node_id(node_id)
        if not node_id:
            return
        refs = _unique_strings(evidence_refs or [])
        if node_id in self._node_index:
            node = self._node_index[node_id]
            node["evidence_refs"] = _unique_strings([*node.get("evidence_refs", []), *refs])
            return
        node = {
            "id": node_id,
            "type": str(type or "unknown"),
            "label": _label(label, fallback=node_id),
            "status": _status(status),
            "summary": _bounded(summary, 360),
            "ts": str(ts or ""),
            "evidence_refs": refs,
            "metadata": _compact_metadata(metadata or {}),
        }
        self._node_index[node_id] = node
        self.nodes.append(node)

    def add_edge(
        self,
        source: str,
        target: str,
        *,
        type: str,
        label: str = "",
        status: str = "",
        evidence_refs: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        source = _clean_node_id(source)
        target = _clean_node_id(target)
        if not source or not target or source == target:
            return
        edge_id = f"{source}->{target}:{type}"
        if edge_id in self._edge_ids:
            return
        self._edge_ids.add(edge_id)
        self.edges.append({
            "id": edge_id,
            "source": source,
            "target": target,
            "type": str(type or "linked"),
            "label": _label(label or type, fallback="linked"),
            "status": _status(status),
            "evidence_refs": _unique_strings(evidence_refs or []),
            "metadata": _compact_metadata(metadata or {}),
        })

    def asdict(self) -> dict[str, Any]:
        evidence_refs = _unique_strings([
            ref
            for item in [*self.nodes, *self.edges]
            for ref in _str_list(item.get("evidence_refs"))
        ])
        return {
            "version": VERSION,
            "root_id": self.root_id,
            "nodes": self.nodes,
            "edges": self.edges,
            "evidence_refs": evidence_refs,
            "warnings": self.warnings,
            "truncated": self.truncated,
        }


def _add_signal_nodes(
    graph: _Graph,
    why_reused: dict[str, Any] | None,
    root_id: str,
) -> list[str]:
    nodes: list[str] = []
    if not isinstance(why_reused, dict):
        return nodes
    for idx, signal in enumerate(_dict_list(why_reused.get("selection_signals"))[:10]):
        sid = _clean_id(signal.get("id")) or _stable_id("signal", signal.get("kind"), idx)
        node_id = f"signal:{sid}"
        graph.add_node(
            node_id,
            type="signal",
            label=_label(signal.get("kind"), fallback="Signal"),
            status=_status(signal.get("severity") or signal.get("status")),
            summary=str(signal.get("summary") or ""),
            evidence_refs=_str_list(signal.get("evidence_refs")),
            metadata={
                "signal_id": signal.get("id"),
                "kind": signal.get("kind"),
                "confidence": signal.get("confidence"),
                "metadata": signal.get("metadata"),
            },
        )
        graph.add_edge(
            node_id,
            root_id,
            type="triggered",
            label="triggered proposal",
            evidence_refs=_str_list(signal.get("evidence_refs")),
        )
        nodes.append(node_id)
    return nodes


def _add_reuse_asset_nodes(
    graph: _Graph,
    why_reused: dict[str, Any] | None,
    root_id: str,
    signal_nodes: list[str],
) -> None:
    if not isinstance(why_reused, dict):
        return
    for key, node_type, edge_type in (
        ("genes", "gene", "selected"),
        ("capsules", "capsule", "selected"),
        ("negative_capsules", "negative_capsule", "cautioned"),
    ):
        for idx, asset in enumerate(_dict_list(why_reused.get(key))[:12]):
            aid = _clean_id(asset.get("id")) or _stable_id(node_type, asset.get("summary"), idx)
            node_id = f"{node_type}:{aid}"
            graph.add_node(
                node_id,
                type=node_type,
                label=_label(asset.get("id"), fallback=node_type.replace("_", " ").title()),
                status=_status(asset.get("polarity") or ("negative" if key == "negative_capsules" else "selected")),
                summary=str(asset.get("summary") or asset.get("rationale") or ""),
                evidence_refs=_str_list(asset.get("evidence_refs")),
                metadata={
                    "asset_id": asset.get("id"),
                    "gdi_score": asset.get("gdi_score"),
                    "outcome_score": asset.get("outcome_score"),
                    "relevance_score": asset.get("relevance_score"),
                    "relevance_source": asset.get("relevance_source"),
                    "matched_signals": asset.get("matched_signals"),
                    "matched_context": asset.get("matched_context"),
                },
            )
            for signal_node in signal_nodes[:4]:
                graph.add_edge(signal_node, node_id, type="matched", label="matched trigger")
            graph.add_edge(
                node_id,
                root_id,
                type=edge_type,
                label="selected for proposal" if edge_type == "selected" else "cautioned proposal",
                evidence_refs=_str_list(asset.get("evidence_refs")),
            )


def _add_file_change_nodes(
    graph: _Graph,
    root_id: str,
    file_changes: list[dict[str, Any]] | None,
    why_reused: dict[str, Any] | None,
    action_gates: dict[str, Any] | None,
) -> None:
    paths: list[str] = []
    for change in _dict_list(file_changes):
        if change.get("path"):
            paths.append(str(change.get("path")))
    if not paths and isinstance(why_reused, dict):
        diff = why_reused.get("proposal_diff")
        if isinstance(diff, dict):
            paths.extend(_str_list(diff.get("paths")))
    if not paths:
        materialization = (action_gates or {}).get("materialization")
        if isinstance(materialization, dict):
            paths.extend(_str_list(materialization.get("paths")))
    for path in _unique_strings(paths)[:20]:
        node_id = f"file_change:{_path_id(path)}"
        graph.add_node(
            node_id,
            type="file_change",
            label=path,
            status="proposed",
            summary=f"Proposed change to {path}.",
            metadata={"path": path},
        )
        graph.add_edge(root_id, node_id, type="proposed_change", label="changes file")


def _add_validation_nodes(
    graph: _Graph,
    proposal: dict[str, Any],
    root_id: str,
    validation_plan: dict[str, Any] | None,
    action_gates: dict[str, Any] | None,
) -> None:
    plan_id = _clean_id(
        (validation_plan or {}).get("id")
        or proposal.get("validation_plan_id")
        or ((action_gates or {}).get("validation") or {}).get("plan_id")
    )
    if not plan_id:
        return
    status = (
        (validation_plan or {}).get("status")
        or ((action_gates or {}).get("validation") or {}).get("status")
        or "not_run"
    )
    plan_node = f"validation_plan:{plan_id}"
    graph.add_node(
        plan_node,
        type="validation_plan",
        label="Validation plan",
        status=_status(status),
        summary=_validation_summary(validation_plan or {}),
        evidence_refs=_validation_refs(validation_plan or action_gates or {}),
        metadata={
            "plan_id": plan_id,
            "last_run_id": (validation_plan or {}).get("last_run_id"),
            "blocked_reasons": (validation_plan or {}).get("blocked_reasons"),
        },
    )
    graph.add_edge(root_id, plan_node, type="requires_validation", label="requires validation")
    last_run_id = _clean_id((validation_plan or {}).get("last_run_id"))
    if last_run_id:
        run_node = f"validation_run:{last_run_id}"
        graph.add_node(
            run_node,
            type="validation_run",
            label=last_run_id,
            status=_status(status),
            summary="Latest validation run.",
            ts=str((validation_plan or {}).get("last_run_at") or ""),
            evidence_refs=[f"validation:{last_run_id}"],
            metadata={"run_id": last_run_id},
        )
        graph.add_edge(plan_node, run_node, type="executed_as", label="executed as")
    for idx, step in enumerate(_dict_list((validation_plan or {}).get("steps"))[:16]):
        step_node = f"validation_step:{plan_id}:{idx}"
        evidence_ref = step.get("evidence_ref")
        graph.add_node(
            step_node,
            type="validation_step",
            label=str(step.get("type") or f"step {idx + 1}"),
            status=_status(step.get("status") or "not_run"),
            summary=str(step.get("notes") or step.get("reason") or step.get("command") or ""),
            evidence_refs=[str(evidence_ref)] if evidence_ref else [],
            metadata={
                "index": idx,
                "required": step.get("required", True),
                "command": step.get("command"),
            },
        )
        graph.add_edge(plan_node, step_node, type="contains", label="contains step")


def _add_backtest_node(
    graph: _Graph,
    root_id: str,
    backtest_comparison: dict[str, Any] | None,
) -> None:
    if not isinstance(backtest_comparison, dict):
        return
    node_id = f"backtest_comparison:{_path_id(root_id)}"
    graph.add_node(
        node_id,
        type="backtest_comparison",
        label="Backtest before/after",
        status=_status(backtest_comparison.get("status")),
        summary=str(backtest_comparison.get("summary") or ""),
        evidence_refs=_str_list(backtest_comparison.get("evidence_refs")),
        metadata={
            "strategy_id": backtest_comparison.get("strategy_id"),
            "before_id": ((backtest_comparison.get("before") or {}) if isinstance(backtest_comparison.get("before"), dict) else {}).get("backtest_id"),
            "after_id": ((backtest_comparison.get("after") or {}) if isinstance(backtest_comparison.get("after"), dict) else {}).get("backtest_id"),
            "metric_count": len(backtest_comparison.get("metrics_delta") or []),
        },
    )
    graph.add_edge(
        root_id,
        node_id,
        type="validated_by",
        label="validated by backtest",
        status=_status(backtest_comparison.get("status")),
        evidence_refs=_str_list(backtest_comparison.get("evidence_refs")),
    )


def _add_lifecycle_nodes(graph: _Graph, proposal: dict[str, Any], root_id: str) -> None:
    state = _status(proposal.get("state") or proposal.get("status"))
    if state in {"approved", "applied", "rolled_back"}:
        approval_node = f"approval:{_path_id(root_id)}"
        graph.add_node(
            approval_node,
            type="approval",
            label="Approved",
            status="approved",
            summary="Operator approved this proposal.",
            ts=str(proposal.get("state_ts") or ""),
            evidence_refs=_str_list(proposal.get("evidence_refs")),
        )
        graph.add_edge(root_id, approval_node, type="approved_by", label="approved by operator")
    if state in {"applied", "rolled_back"}:
        apply_node = f"apply:{_path_id(root_id)}"
        graph.add_node(
            apply_node,
            type="apply",
            label="Applied",
            status="applied",
            summary="Proposal changes were applied through the governed workflow.",
            ts=str(proposal.get("state_ts") or ""),
            evidence_refs=_str_list(proposal.get("evidence_refs")),
        )
        graph.add_edge(root_id, apply_node, type="applied_as", label="applied as mutation")
    if state == "rolled_back":
        rollback_node = f"rollback:{_path_id(root_id)}"
        graph.add_node(
            rollback_node,
            type="rollback",
            label="Rolled back",
            status="rolled_back",
            summary="Applied proposal was rolled back.",
            ts=str(proposal.get("state_ts") or ""),
            evidence_refs=_str_list(proposal.get("evidence_refs")),
        )
        graph.add_edge(root_id, rollback_node, type="rolled_back_by", label="rolled back")
    if state == "rejected":
        reject_node = f"rejection:{_path_id(root_id)}"
        graph.add_node(
            reject_node,
            type="rejection",
            label="Rejected",
            status="rejected",
            summary=str(proposal.get("state_note") or "Operator rejected this proposal."),
            ts=str(proposal.get("state_ts") or ""),
            evidence_refs=_str_list(proposal.get("evidence_refs")),
        )
        graph.add_edge(root_id, reject_node, type="downweighted_by", label="downweighted by rejection")


def _add_post_apply_nodes(
    graph: _Graph,
    root_id: str,
    post_apply_monitor: dict[str, Any] | None,
) -> None:
    if not isinstance(post_apply_monitor, dict):
        return
    monitor_node = f"post_apply_monitor:{_path_id(root_id)}"
    graph.add_node(
        monitor_node,
        type="post_apply_monitor",
        label="Post-apply monitor",
        status=_status(post_apply_monitor.get("status")),
        summary=str(post_apply_monitor.get("summary") or ""),
        ts=str(post_apply_monitor.get("observed_at") or ""),
        evidence_refs=_str_list(post_apply_monitor.get("evidence_refs")),
        metadata={
            "observation_count": len(post_apply_monitor.get("observations") or []),
            "weighted_status": (post_apply_monitor.get("weighted_summary") or {}).get("status")
            if isinstance(post_apply_monitor.get("weighted_summary"), dict) else None,
        },
    )
    graph.add_edge(
        root_id,
        monitor_node,
        type="observed_by",
        label="observed after apply",
        evidence_refs=_str_list(post_apply_monitor.get("evidence_refs")),
    )
    for idx, obs in enumerate(_dict_list(post_apply_monitor.get("observations"))[:8]):
        obs_id = _clean_id(obs.get("id")) or _stable_id("obs", obs.get("journal_ref"), idx)
        obs_node = f"post_apply_observation:{obs_id}"
        graph.add_node(
            obs_node,
            type="post_apply_observation",
            label=str(obs.get("source") or "observation"),
            status=_status(obs.get("status") or obs.get("outcome")),
            summary=str(obs.get("summary") or obs.get("note") or ""),
            ts=str(obs.get("observed_at") or obs.get("ts") or ""),
            evidence_refs=_str_list(obs.get("evidence_refs")),
            metadata={
                "source": obs.get("source"),
                "run_id": obs.get("run_id"),
                "journal_ref": obs.get("journal_ref"),
            },
        )
        graph.add_edge(monitor_node, obs_node, type="observed_by", label="has observation")


def _add_action_gate_node(
    graph: _Graph,
    root_id: str,
    action_gates: dict[str, Any] | None,
) -> None:
    if not isinstance(action_gates, dict):
        return
    blockers = _str_list(action_gates.get("blockers"))
    warnings = _str_list(action_gates.get("warnings"))
    node_id = f"action_gates:{_path_id(root_id)}"
    graph.add_node(
        node_id,
        type="action_gates",
        label="Action gates",
        status="pass" if action_gates.get("can_apply") else "blocked" if blockers else "warning" if warnings else "pending",
        summary="Apply gates are clear." if action_gates.get("can_apply") else "; ".join(blockers[:3] or warnings[:3]),
        evidence_refs=_str_list((action_gates.get("evidence") or {}).get("refs")) if isinstance(action_gates.get("evidence"), dict) else [],
        metadata={
            "can_apply": action_gates.get("can_apply"),
            "blockers": blockers[:8],
            "warnings": warnings[:8],
            "state": action_gates.get("state"),
        },
    )
    graph.add_edge(node_id, root_id, type="gates", label="gates apply")


def _proposal_strategy(proposal: dict[str, Any]) -> str | None:
    metadata = proposal.get("metadata") if isinstance(proposal.get("metadata"), dict) else {}
    if metadata.get("strategy_id"):
        return str(metadata["strategy_id"])
    if proposal.get("strategy_id"):
        return str(proposal["strategy_id"])
    target = str(proposal.get("target") or "")
    parts = target.replace("\\", "/").split("/")
    if "strategies" in parts:
        idx = parts.index("strategies")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def _validation_summary(plan: dict[str, Any]) -> str:
    steps = _dict_list(plan.get("steps"))
    required = sum(1 for step in steps if step.get("required", True))
    if not steps:
        return ""
    return f"{len(steps)} validation step(s), {required} required."


def _validation_refs(value: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    validation = value.get("validation") if isinstance(value.get("validation"), dict) else value
    refs.extend(_str_list(validation.get("evidence_refs") if isinstance(validation, dict) else []))
    for step in _dict_list(value.get("steps")):
        if step.get("evidence_ref"):
            refs.append(str(step["evidence_ref"]))
    if value.get("last_run_id"):
        refs.append(f"validation:{value['last_run_id']}")
    return _unique_strings(refs)


def _compact_metadata(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in value.items():
        if item in (None, "", [], {}):
            continue
        if isinstance(item, (str, int, float, bool)):
            out[key] = _bounded(str(item), 240) if isinstance(item, str) else item
        elif isinstance(item, list):
            out[key] = [_bounded(str(x), 160) for x in item[:12]]
        elif isinstance(item, dict):
            compact = {
                str(k): _bounded(str(v), 160)
                for k, v in list(item.items())[:12]
                if isinstance(v, (str, int, float, bool))
            }
            if compact:
                out[key] = compact
    return out


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [x for x in (value or []) if isinstance(x, dict)]


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(x) for x in value if str(x or "").strip()]
    return []


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        s = str(value or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _status(value: Any) -> str:
    return str(value or "unknown").strip().lower() or "unknown"


def _label(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    return _bounded(text.replace("_", " "), 72)


def _bounded(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def _clean_id(value: Any) -> str:
    return str(value or "").strip()


def _clean_node_id(value: Any) -> str:
    text = str(value or "").strip()
    return text.replace(" ", "_")


def _stable_id(prefix: str, value: Any, idx: int) -> str:
    return f"{prefix}_{idx}_{_path_id(str(value or idx))}"


def _path_id(path: str) -> str:
    return hashlib.sha1(str(path or "").encode("utf-8")).hexdigest()[:12]
