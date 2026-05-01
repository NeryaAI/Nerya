"""Operator-facing API envelope helpers.

Every response from the operator-facing BFF (``routes_operator``,
``routes_inbox``, ``routes_agent_tasks``) follows the same envelope so
the dashboard can render statuses, primary actions, and source links
without writing per-endpoint adapters.

The shape::

    {
      "ok": True,
      "status": "ok" | "warn" | "error" | "blocked",
      "severity": "info" | "warn" | "danger",
      "summary": "human readable summary",
      "primary_action": {"id": ..., "label": ..., "href": ...} | None,
      "next_actions": [...],
      "source_refs": [...],
      "debug_refs": [...],
      "data": {...},
    }

Helpers in this module:

* :func:`ok` / :func:`warn` / :func:`error` / :func:`blocked` —
  shorthand constructors that fill in sensible defaults.
* :func:`action` — build an action dict.
* :func:`source_ref` / :func:`debug_ref` — build a reference dict.
* :func:`merge_data` — merge multiple sub-dicts into the ``data``
  field of an existing envelope (used by ``/operator/overview``
  which composes several sub-envelopes).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional


__all__ = [
    "Envelope",
    "Severity",
    "Status",
    "action",
    "blocked",
    "debug_ref",
    "envelope",
    "error",
    "merge_data",
    "ok",
    "source_ref",
    "warn",
]


Severity = str  # "info" | "warn" | "danger"
Status = str  # "ok" | "warn" | "error" | "blocked"
Envelope = dict[str, Any]


def envelope(
    *,
    status: Status,
    severity: Severity,
    summary: str,
    data: Optional[Mapping[str, Any]] = None,
    primary_action: Optional[Mapping[str, Any]] = None,
    next_actions: Optional[Iterable[Mapping[str, Any]]] = None,
    source_refs: Optional[Iterable[Mapping[str, Any]]] = None,
    debug_refs: Optional[Iterable[Mapping[str, Any]]] = None,
    ok_flag: bool = True,
) -> Envelope:
    """Build the canonical operator envelope.

    Returns a fresh dict — callers can safely mutate ``data`` after the
    fact (eg. to attach a list of items computed inline).
    """

    return {
        "ok": bool(ok_flag),
        "status": status,
        "severity": severity,
        "summary": summary,
        "primary_action": dict(primary_action) if primary_action else None,
        "next_actions": [dict(a) for a in (next_actions or ())],
        "source_refs": [dict(r) for r in (source_refs or ())],
        "debug_refs": [dict(r) for r in (debug_refs or ())],
        "data": dict(data or {}),
    }


def ok(
    summary: str,
    *,
    data: Optional[Mapping[str, Any]] = None,
    primary_action: Optional[Mapping[str, Any]] = None,
    next_actions: Optional[Iterable[Mapping[str, Any]]] = None,
    source_refs: Optional[Iterable[Mapping[str, Any]]] = None,
    debug_refs: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Envelope:
    return envelope(
        status="ok",
        severity="info",
        summary=summary,
        data=data,
        primary_action=primary_action,
        next_actions=next_actions,
        source_refs=source_refs,
        debug_refs=debug_refs,
    )


def warn(
    summary: str,
    *,
    data: Optional[Mapping[str, Any]] = None,
    primary_action: Optional[Mapping[str, Any]] = None,
    next_actions: Optional[Iterable[Mapping[str, Any]]] = None,
    source_refs: Optional[Iterable[Mapping[str, Any]]] = None,
    debug_refs: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Envelope:
    return envelope(
        status="warn",
        severity="warn",
        summary=summary,
        data=data,
        primary_action=primary_action,
        next_actions=next_actions,
        source_refs=source_refs,
        debug_refs=debug_refs,
    )


def error(
    summary: str,
    *,
    data: Optional[Mapping[str, Any]] = None,
    primary_action: Optional[Mapping[str, Any]] = None,
    next_actions: Optional[Iterable[Mapping[str, Any]]] = None,
    source_refs: Optional[Iterable[Mapping[str, Any]]] = None,
    debug_refs: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Envelope:
    return envelope(
        status="error",
        severity="danger",
        summary=summary,
        ok_flag=False,
        data=data,
        primary_action=primary_action,
        next_actions=next_actions,
        source_refs=source_refs,
        debug_refs=debug_refs,
    )


def blocked(
    summary: str,
    *,
    data: Optional[Mapping[str, Any]] = None,
    primary_action: Optional[Mapping[str, Any]] = None,
    next_actions: Optional[Iterable[Mapping[str, Any]]] = None,
    source_refs: Optional[Iterable[Mapping[str, Any]]] = None,
    debug_refs: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Envelope:
    return envelope(
        status="blocked",
        severity="warn",
        summary=summary,
        ok_flag=False,
        data=data,
        primary_action=primary_action,
        next_actions=next_actions,
        source_refs=source_refs,
        debug_refs=debug_refs,
    )


def action(
    id: str,
    label: str,
    *,
    href: Optional[str] = None,
    method: Optional[str] = None,
    body: Optional[Mapping[str, Any]] = None,
    requires_scope: Optional[str] = None,
    disabled_reason: Optional[str] = None,
    severity: Optional[Severity] = None,
) -> dict[str, Any]:
    """Build an action descriptor.

    ``href`` is a frontend route or backend path; ``method``/``body``
    describe an API call when the action is a POST. The dashboard
    decides whether to render the action as a button (mutation) or a
    link (navigation).
    """

    out: dict[str, Any] = {"id": id, "label": label}
    if href:
        out["href"] = href
    if method:
        out["method"] = method.upper()
    if body is not None:
        out["body"] = dict(body)
    if requires_scope:
        out["requires_scope"] = requires_scope
    if disabled_reason:
        out["disabled_reason"] = disabled_reason
    if severity:
        out["severity"] = severity
    return out


def source_ref(
    kind: str,
    id: str,
    *,
    label: Optional[str] = None,
    href: Optional[str] = None,
) -> dict[str, Any]:
    """Build a source reference (eg. ``strategy:eth_mean_reversion``)."""

    out: dict[str, Any] = {"kind": kind, "id": id}
    if label:
        out["label"] = label
    if href:
        out["href"] = href
    return out


def debug_ref(
    kind: str,
    id: str,
    *,
    label: Optional[str] = None,
    href: Optional[str] = None,
) -> dict[str, Any]:
    """Build a debug reference (eg. ``trace:turn_xyz``)."""

    return source_ref(kind, id, label=label, href=href)


def merge_data(envelope_dict: Envelope, **sections: Any) -> Envelope:
    """Merge new sections into ``envelope_dict.data`` (in place).

    Skips ``None`` values so callers can pass optional sections without
    polluting the response.
    """

    bucket = envelope_dict.setdefault("data", {})
    for key, value in sections.items():
        if value is None:
            continue
        bucket[key] = value
    return envelope_dict
