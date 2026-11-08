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

import re
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
        "proposal_id": {
            "type": "string",
            "description": (
                "Optional exact existing UI proposal id to recover its read-only "
                "candidate preview. This never applies or creates a proposal."
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
                "must use an allow-listed read-only kind from workspace_ui_inspect. "
                "Page and widget ids must match [a-z][a-z0-9_-]*; dots and leading "
                "underscores are invalid. Load coding/references/native-panels.md "
                "for the operation and source format before proposing. "
                "For quote/account panels use market_ticker with config.venue and "
                "config.market (price and provider-reported 24h change/percentage; "
                "config.show_change defaults to true, missing fields show unknown), "
                "chart for trends, and portfolio for account totals. Sources stay on "
                "the server; inspect/propose alone do not verify dynamic data."
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


def _unchecked_data_validation() -> dict[str, str]:
    return {
        "status": "not_checked",
        "detail": (
            "Layout validation only: no quote or account source was queried. "
            "Data availability, freshness and paper/live account modes remain "
            "unverified; not_checked is not a data blocker or a zero balance."
        ),
    }


def _record(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string(value: Any) -> str:
    return str(value or "").strip()


def _page_href(page_id: Any) -> str:
    page = _string(page_id)
    return f"/workspace/pages/{page}" if page else ""


def _manifest_with_page_hrefs(manifest: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(manifest)
    pages: list[dict[str, Any]] = []
    for raw_page in manifest.get("pages", []):
        if not isinstance(raw_page, Mapping):
            continue
        page = dict(raw_page)
        href = _page_href(page.get("id"))
        if href:
            page["href"] = href
        pages.append(page)
    result["pages"] = pages
    return result


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
                "href": _page_href(page.get("id")),
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


def _preview_handoff(proposal_id: str) -> dict[str, Any]:
    preview_url = f"/workspace/pages/preview?proposal_id={proposal_id}"
    return {
        "preview_url": preview_url,
        "preview_href": preview_url,
        "preview_action": {
            "id": "preview",
            "label": "Preview panel",
            "href": preview_url,
        },
    }


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


def _manifest_validation_diagnostics(errors: list[Any]) -> dict[str, Any]:
    # Validator strings can embed caller-owned ids, kinds and arbitrary keys.
    # Return only schema vocabulary, never raw values or a truncated payload.
    schema_fields = {
        "manifest", "version", "revision", "home", "pages", "widgets",
        "id", "kind", "title", "description", "icon", "nav", "label",
        "order", "section", "hidden", "span", "config", "source",
        "read_only", "text", "provider", "action", "params", "capability",
    }
    reason_pattern = (
        r"(is required|is not allowed|is not registered|is reserved|"
        r"must (?:be (?:a string|a mapping|a list|a mapping or source id|"
        r"a safe source id|finite|an internal route or identifier|"
        r"full/half/third or an integer 1\.\.12|between 1 and 12|"
        r"an integer between -10000 and 10000|primary or advanced|boolean|"
        r"true for dashboard widgets|true|[0-9]{1,5})|"
        r"match \[a-z\]\[a-z0-9_-\]\*|contain only JSON-like values)|"
        r"exceeds (?:nesting depth [0-9]{1,5}|[0-9]{1,5} characters)|"
        r"has more than [0-9]{1,5} (?:keys|items|widgets|pages)|"
        r"has an unsafe key|contains executable markup|"
        r"needs id, action, or capability)$"
    )
    issues = []
    for raw in errors[:8]:
        text = raw if isinstance(raw, str) else ""
        field_match = re.match(r"manifest(?=[.\s]|$)([^ ]*)", text)
        field = "manifest"
        if field_match:
            for token in re.findall(r"\.[^.\[\s]+|\[[0-9]+\]", field_match.group(1)):
                if token.startswith("[") and len(token) <= 6:
                    field += token
                elif token[1:] in schema_fields:
                    field += token
                else:
                    field += ".[redacted]"
                    break
        reason_match = re.search(reason_pattern, text)
        reason = reason_match.group(1) if reason_match else "invalid schema value"
        if " contains duplicate " in text:
            reason = "contains duplicate identifier"
        issues.append({"field": field[:160], "reason": reason})
    return {
        "validation_errors": issues,
        "validation_errors_truncated": len(errors) > len(issues),
        "supported_widget_kinds": sorted(workspace_ui.WIDGET_KINDS),
        "config_schema_pointer": (
            "nerya/workspace/ui.py::_normalize_widget / _safe_value; "
            "nerya/skills/builtin/coding/references/native-panels.md"
        ),
    }


def workspace_ui_inspect_handler(call: ToolCall, *, config: Config) -> ToolResult:
    """Return the current declarative layout and safe widget catalog."""

    args = call.arguments or {}
    proposal_id = args.get("proposal_id")
    if proposal_id is not None:
        if not isinstance(proposal_id, str) or not proposal_id:
            return schema_validation_result(call, "proposal_id must be a non-empty exact string")
        preview = workspace_ui.preview(config.paths, proposal_id)
        if not preview.get("ok"):
            status = int(preview.get("_status") or 404)
            kind = ToolErrorKind.NOT_FOUND if status == 404 else ToolErrorKind.SCHEMA_VALIDATION
            if status not in {400, 404}:
                kind = ToolErrorKind.EXECUTION_ERROR
            return _error_result(
                call,
                kind=kind,
                message="existing workspace UI proposal preview could not be read",
                detail={
                    "proposal_id": proposal_id,
                    "status": status,
                    "error": preview.get("error"),
                    "errors": list(preview.get("errors") or []),
                },
                recovery_hint={"action": "use_the_exact_producer_returned_proposal_id"},
            )
        recovered_manifest = _record(preview.get("manifest"))
        data = {
            "ok": True,
            "source": "proposal",
            "path": preview.get("path", workspace_ui.UI_RELATIVE_PATH),
            "proposal_id": proposal_id,
            "state": _string(preview.get("state")) or "pending_review",
            "applied": bool(preview.get("applied")),
            "status": _string(preview.get("state")) or "pending_review",
            "revision": int(preview.get("revision") or 0),
            "digest": _string(preview.get("digest")),
            "summary": _manifest_summary(recovered_manifest),
            "selected_page": None,
            "warnings": list(preview.get("warnings") or []),
            "structural_validation": preview.get("structural_validation", "passed"),
            "data_validation": _unchecked_data_validation(),
            "manifest": recovered_manifest,
            "catalog": workspace_ui.catalog(),
            "next_required_action": "preview_and_review_existing_proposal",
        }
        data.update(_preview_handoff(proposal_id))
        return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data)

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
        "data_validation": _unchecked_data_validation(),
        "next_required_action": (
            "Call workspace_ui_propose with incremental operations when the operator "
            "has requested a persistent change."
        ),
    }
    if args.get("include_manifest", True) is not False:
        data["manifest"] = _manifest_with_page_hrefs(focused)
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
        validation_errors = _record(result.get("detail")).get("errors")
        if kind == ToolErrorKind.SCHEMA_VALIDATION and isinstance(validation_errors, list):
            diagnostics = _manifest_validation_diagnostics(validation_errors)
            issues = diagnostics["validation_errors"]
            return _error_result(
                call,
                kind=kind,
                message=(
                    "manifest validation failed: "
                    + "; ".join(f"{row['field']}: {row['reason']}" for row in issues)
                    + ". Supported widget kinds: "
                    + ", ".join(diagnostics["supported_widget_kinds"])
                    + ". Config schema/reference: " + diagnostics["config_schema_pointer"]
                    + ". Revise only invalid fields; preserve the requested change."
                ),
                detail={
                    "status": status,
                    "errors": ["manifest validation failed"],
                    "detail": {"errors": [f"{row['field']}: {row['reason']}" for row in issues]},
                    "revision": result.get("revision"),
                    "digest": result.get("digest"),
                    **diagnostics,
                },
                recovery_hint={"action": "revise_operations_against_schema"},
            )
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

    proposal_id = result.get("proposal_id")
    if not isinstance(proposal_id, str) or not proposal_id:
        return _error_result(
            call,
            kind=ToolErrorKind.EXECUTION_ERROR,
            message="workspace UI proposal was created without an id",
            detail={"status": result.get("status")},
        )

    # Validate the exact persisted candidate before handing out a navigation URL.
    preview = workspace_ui.preview(config.paths, proposal_id)
    if not preview.get("ok"):
        return _error_result(
            call,
            kind=ToolErrorKind.EXECUTION_ERROR,
            message="workspace UI proposal has no valid read-only preview",
            detail={"proposal_id": proposal_id, "error": preview.get("error")},
            recovery_hint={"action": "inspect_existing_proposal"},
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
        **_preview_handoff(proposal_id),
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
        "applied": False,
        "data_validation": _unchecked_data_validation(),
        "summary": summary,
        "target": workspace_ui.UI_RELATIVE_PATH,
        "base_revision": int(current.get("revision") or 0),
        "candidate_revision": int(result.get("revision") or int(current.get("revision") or 0) + 1),
        "affected": affected,
        "operations": operations,
        "diff": diff_text,
        "diff_truncated": diff_truncated,
        "warnings": list(result.get("warnings") or []),
        "next_required_action": "preview_and_review_proposal",
    }
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data)


__all__ = [
    "WORKSPACE_UI_INSPECT_SCHEMA",
    "WORKSPACE_UI_PROPOSE_SCHEMA",
    "workspace_ui_inspect_handler",
    "workspace_ui_propose_handler",
]

