"""Conversation-friendly tools for declarative workspace UI customization.

The dashboard manifest already has a strict, proposal-only mutation boundary in
:mod:`nerya.workspace.ui`.  These tools expose that boundary to the native agent
without asking the model to rewrite a complete YAML document:

* ``workspace_ui_inspect`` reads the current manifest, revision and finite
  widget catalog.
* ``workspace_ui_propose`` accepts small, structured operations and creates a
  reviewable ``core_config_patch`` proposal.  It never changes the live UI.
"""

from __future__ import annotations

from typing import Any, Mapping

from ...core.config import Config
from ...workspace import ui as workspace_ui
from ..tool_errors import schema_validation_result
from ..types import ToolCall, ToolError, ToolErrorKind, ToolResult


WORKSPACE_UI_INSPECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "page_id": {
            "type": "string",
            "description": (
                "Optional page id to focus on. Use 'home' for the main dashboard. "
                "Omit it to inspect the complete manifest."
            ),
        },
        "include_manifest": {
            "type": "boolean",
            "default": True,
            "description": "Include the current declarative manifest in the result.",
        },
        "include_catalog": {
            "type": "boolean",
            "default": True,
            "description": "Include the allow-listed read-only widget catalog.",
        },
    },
    "additionalProperties": False,
}


WORKSPACE_UI_PROPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Concise operator-facing summary of the requested UI change.",
        },
        "operations": {
            "type": "array",
            "minItems": 1,
            "description": (
                "Incremental workspace UI operations. Supported operations: "
                "update_home with changes; add_widget/upsert_widget/update_widget/"
                "remove_widget with page='home' or a page id; add_page/upsert_page/"
                "update_page/remove_page; set_nav; reorder_widgets; reorder_pages. "
                "Prefer upsert_widget and upsert_page for conversational requests "
                "because they safely create or update the same stable id. Upserts "
                "preserve omitted fields and merge widget config/source or page nav "
                "mappings, so a small edit does not erase unrelated settings. A widget "
                "must use an allow-listed read-only kind from workspace_ui_inspect."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string"},
                },
                "required": ["op"],
                "additionalProperties": True,
            },
        },
        "rationale": {
            "type": "string",
            "description": "Optional review notes explaining why the change is useful.",
        },
        "base_revision": {
            "type": "integer",
            "description": (
                "Optional optimistic concurrency guard. When omitted, the tool binds "
                "the proposal to the current server revision automatically."
            ),
        },
        "base_digest": {
            "type": "string",
            "description": (
                "Optional current manifest digest. When omitted, the current digest "
                "is captured automatically."
            ),
        },
        "actor_id": {
            "type": "string",
            "description": "Optional actor/session identifier stored in proposal metadata.",
        },
    },
    "required": ["summary", "operations"],
    "additionalProperties": False,
}


def _record(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string(value: Any) -> str:
    return str(value or "").strip()


def _manifest_summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    home = _record(manifest.get("home"))
    home_widgets = [
        _record(widget)
        for widget in home.get("widgets", [])
        if isinstance(widget, Mapping)
    ]
    pages: list[dict[str, Any]] = []
    total_widgets = len(home_widgets)
    for raw_page in manifest.get("pages", []):
        if not isinstance(raw_page, Mapping):
            continue
        page = _record(raw_page)
        widgets = [
            _record(widget)
            for widget in page.get("widgets", [])
            if isinstance(widget, Mapping)
        ]
        total_widgets += len(widgets)
        pages.append(
            {
                "id": _string(page.get("id")),
                "title": _string(page.get("title")),
                "description": _string(page.get("description")),
                "nav": _record(page.get("nav")),
                "widget_count": len(widgets),
                "widgets": [
                    {
                        "id": _string(widget.get("id")),
                        "kind": _string(widget.get("kind")),
                        "title": _string(widget.get("title")),
                    }
                    for widget in widgets
                ],
            }
        )
    return {
        "home": {
            "title": _string(home.get("title")),
            "description": _string(home.get("description")),
            "widget_count": len(home_widgets),
            "widgets": [
                {
                    "id": _string(widget.get("id")),
                    "kind": _string(widget.get("kind")),
                    "title": _string(widget.get("title")),
                }
                for widget in home_widgets
            ],
        },
        "page_count": len(pages),
        "widget_count": total_widgets,
        "pages": pages,
    }


def _focused_manifest(manifest: Mapping[str, Any], page_id: str) -> dict[str, Any] | None:
    page_id = page_id.strip().lower()
    if not page_id:
        return dict(manifest)
    if page_id in {"home", "dashboard", "/"}:
        return {
            "version": manifest.get("version", 1),
            "home": _record(manifest.get("home")),
            "pages": [],
        }
    for raw_page in manifest.get("pages", []):
        if isinstance(raw_page, Mapping) and _string(raw_page.get("id")) == page_id:
            return {
                "version": manifest.get("version", 1),
                "home": {"widgets": []},
                "pages": [dict(raw_page)],
            }
    return None


def _error_result(
    call: ToolCall,
    *,
    kind: ToolErrorKind,
    message: str,
    detail: Mapping[str, Any] | None = None,
    recovery_hint: Mapping[str, Any] | None = None,
) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(
            kind=kind,
            message=message,
            detail=dict(detail or {}),
            retryable=kind in {ToolErrorKind.CONFLICT, ToolErrorKind.EXECUTION_ERROR},
            recovery_hint=dict(recovery_hint or {}),
        ),
    )


def workspace_ui_inspect_handler(call: ToolCall, *, config: Config) -> ToolResult:
    """Return the current declarative layout and safe widget catalog."""

    args = call.arguments or {}
    current = workspace_ui.read(config.paths)
    if not current.get("ok"):
        return _error_result(
            call,
            kind=ToolErrorKind.EXECUTION_ERROR,
            message="workspace UI manifest is invalid",
            detail={
                "path": current.get("path"),
                "errors": list(current.get("errors") or []),
                "warnings": list(current.get("warnings") or []),
            },
            recovery_hint={"action": "repair_workspace_ui_manifest"},
        )

    manifest = _record(current.get("manifest"))
    requested_page = _string(args.get("page_id"))
    focused = _focused_manifest(manifest, requested_page)
    if focused is None:
        page_ids = [
            _string(page.get("id"))
            for page in manifest.get("pages", [])
            if isinstance(page, Mapping)
        ]
        return _error_result(
            call,
            kind=ToolErrorKind.NOT_FOUND,
            message=f"workspace UI page {requested_page!r} was not found",
            detail={"page_id": requested_page, "available_pages": page_ids},
            recovery_hint={"tool": "workspace_ui_inspect", "page_id": ""},
        )

    data: dict[str, Any] = {
        "ok": True,
        "status": current.get("status"),
        "path": current.get("path"),
        "source": current.get("source"),
        "revision": int(current.get("revision") or 0),
        "digest": _string(current.get("digest")),
        "summary": _manifest_summary(manifest),
        "selected_page": requested_page or None,
        "warnings": list(current.get("warnings") or []),
        "next_required_action": (
            "Call workspace_ui_propose with incremental operations when the operator "
            "has requested a persistent change."
        ),
    }
    if args.get("include_manifest", True) is not False:
        data["manifest"] = focused
    if args.get("include_catalog", True) is not False:
        data["catalog"] = current.get("catalog") or {}
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data)


def _affected_resources(operations: list[Mapping[str, Any]]) -> dict[str, Any]:
    pages: set[str] = set()
    widgets: set[str] = set()
    touches_home = False
    touches_navigation = False
    operation_names: list[str] = []

    for operation in operations:
        op = _string(operation.get("op") or operation.get("action")).lower()
        if op:
            operation_names.append(op)
        page_value = operation.get("page") or operation.get("target")
        if isinstance(page_value, Mapping):
            page_id = _string(page_value.get("id"))
            if page_id:
                pages.add(page_id)
            for widget in page_value.get("widgets", []):
                if isinstance(widget, Mapping) and _string(widget.get("id")):
                    widgets.add(_string(widget.get("id")))
        else:
            page_id = _string(page_value).lower()
            if page_id in {"", "home", "dashboard", "/"}:
                if op.startswith(("add_widget", "upsert_widget", "update_widget", "remove_widget", "reorder_widgets")):
                    touches_home = True
            elif page_id:
                pages.add(page_id)

        explicit_page_id = _string(operation.get("page_id") or operation.get("id"))
        if op.startswith(("add_page", "upsert_page", "update_page", "remove_page", "set_nav", "page.")) and explicit_page_id:
            pages.add(explicit_page_id)

        widget_value = operation.get("widget") or operation.get("value")
        if isinstance(widget_value, Mapping) and _string(widget_value.get("id")):
            widgets.add(_string(widget_value.get("id")))
        explicit_widget_id = _string(operation.get("widget_id"))
        if op.startswith(("add_widget", "upsert_widget", "update_widget", "remove_widget", "widget.")):
            if explicit_widget_id:
                widgets.add(explicit_widget_id)
            elif _string(operation.get("id")):
                widgets.add(_string(operation.get("id")))

        if op in {"update_home", "home.update"}:
            touches_home = True
        if op in {
            "set_nav",
            "page.nav",
            "reorder_pages",
            "pages.reorder",
            "add_page",
            "page.add",
            "upsert_page",
            "page.upsert",
            "remove_page",
            "page.remove",
        }:
            touches_navigation = True

    return {
        "home": touches_home,
        "navigation": touches_navigation,
        "pages": sorted(pages),
        "widgets": sorted(widgets),
        "operation_count": len(operations),
        "operation_names": operation_names,
    }


def workspace_ui_propose_handler(call: ToolCall, *, config: Config) -> ToolResult:
    """Create a review-only UI proposal from small structured operations."""

    args = call.arguments or {}
    summary = _string(args.get("summary"))
    if not summary:
        return schema_validation_result(call, "summary is required")
    raw_operations = args.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        return schema_validation_result(call, "operations must be a non-empty list")
    operations: list[dict[str, Any]] = []
    for index, raw_operation in enumerate(raw_operations):
        if not isinstance(raw_operation, Mapping):
            return schema_validation_result(
                call, f"operations[{index}] must be an object with an op field",
            )
        operation = dict(raw_operation)
        if not _string(operation.get("op") or operation.get("action")):
            return schema_validation_result(call, f"operations[{index}].op is required")
        operations.append(operation)

    current = workspace_ui.read(config.paths)
    if not current.get("ok"):
        return _error_result(
            call,
            kind=ToolErrorKind.EXECUTION_ERROR,
            message="workspace UI manifest is invalid and cannot be patched",
            detail={
                "path": current.get("path"),
                "errors": list(current.get("errors") or []),
            },
            recovery_hint={"tool": "workspace_ui_inspect"},
        )

    payload: dict[str, Any] = {
        "summary": summary,
        "rationale": _string(args.get("rationale")),
        "patch": {"operations": operations},
        "base_revision": (
            args["base_revision"]
            if args.get("base_revision") is not None
            else int(current.get("revision") or 0)
        ),
        "base_digest": _string(args.get("base_digest")) or _string(current.get("digest")),
    }
    actor_id = _string(args.get("actor_id"))
    if actor_id:
        payload["actor_id"] = actor_id

    result = workspace_ui.propose(config.paths, payload)
    if not result.get("ok"):
        status = int(result.get("_status") or 400)
        kind = ToolErrorKind.EXECUTION_ERROR
        if status == 400:
            kind = ToolErrorKind.SCHEMA_VALIDATION
        elif status == 404:
            kind = ToolErrorKind.NOT_FOUND
        elif status == 409:
            kind = ToolErrorKind.CONFLICT
        errors = [str(item) for item in result.get("errors") or [] if str(item)]
        message = errors[0] if errors else "workspace UI proposal failed"
        return _error_result(
            call,
            kind=kind,
            message=message,
            detail={
                "status": status,
                "errors": errors,
                "detail": result.get("detail"),
                "revision": result.get("revision"),
                "digest": result.get("digest"),
            },
            recovery_hint={
                "tool": "workspace_ui_inspect",
                "action": "refresh_layout_and_retry",
            },
        )

    proposal_id = _string(result.get("proposal_id"))
    if not proposal_id:
        return _error_result(
            call,
            kind=ToolErrorKind.EXECUTION_ERROR,
            message="workspace UI proposal was created without an id",
            detail={"status": result.get("status")},
        )

    diff_record = _record(result.get("diff"))
    diff_text = _string(diff_record.get("text")) or _string(result.get("diff"))
    diff_truncated = len(diff_text) > 20_000
    if diff_truncated:
        diff_text = diff_text[:20_000] + "\n... [diff truncated]"
    affected = _affected_resources(operations)
    state = _string(result.get("state")) or "pending_review"
    data = {
        "ok": True,
        "resource_kind": "workspace_ui",
        "proposal_id": proposal_id,
        "proposal": {
            "id": proposal_id,
            "kind": "core_config_patch",
            "state": state,
            "summary": summary,
            "target": workspace_ui.UI_RELATIVE_PATH,
            "metadata": {
                "workspace_ui": True,
                "base_revision": int(current.get("revision") or 0),
                "base_digest": _string(current.get("digest")),
                "affected": affected,
            },
        },
        "state": state,
        "summary": summary,
        "target": workspace_ui.UI_RELATIVE_PATH,
        "base_revision": int(current.get("revision") or 0),
        "candidate_revision": int(result.get("revision") or int(current.get("revision") or 0) + 1),
        "affected": affected,
        "operations": operations,
        "diff": diff_text,
        "diff_truncated": diff_truncated,
        "warnings": list(result.get("warnings") or []),
        "next_required_action": "review_approve_and_apply_proposal",
    }
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data)


__all__ = [
    "WORKSPACE_UI_INSPECT_SCHEMA",
    "WORKSPACE_UI_PROPOSE_SCHEMA",
    "workspace_ui_inspect_handler",
    "workspace_ui_propose_handler",
]

