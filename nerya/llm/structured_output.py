"""Structured output validator. Extracts JSON from an LLM response and,
when a schema is supplied, enforces it. An enforced schema violation
raises `LLMStructuredOutputError` so callers can surface provenance.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.errors import LLMStructuredOutputError

try:
    import jsonschema
    _HAS_JSONSCHEMA = True
except Exception:  # pragma: no cover
    _HAS_JSONSCHEMA = False


def parse(raw: str, *, schema: dict | None = None, strict: bool = True) -> Any:
    payload = _extract_json(raw)
    if schema is not None:
        if _HAS_JSONSCHEMA:
            try:
                jsonschema.validate(instance=payload, schema=schema)
            except Exception as exc:  # validation error, format error, etc.
                if strict:
                    raise LLMStructuredOutputError(
                        f"schema validation failed: {exc}"
                    ) from exc
        elif strict:
            # without jsonschema installed we still enforce minimal shape
            if not _shape_ok(payload, schema):
                raise LLMStructuredOutputError(
                    "schema validation failed: jsonschema not installed and minimal shape check failed"
                )
    return payload


def _shape_ok(payload: Any, schema: dict) -> bool:
    expected = (schema or {}).get("type")
    if expected == "object" and not isinstance(payload, dict):
        return False
    if expected == "array" and not isinstance(payload, list):
        return False
    required = (schema or {}).get("required") or []
    if isinstance(payload, dict):
        for key in required:
            if key not in payload:
                return False
    return True


def _extract_json(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    if text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except Exception:
            pass
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, flags=re.S)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"(\{.*\}|\[.*\])", text, flags=re.S)
    if m:
        return json.loads(m.group(1))
    return {"raw": text}
