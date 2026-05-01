"""Lightweight payload validation against an ``input_schema`` declaration.

The built-in schemas Nerya uses are JSON-Schema-shaped but do not need a
full validator — we only enforce the fields the runtime really depends
on:

* ``required``: a list of keys that must be present in the payload.
* ``properties``: per-key type hints. A ``type`` of one of
  ``string``, ``number``, ``integer``, ``boolean``, ``object``,
  ``array`` is enforced; anything else is accepted.

A payload that fails validation is rejected before the skill handler
runs, so schema mismatches surface at dispatch time rather than deep in
the trading kernel.
"""

from __future__ import annotations

from typing import Any


_TYPE_MAP = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
}


class SkillSchemaError(ValueError):
    """Raised when a skill payload does not match its ``input_schema``."""


def validate_payload(payload: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate ``payload`` against ``schema``. Raises ``SkillSchemaError``
    with a message that names the first failing field.

    An empty or missing schema is treated as "no validation" — this is
    intentional so that older manifests without a schema still work.
    """
    if not schema:
        return
    if not isinstance(payload, dict):
        raise SkillSchemaError(
            f"payload must be an object, got {type(payload).__name__}"
        )
    required = set(schema.get("required") or [])
    props = schema.get("properties") or {}

    def _type_hint(field: str) -> str:
        spec = props.get(field) if isinstance(props, dict) else None
        if not isinstance(spec, dict):
            return "?"
        t = spec.get("type")
        if isinstance(t, list):
            return "|".join(str(x) for x in t)
        return str(t or "?")

    def _required_summary() -> str:
        # Always render the FULL required list with types so a model can
        # fix its payload in a single retry instead of trial-and-error.
        if not required:
            return ""
        bits = sorted(f"{f}: {_type_hint(f)}" for f in required)
        return " (required fields: " + ", ".join(bits) + ")"

    for key in required:
        if key not in payload:
            raise SkillSchemaError(
                f"missing required field {key!r}" + _required_summary()
            )
    for key, spec in props.items():
        if key not in payload:
            continue
        value = payload[key]
        # ``None`` on an optional field is universally accepted so manifests
        # can keep declaring types without forcing every caller to strip
        # nullable fields. Required fields still cannot be null.
        if value is None:
            if key in required:
                raise SkillSchemaError(
                    f"required field {key!r} must not be null"
                    + _required_summary()
                )
            continue
        expected = spec.get("type") if isinstance(spec, dict) else None
        if not expected:
            continue
        # Allow JSON-Schema-style list-of-types, e.g. ["string", "null"].
        expected_list = [expected] if isinstance(expected, str) else list(expected)
        if "null" in expected_list:
            # already handled above for None; strip so the real check runs.
            expected_list = [t for t in expected_list if t != "null"]
        if not expected_list:
            continue
        # ``True`` and ``False`` are ints in Python; reject that cross-type
        # match for numeric/integer fields.
        if any(t in ("number", "integer") for t in expected_list) and isinstance(value, bool):
            raise SkillSchemaError(
                f"field {key!r} must be {expected_list!r}, got bool"
            )
        py_types: list[type | tuple[type, ...]] = []
        for t in expected_list:
            pt = _TYPE_MAP.get(t)
            if pt is not None:
                py_types.append(pt)
        if not py_types:
            continue
        # Flatten into a single tuple for isinstance.
        flat: list[type] = []
        for pt in py_types:
            if isinstance(pt, tuple):
                flat.extend(pt)
            else:
                flat.append(pt)
        if not isinstance(value, tuple(flat)):
            raise SkillSchemaError(
                f"field {key!r} must be {expected_list!r}, got {type(value).__name__}"
            )
