"""Tool input-validation error rendering.

Render schema-validation failures as short plain-English issue lists.
A Python-dict dump of the JSON schema is unreadable to most models;
one-sentence-per-issue prose renders reliably across Claude, DeepSeek,
Qwen, OpenAI, and Gemini alike.

The output is intentionally plain prose that lands inside a
``<tool_use_error>`` wrapper in the resulting ``tool_result`` block,
so every supported model reads the same shape and knows to re-issue
the same tool call with a corrected payload.
"""

from __future__ import annotations

from typing import Any, Iterable


__all__ = [
    "collect_schema_issues",
    "format_schema_validation_error",
    "SchemaIssue",
]


# ---------------------------------------------------------------------------
# Public data shape
# ---------------------------------------------------------------------------


class SchemaIssue(dict):
    """Typed dict wrapper so telemetry can round-trip issues as JSON.

    Keys:

    * ``kind``      — ``missing`` | ``unexpected`` | ``type`` | ``enum``.
    * ``field``     — dotted path, e.g. ``todos[0].activeForm``.
    * ``expected``  — expected type/enum (present for ``type``/``enum``).
    * ``actual``    — observed type (present for ``type`` only).
    """


# ---------------------------------------------------------------------------
# Path formatting (mirrors toolErrors.ts:formatValidationPath)
# ---------------------------------------------------------------------------


def _format_path(path: Iterable[Any]) -> str:
    """Turn ``['todos', 0, 'activeForm']`` into ``todos[0].activeForm``."""

    out = ""
    for i, seg in enumerate(path):
        if isinstance(seg, int):
            out += f"[{seg}]"
        else:
            out += str(seg) if i == 0 else f".{seg}"
    return out


# ---------------------------------------------------------------------------
# Type helpers
# ---------------------------------------------------------------------------


def _jsonschema_type_of(value: Any) -> str:
    """Return the JSON-schema ``type`` string for a Python value."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _type_matches(expected: str, value: Any) -> bool:
    """Return True if ``value`` satisfies the JSON-schema ``expected`` type.

    JSON Schema allows a ``number`` slot to accept integers, so we
    mirror that here. Everything else is strict.
    """

    actual = _jsonschema_type_of(value)
    if expected == actual:
        return True
    if expected == "number" and actual == "integer":
        return True
    return False


# ---------------------------------------------------------------------------
# Issue collection
# ---------------------------------------------------------------------------


def collect_schema_issues(
    payload: Any,
    schema: dict[str, Any],
    *,
    path: tuple[Any, ...] = (),
) -> list[SchemaIssue]:
    """Walk ``payload`` against ``schema`` and return every violation.

    Implements the same useful subset as the legacy
    ``_validate_against_schema`` (type / required / enum on direct
    properties) but returns a structured list instead of stopping at
    the first error. ``format_schema_validation_error`` turns that
    list into the English text the model sees.
    """

    issues: list[SchemaIssue] = []
    if not schema:
        return issues

    expected = schema.get("type")
    if expected == "object":
        if not isinstance(payload, dict):
            issues.append(SchemaIssue(
                kind="type",
                field=_format_path(path) or "<root>",
                expected="object",
                actual=_jsonschema_type_of(payload),
            ))
            return issues

        for key in schema.get("required") or []:
            if key not in payload:
                issues.append(SchemaIssue(
                    kind="missing",
                    field=_format_path((*path, key)),
                ))

        props = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        if additional is False:
            for key in payload:
                if key not in props:
                    issues.append(SchemaIssue(
                        kind="unexpected",
                        field=_format_path((*path, key)),
                    ))

        for key, sub in props.items():
            if key not in payload:
                continue
            sub_expected = sub.get("type")
            value = payload[key]
            field_path = _format_path((*path, key))
            if sub_expected and not _type_matches(sub_expected, value):
                issues.append(SchemaIssue(
                    kind="type",
                    field=field_path,
                    expected=sub_expected,
                    actual=_jsonschema_type_of(value),
                ))
                # Don't recurse when the type is already wrong — the
                # downstream checks would double-count.
                continue
            enum = sub.get("enum")
            if enum and value not in enum:
                issues.append(SchemaIssue(
                    kind="enum",
                    field=field_path,
                    expected=enum,
                    actual=value,
                ))
        return issues

    # Primitive root schemas (rare in our tool registry, but keep the
    # shape consistent so future tool authors get useful errors).
    if expected and not _type_matches(expected, payload):
        issues.append(SchemaIssue(
            kind="type",
            field=_format_path(path) or "<root>",
            expected=expected,
            actual=_jsonschema_type_of(payload),
        ))
    return issues


# ---------------------------------------------------------------------------
# Rendering (mirrors toolErrors.ts:formatZodValidationError)
# ---------------------------------------------------------------------------


def format_schema_validation_error(
    tool_name: str,
    issues: list[SchemaIssue],
) -> str:
    """Render a list of issues as one English sentence per row.

    Example output::

        strategy_generate_proposal failed due to the following issues:
        The required parameter `strategy_id` is missing
        The required parameter `markets` is missing

    A single-issue list uses the singular "issue"; multi-issue lists
    use the plural "issues".
    """

    if not issues:
        return f"{tool_name} failed schema validation (no detail available)"

    parts: list[str] = []
    for issue in issues:
        kind = issue.get("kind")
        field = issue.get("field") or "<unknown>"
        if kind == "missing":
            parts.append(f"The required parameter `{field}` is missing")
        elif kind == "unexpected":
            parts.append(f"An unexpected parameter `{field}` was provided")
        elif kind == "type":
            parts.append(
                f"The parameter `{field}` type is expected as "
                f"`{issue.get('expected')}` but provided as "
                f"`{issue.get('actual')}`"
            )
        elif kind == "enum":
            parts.append(
                f"The parameter `{field}` must be one of "
                f"{issue.get('expected')!r} (got "
                f"{issue.get('actual')!r})"
            )
        else:
            parts.append(f"`{field}` failed validation")

    joiner = "issues" if len(parts) > 1 else "issue"
    return (
        f"{tool_name} failed due to the following {joiner}:\n"
        + "\n".join(parts)
    )
