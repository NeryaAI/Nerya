"""Safe, declarative workspace UI manifests.

The dashboard shell is compiled code, but operators still need a durable way
to add a read-only panel or a page from an agent conversation.  This module is
the small boundary between those two worlds.  It stores a versioned YAML
manifest and exposes only a deliberately finite widget vocabulary; no React,
JavaScript, HTML, or arbitrary URL can enter the renderer through this file.

The canonical file is ``ui/workspace.yml``.  ``workspace/ui.yml`` was used by
an early prototype and remains a read-only migration alias so an operator does
not lose a layout while upgrading.

Mutations are proposal-only.  :func:`propose` writes an
``evolution/proposals/<id>`` candidate and :func:`apply` delegates the actual
write to the existing candidate-bundle/approval promotion pipeline.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..core import yaml_io
from ..core.paths import WorkspacePaths


UI_SCHEMA_VERSION = 1
UI_RELATIVE_PATH = "ui/workspace.yml"
UI_LEGACY_RELATIVE_PATH = "workspace/ui.yml"
UI_MAX_BYTES = 256 * 1024
UI_MAX_PAGES = 64
UI_MAX_WIDGETS_PER_CONTAINER = 96
UI_MAX_CONFIG_DEPTH = 8
UI_MAX_CONFIG_KEYS = 64
UI_MAX_CONFIG_LIST = 128

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ACTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/-]{0,127}$")
_SAFE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_DANGEROUS_KEYS = frozenset(
    {
        "html",
        "raw_html",
        "javascript",
        "script",
        "scripts",
        "code",
        "component",
        "module",
        "remote_component",
        "iframe",
        "eval",
        "onload",
        "onclick",
        "onerror",
    }
)
_RESERVED_PAGE_IDS = frozenset(
    {
        "home",
        "chat",
        "dashboard",
        "settings",
        "portfolio",
        "strategies",
        "agents",
        "skills",
        "inbox",
    }
)


# This list is intentionally finite.  A skill can contribute a descriptor to
# the capability catalog in the future, but it cannot smuggle executable code
# into a manifest or ask the shell to import a component at runtime.
WIDGET_KIND_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "kind": "kpi",
        "title": "KPI",
        "description": "A single read-only metric with an optional trend.",
        "read_only": True,
        "source": "operator.overview",
    },
    {
        "kind": "metric",
        "title": "Metric",
        "description": "A compact operator-authored metric tile.",
        "read_only": True,
    },
    {
        "kind": "chart",
        "title": "Chart",
        "description": "A read-only time-series or candle chart.",
        "read_only": True,
        "source": "market.candles",
    },
    {
        "kind": "market_ticker",
        "title": "Market ticker",
        "description": "A read-only ticker for an allow-listed public venue.",
        "read_only": True,
        "source": "market.ticker",
    },
    {
        "kind": "portfolio",
        "title": "Portfolio snapshot",
        "description": "Paper/live account totals and open-position count.",
        "read_only": True,
        "source": "portfolio.summary",
    },
    {
        "kind": "strategy_table",
        "title": "Strategy table",
        "description": "A bounded read-only list of strategy status and PnL.",
        "read_only": True,
        "source": "strategy.list",
    },
    {
        "kind": "table",
        "title": "Table",
        "description": "A bounded read-only table with operator-authored rows.",
        "read_only": True,
    },
    {
        "kind": "attention",
        "title": "Attention",
        "description": "Approvals, alerts, and pending operator actions.",
        "read_only": True,
        "source": "operator.overview",
    },
    {
        "kind": "agent_panel",
        "title": "Agent panel",
        "description": "A read-only view of registered Agent Team roles.",
        "read_only": True,
        "source": "teams.roles",
    },
    {
        "kind": "markdown",
        "title": "Markdown",
        "description": "Operator-authored plain markdown (sanitized by the UI).",
        "read_only": True,
    },
    {
        "kind": "link",
        "title": "Shortcut",
        "description": "A link to another internal Nerya surface.",
        "read_only": True,
    },
    {
        "kind": "skill_panel",
        "title": "Skill panel",
        "description": "A read-only view backed by an explicitly registered skill action.",
        "read_only": True,
        "source": "skill:<id>.<read_action>",
    },
)
WIDGET_KINDS = frozenset(row["kind"] for row in WIDGET_KIND_CATALOG)

DEFAULT_MANIFEST: dict[str, Any] = {
    "version": UI_SCHEMA_VERSION,
    "home": {"widgets": []},
    "pages": [],
}


class WorkspaceUiError(ValueError):
    """Base error for malformed or unsafe UI manifest input."""


class WorkspaceUiConflict(WorkspaceUiError):
    """Raised when a proposal is based on a stale manifest revision/digest."""


@dataclass(frozen=True)
class ValidationResult:
    manifest: dict[str, Any]
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def _clone(value: Any) -> Any:
    return copy.deepcopy(value)


def _as_paths(value: WorkspacePaths | Path | str) -> WorkspacePaths:
    if isinstance(value, WorkspacePaths):
        return value
    return WorkspacePaths(Path(value).expanduser().resolve())


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def _text(value: Any, *, field: str, max_len: int, errors: list[str], required: bool = False) -> str | None:
    if value is None:
        if required:
            errors.append(f"{field} is required")
        return None
    if not isinstance(value, str):
        errors.append(f"{field} must be a string")
        return None
    out = value.strip()
    if required and not out:
        errors.append(f"{field} is required")
    if len(out) > max_len:
        errors.append(f"{field} exceeds {max_len} characters")
        return None
    return out


def _unknown_keys(raw: Mapping[str, Any], allowed: set[str], *, field: str, errors: list[str]) -> None:
    for key in raw:
        key_text = str(key)
        if key_text not in allowed:
            errors.append(f"{field}.{key_text} is not allowed")


def _safe_value(value: Any, *, field: str, errors: list[str], depth: int = 0) -> Any:
    """Copy a JSON-like value while rejecting code-bearing fields/values."""

    if depth > UI_MAX_CONFIG_DEPTH:
        errors.append(f"{field} exceeds nesting depth {UI_MAX_CONFIG_DEPTH}")
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            errors.append(f"{field} must be finite")
            return None
        return value
    if isinstance(value, str):
        if len(value) > 16_384:
            errors.append(f"{field} exceeds 16384 characters")
            return None
        lower = value.lower()
        if "javascript:" in lower or "<script" in lower or "</script" in lower:
            errors.append(f"{field} contains executable markup")
            return None
        return value
    if isinstance(value, Mapping):
        if len(value) > UI_MAX_CONFIG_KEYS:
            errors.append(f"{field} has more than {UI_MAX_CONFIG_KEYS} keys")
            return None
        out: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if not _SAFE_KEY_RE.fullmatch(key):
                errors.append(f"{field}.{key} has an unsafe key")
                continue
            if key.lower() in _DANGEROUS_KEYS:
                errors.append(f"{field}.{key} is not allowed")
                continue
            if key.lower() in {"url", "href", "src", "endpoint", "base_url", "remote_url"}:
                text_value = str(raw_value or "").strip().lower()
                if text_value.startswith(("http://", "https://", "javascript:", "data:")) and not text_value.startswith("/"):
                    errors.append(f"{field}.{key} must be an internal route or identifier")
                    continue
            child = _safe_value(raw_value, field=f"{field}.{key}", errors=errors, depth=depth + 1)
            if child is not None or raw_value is None:
                out[key] = child
        return out
    if isinstance(value, (list, tuple)):
        if len(value) > UI_MAX_CONFIG_LIST:
            errors.append(f"{field} has more than {UI_MAX_CONFIG_LIST} items")
            return None
        return [
            _safe_value(item, field=f"{field}[{index}]", errors=errors, depth=depth + 1)
            for index, item in enumerate(value)
        ]
    errors.append(f"{field} must contain only JSON-like values")
    return None


def _normalize_source(value: Any, *, field: str, errors: list[str]) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        source_id = value.strip()
        if not source_id or not _ACTION_RE.fullmatch(source_id):
            errors.append(f"{field} must be a safe source id")
            return None
        return {"id": source_id, "read_only": True}
    if not isinstance(value, Mapping):
        errors.append(f"{field} must be a mapping or source id")
        return None
    allowed = {"id", "provider", "action", "params", "capability", "read_only"}
    _unknown_keys(value, allowed, field=field, errors=errors)
    out: dict[str, Any] = {}
    for key in ("id", "provider", "action", "capability"):
        if key in value:
            item = _text(value.get(key), field=f"{field}.{key}", max_len=128, errors=errors)
            if item:
                if key in {"id", "action"} and not _ACTION_RE.fullmatch(item):
                    errors.append(f"{field}.{key} is not a safe identifier")
                out[key] = item
    if "params" in value:
        params = _safe_value(value.get("params"), field=f"{field}.params", errors=errors)
        if params is not None and not isinstance(params, dict):
            errors.append(f"{field}.params must be a mapping")
        elif params is not None:
            out["params"] = params
    if value.get("read_only") is False:
        errors.append(f"{field}.read_only must be true for dashboard widgets")
    out["read_only"] = True
    if not any(out.get(key) for key in ("id", "action", "capability")):
        errors.append(f"{field} needs id, action, or capability")
    return out


def _normalize_span(value: Any, *, field: str, errors: list[str]) -> str | int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        errors.append(f"{field} must be full/half/third or an integer 1..12")
        return None
    if isinstance(value, int):
        if not 1 <= value <= 12:
            errors.append(f"{field} must be between 1 and 12")
            return None
        return value
    if isinstance(value, str) and value.strip().lower() in {"full", "wide", "half", "third"}:
        return value.strip().lower()
    errors.append(f"{field} must be full/half/third or an integer 1..12")
    return None


def _normalize_widget(value: Any, *, field: str, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        errors.append(f"{field} must be a mapping")
        return None
    allowed = {"id", "kind", "title", "description", "span", "config", "source", "read_only", "text"}
    _unknown_keys(value, allowed, field=field, errors=errors)
    widget_id = _text(value.get("id"), field=f"{field}.id", max_len=64, errors=errors, required=True)
    if widget_id and not _SLUG_RE.fullmatch(widget_id):
        errors.append(f"{field}.id must match [a-z][a-z0-9_-]*")
    kind = _text(value.get("kind"), field=f"{field}.kind", max_len=64, errors=errors, required=True)
    if kind and kind not in WIDGET_KINDS:
        errors.append(f"{field}.kind {kind!r} is not registered")
    out: dict[str, Any] = {}
    if widget_id:
        out["id"] = widget_id
    if kind:
        out["kind"] = kind
    for key, max_len in (("title", 120), ("description", 500)):
        if key in value:
            item = _text(value.get(key), field=f"{field}.{key}", max_len=max_len, errors=errors)
            if item:
                out[key] = item
    if "text" in value:
        text_value = _text(value.get("text"), field=f"{field}.text", max_len=16_384, errors=errors)
        if text_value:
            out["text"] = text_value
    span = _normalize_span(value.get("span"), field=f"{field}.span", errors=errors)
    if span is not None:
        out["span"] = span
    if "config" in value:
        config = _safe_value(value.get("config"), field=f"{field}.config", errors=errors)
        if config is not None and not isinstance(config, dict):
            errors.append(f"{field}.config must be a mapping")
        elif config is not None:
            out["config"] = config
    elif kind == "markdown":
        out["config"] = {}
    source = _normalize_source(value.get("source"), field=f"{field}.source", errors=errors)
    if source is not None:
        out["source"] = source
    # A false read_only flag is never accepted.  The renderer treats all
    # manifest widgets as read-only, regardless of what an agent submits.
    if value.get("read_only") is False:
        errors.append(f"{field}.read_only must be true")
    out["read_only"] = True
    return out


def _normalize_widgets(value: Any, *, field: str, errors: list[str]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        errors.append(f"{field} must be a list")
        return []
    if len(value) > UI_MAX_WIDGETS_PER_CONTAINER:
        errors.append(f"{field} has more than {UI_MAX_WIDGETS_PER_CONTAINER} widgets")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value[:UI_MAX_WIDGETS_PER_CONTAINER]):
        widget = _normalize_widget(raw, field=f"{field}[{index}]", errors=errors)
        if widget is None:
            continue
        widget_id = str(widget.get("id") or "")
        if widget_id in seen:
            errors.append(f"{field} contains duplicate widget id {widget_id!r}")
            continue
        seen.add(widget_id)
        out.append(widget)
    return out


def _normalize_nav(value: Any, *, field: str, errors: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        errors.append(f"{field} must be a mapping")
        return {}
    allowed = {"label", "order", "section", "hidden"}
    _unknown_keys(value, allowed, field=field, errors=errors)
    out: dict[str, Any] = {}
    if "label" in value:
        label = _text(value.get("label"), field=f"{field}.label", max_len=120, errors=errors)
        if label:
            out["label"] = label
    if "order" in value:
        order = value.get("order")
        if isinstance(order, bool) or not isinstance(order, int) or not -10_000 <= order <= 10_000:
            errors.append(f"{field}.order must be an integer between -10000 and 10000")
        else:
            out["order"] = order
    if "section" in value:
        section = _text(value.get("section"), field=f"{field}.section", max_len=16, errors=errors)
        if section not in {"primary", "advanced"}:
            errors.append(f"{field}.section must be primary or advanced")
        elif section:
            out["section"] = section
    if "hidden" in value:
        hidden = value.get("hidden")
        if not isinstance(hidden, bool):
            errors.append(f"{field}.hidden must be boolean")
        else:
            out["hidden"] = hidden
    return out


def _title_from_id(value: str) -> str:
    return " ".join(part.capitalize() for part in value.replace("_", "-").split("-") if part) or value


def validate_manifest(raw: Any) -> ValidationResult:
    """Validate and normalize an untrusted UI manifest.

    The function never returns caller-owned objects.  Consumers can safely
    mutate the returned manifest while constructing a proposal preview.
    """

    errors: list[str] = []
    warnings: list[str] = []
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        return ValidationResult(_clone(DEFAULT_MANIFEST), ("manifest must be a mapping",), ())
    allowed_top = {"version", "revision", "home", "pages"}
    _unknown_keys(raw, allowed_top, field="manifest", errors=errors)

    version_raw = raw.get("version", UI_SCHEMA_VERSION)
    if isinstance(version_raw, str) and version_raw.strip().isdigit():
        version_raw = int(version_raw.strip())
    if isinstance(version_raw, bool) or not isinstance(version_raw, int) or version_raw != UI_SCHEMA_VERSION:
        errors.append(f"manifest.version must be {UI_SCHEMA_VERSION}")

    revision = raw.get("revision", 0)
    if isinstance(revision, str) and revision.strip().isdigit():
        revision = int(revision.strip())
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        warnings.append("manifest.revision is invalid; treating it as 0")
        revision = 0

    home_raw = raw.get("home")
    if home_raw is None:
        home_raw = {}
    if not isinstance(home_raw, Mapping):
        errors.append("manifest.home must be a mapping")
        home_raw = {}
    _unknown_keys(home_raw, {"widgets", "title", "description"}, field="manifest.home", errors=errors)
    home: dict[str, Any] = {
        "widgets": _normalize_widgets(home_raw.get("widgets"), field="manifest.home.widgets", errors=errors),
    }
    for key, max_len in (("title", 120), ("description", 500)):
        if key in home_raw:
            item = _text(home_raw.get(key), field=f"manifest.home.{key}", max_len=max_len, errors=errors)
            if item:
                home[key] = item

    pages_raw = raw.get("pages", [])
    if not isinstance(pages_raw, (list, tuple)):
        errors.append("manifest.pages must be a list")
        pages_raw = []
    if len(pages_raw) > UI_MAX_PAGES:
        errors.append(f"manifest.pages has more than {UI_MAX_PAGES} pages")
    pages: list[dict[str, Any]] = []
    page_ids: set[str] = set()
    for index, page_raw in enumerate(pages_raw[:UI_MAX_PAGES]):
        field = f"manifest.pages[{index}]"
        if not isinstance(page_raw, Mapping):
            errors.append(f"{field} must be a mapping")
            continue
        allowed_page = {"id", "title", "description", "icon", "widgets", "nav"}
        _unknown_keys(page_raw, allowed_page, field=field, errors=errors)
        page_id = _text(page_raw.get("id"), field=f"{field}.id", max_len=64, errors=errors, required=True)
        if page_id and not _SLUG_RE.fullmatch(page_id):
            errors.append(f"{field}.id must match [a-z][a-z0-9_-]*")
        if page_id and page_id in _RESERVED_PAGE_IDS:
            errors.append(f"{field}.id {page_id!r} is reserved")
        if page_id and page_id in page_ids:
            errors.append(f"manifest.pages contains duplicate id {page_id!r}")
        if page_id:
            page_ids.add(page_id)
        title = _text(page_raw.get("title"), field=f"{field}.title", max_len=120, errors=errors)
        if not title and page_id:
            title = _title_from_id(page_id)
            warnings.append(f"{field}.title missing; derived from id")
        page: dict[str, Any] = {
            "id": page_id or f"page_{index}",
            "title": title or f"Page {index + 1}",
            "widgets": _normalize_widgets(page_raw.get("widgets"), field=f"{field}.widgets", errors=errors),
        }
        for key, max_len in (("description", 500), ("icon", 64)):
            if key in page_raw:
                item = _text(page_raw.get(key), field=f"{field}.{key}", max_len=max_len, errors=errors)
                if item:
                    page[key] = item
        page["nav"] = _normalize_nav(page_raw.get("nav"), field=f"{field}.nav", errors=errors)
        pages.append(page)

    manifest = {"version": UI_SCHEMA_VERSION, "home": home, "pages": pages}
    # Revision is persisted in the YAML document but deliberately omitted from
    # the renderer-facing manifest.  It is returned as a sibling response key.
    if revision:
        manifest["_revision"] = revision
    return ValidationResult(manifest, tuple(errors), tuple(warnings))


def _manifest_without_revision(manifest: Mapping[str, Any]) -> dict[str, Any]:
    out = _clone(dict(manifest))
    out.pop("_revision", None)
    return out


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    canonical = json.dumps(_manifest_without_revision(manifest), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _yaml_document(manifest: Mapping[str, Any], *, revision: int) -> str:
    doc = _manifest_without_revision(manifest)
    doc["revision"] = max(0, int(revision))
    return yaml_io.dumps(doc)


def catalog(*, extensions: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Return the finite widget catalog plus sanitized skill descriptors."""

    out_extensions: list[dict[str, Any]] = []
    for raw in extensions or ():
        if not isinstance(raw, Mapping):
            continue
        # Extension descriptors are metadata only.  Drop any executable or
        # arbitrary-renderer fields before exposing them to the browser.
        row: dict[str, Any] = {}
        for key in ("id", "skill_id", "title", "description", "slot", "kind", "version"):
            value = raw.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text and len(text) <= 256 and "<script" not in text.lower():
                row[key] = text
        if row and (row.get("id") or row.get("skill_id")):
            row["read_only"] = True
            out_extensions.append(row)
    out_extensions.sort(key=lambda row: (row.get("slot", ""), row.get("skill_id", ""), row.get("title", "")))
    return {
        "widget_kinds": [_clone(row) for row in WIDGET_KIND_CATALOG],
        "extensions": out_extensions,
    }


def _candidate_paths(paths: WorkspacePaths) -> list[tuple[str, Path, str]]:
    return [
        (UI_RELATIVE_PATH, paths.root / UI_RELATIVE_PATH, "workspace"),
        (UI_LEGACY_RELATIVE_PATH, paths.root / UI_LEGACY_RELATIVE_PATH, "legacy"),
    ]


def _safe_manifest_file(path: Path) -> tuple[Any, list[str]]:
    errors: list[str] = []
    if path.is_symlink():
        return None, ["manifest path must not be a symlink"]
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, [f"cannot stat manifest: {exc}"]
    if size > UI_MAX_BYTES:
        return None, [f"manifest exceeds {UI_MAX_BYTES} bytes"]
    try:
        text = path.read_text(encoding="utf-8")
        return yaml_io.loads(text), errors
    except Exception as exc:  # yaml parser and filesystem errors
        return None, [f"cannot parse manifest: {type(exc).__name__}: {exc}"]


def read(paths_value: WorkspacePaths | Path | str, *, extensions: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Load, validate, and return the renderer-facing UI envelope."""

    paths = _as_paths(paths_value)
    selected: tuple[str, Path, str] | None = None
    for relative, path, source in _candidate_paths(paths):
        if path.exists() or path.is_symlink():
            selected = (relative, path, source)
            break

    if selected is None:
        manifest = _clone(DEFAULT_MANIFEST)
        return {
            "ok": True,
            "status": "ok",
            "source": "default",
            "path": UI_RELATIVE_PATH,
            "revision": 0,
            "digest": manifest_digest(manifest),
            "manifest": manifest,
            "catalog": catalog(extensions=extensions),
            "errors": [],
            "warnings": [],
        }

    relative, path, source = selected
    raw, load_errors = _safe_manifest_file(path)
    if load_errors:
        return {
            "ok": False,
            "status": "error",
            "source": "invalid",
            "path": relative,
            "revision": 0,
            "digest": manifest_digest(DEFAULT_MANIFEST),
            "manifest": _clone(DEFAULT_MANIFEST),
            "catalog": catalog(extensions=extensions),
            "errors": load_errors,
            "warnings": [],
        }
    result = validate_manifest(raw)
    revision = int(result.manifest.pop("_revision", 0) or 0)
    warnings = list(result.warnings)
    if source == "legacy":
        warnings.append(f"legacy manifest path in use; migrate to {UI_RELATIVE_PATH}")
    status = "ok" if not result.errors and not warnings else "warn" if not result.errors else "error"
    return {
        "ok": not result.errors,
        "status": status,
        "source": source if not result.errors else "invalid",
        "path": relative,
        "revision": revision,
        "digest": manifest_digest(result.manifest),
        "manifest": result.manifest,
        "catalog": catalog(extensions=extensions),
        "errors": list(result.errors),
        "warnings": warnings,
    }


def _current_yaml(paths: WorkspacePaths, current: Mapping[str, Any]) -> str:
    for _, path, _ in _candidate_paths(paths):
        if path.exists() and not path.is_symlink():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                break
    return _yaml_document(current, revision=int(current.get("_revision", 0) or 0))


def _target_container(manifest: dict[str, Any], target: Any) -> list[dict[str, Any]]:
    target_text = str(target or "home").strip().lower()
    if target_text in {"home", "/", "dashboard"}:
        return manifest["home"]["widgets"]
    for page in manifest["pages"]:
        if page.get("id") == target_text:
            return page["widgets"]
    raise WorkspaceUiError(f"unknown dashboard page {target!r}")


def _apply_operations(base: Mapping[str, Any], operations: Any) -> dict[str, Any]:
    if isinstance(operations, Mapping):
        operations = operations.get("operations") or operations.get("changes") or [operations]
    if not isinstance(operations, (list, tuple)):
        raise WorkspaceUiError("patch.operations must be a list")
    candidate = _clone(dict(base))
    candidate.pop("_revision", None)
    for index, operation in enumerate(operations):
        if not isinstance(operation, Mapping):
            raise WorkspaceUiError(f"patch.operations[{index}] must be a mapping")
        op = str(operation.get("op") or operation.get("action") or "").strip().lower()
        if op in {"add_widget", "widget.add"}:
            target = _target_container(candidate, operation.get("page") or operation.get("target"))
            widget = operation.get("widget") or operation.get("value")
            if not isinstance(widget, Mapping):
                raise WorkspaceUiError(f"patch.operations[{index}].widget must be a mapping")
            target.append(_clone(dict(widget)))
        elif op in {"update_widget", "widget.update"}:
            target = _target_container(candidate, operation.get("page") or operation.get("target"))
            widget_id = str(operation.get("id") or operation.get("widget_id") or "")
            changes = operation.get("changes") or operation.get("patch")
            if not widget_id or not isinstance(changes, Mapping):
                raise WorkspaceUiError(f"patch.operations[{index}] needs id and changes")
            for widget in target:
                if widget.get("id") == widget_id:
                    widget.update(_clone(dict(changes)))
                    break
            else:
                raise WorkspaceUiError(f"widget {widget_id!r} not found")
        elif op in {"remove_widget", "widget.remove"}:
            target = _target_container(candidate, operation.get("page") or operation.get("target"))
            widget_id = str(operation.get("id") or operation.get("widget_id") or "")
            before = len(target)
            target[:] = [widget for widget in target if widget.get("id") != widget_id]
            if len(target) == before:
                raise WorkspaceUiError(f"widget {widget_id!r} not found")
        elif op in {"add_page", "page.add"}:
            page = operation.get("page") or operation.get("value")
            if not isinstance(page, Mapping):
                raise WorkspaceUiError(f"patch.operations[{index}].page must be a mapping")
            candidate["pages"].append(_clone(dict(page)))
        elif op in {"update_page", "page.update"}:
            page_id = str(operation.get("id") or operation.get("page_id") or "")
            changes = operation.get("changes") or operation.get("patch")
            if not page_id or not isinstance(changes, Mapping):
                raise WorkspaceUiError(f"patch.operations[{index}] needs page id and changes")
            for page in candidate["pages"]:
                if page.get("id") == page_id:
                    page.update(_clone(dict(changes)))
                    break
            else:
                raise WorkspaceUiError(f"page {page_id!r} not found")
        elif op in {"remove_page", "page.remove"}:
            page_id = str(operation.get("id") or operation.get("page_id") or "")
            before = len(candidate["pages"])
            candidate["pages"] = [page for page in candidate["pages"] if page.get("id") != page_id]
            if len(candidate["pages"]) == before:
                raise WorkspaceUiError(f"page {page_id!r} not found")
        elif op in {"set_nav", "page.nav"}:
            page_id = str(operation.get("id") or operation.get("page_id") or "")
            nav = operation.get("nav") or operation.get("value")
            if not isinstance(nav, Mapping):
                raise WorkspaceUiError(f"patch.operations[{index}].nav must be a mapping")
            for page in candidate["pages"]:
                if page.get("id") == page_id:
                    page["nav"] = _clone(dict(nav))
                    break
            else:
                raise WorkspaceUiError(f"page {page_id!r} not found")
        elif op in {"reorder_widgets", "widgets.reorder"}:
            target = _target_container(candidate, operation.get("page") or operation.get("target"))
            ids = operation.get("ids") or operation.get("order")
            if not isinstance(ids, (list, tuple)):
                raise WorkspaceUiError(f"patch.operations[{index}].ids must be a list")
            by_id = {str(widget.get("id")): widget for widget in target}
            if set(map(str, ids)) != set(by_id):
                raise WorkspaceUiError(f"patch.operations[{index}].ids must contain each widget exactly once")
            target[:] = [by_id[str(widget_id)] for widget_id in ids]
        elif op in {"reorder_pages", "pages.reorder"}:
            ids = operation.get("ids") or operation.get("order")
            if not isinstance(ids, (list, tuple)):
                raise WorkspaceUiError(f"patch.operations[{index}].ids must be a list")
            by_id = {str(page.get("id")): page for page in candidate["pages"]}
            if set(map(str, ids)) != set(by_id):
                raise WorkspaceUiError(f"patch.operations[{index}].ids must contain each page exactly once")
            candidate["pages"] = [by_id[str(page_id)] for page_id in ids]
        else:
            raise WorkspaceUiError(f"patch.operations[{index}] has unsupported op {op!r}")
    return candidate


def _proposal_error(paths: WorkspacePaths, message: str, *, status: int = 400, detail: Any = None) -> dict[str, Any]:
    current = read(paths)
    out = dict(current)
    out.update({"ok": False, "status": "error", "errors": [message]})
    if detail is not None:
        out["detail"] = detail
    out["_status"] = status
    return out


def propose(
    paths_value: WorkspacePaths | Path | str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a full manifest/structured patch and create a review proposal."""

    paths = _as_paths(paths_value)
    body = dict(payload or {})
    current_response = read(paths)
    current = _clone(current_response["manifest"])
    current["_revision"] = int(current_response.get("revision") or 0)
    expected_revision = body.get("base_revision")
    if expected_revision is not None:
        try:
            expected_int = int(expected_revision)
        except (TypeError, ValueError):
            return _proposal_error(paths, "base_revision must be an integer")
        if expected_int != int(current_response.get("revision") or 0):
            return _proposal_error(
                paths,
                "manifest revision is stale",
                status=409,
                detail={"reason": "stale_revision", "expected": expected_int, "current": current_response.get("revision", 0)},
            )
    expected_digest = str(body.get("base_digest") or "").strip()
    if expected_digest and expected_digest != str(current_response.get("digest") or ""):
        return _proposal_error(paths, "manifest digest is stale", status=409, detail={"reason": "stale_digest", "current": current_response.get("digest")})

    if "manifest" in body and body.get("manifest") is not None:
        candidate_raw = body.get("manifest")
    elif "patch" in body or "operations" in body or "changes" in body:
        try:
            candidate_raw = _apply_operations(current, body.get("patch") or {"operations": body.get("operations") or body.get("changes")})
        except WorkspaceUiError as exc:
            return _proposal_error(paths, str(exc))
    else:
        return _proposal_error(paths, "manifest or patch.operations is required")

    validated = validate_manifest(candidate_raw)
    if not validated.ok:
        return _proposal_error(paths, "manifest validation failed", detail={"errors": list(validated.errors), "warnings": list(validated.warnings)})
    candidate = validated.manifest
    candidate_revision = int(current_response.get("revision") or 0) + 1
    before_yaml = _current_yaml(paths, current)
    after_yaml = _yaml_document(candidate, revision=candidate_revision)
    diff_text = "".join(
        difflib.unified_diff(
            before_yaml.splitlines(keepends=True),
            after_yaml.splitlines(keepends=True),
            fromfile=f"a/{UI_RELATIVE_PATH}",
            tofile=f"b/{UI_RELATIVE_PATH}",
        )
    )
    if manifest_digest(current) == manifest_digest(candidate):
        return _proposal_error(paths, "manifest has no changes", status=409)

    from ..evolution.patch_proposal import create_proposal

    summary = str(body.get("summary") or "Customize dashboard workspace UI").strip()[:240]
    rationale = str(body.get("rationale") or "Dashboard UI customization requested by the operator.").strip()
    actor_id = str(body.get("actor_id") or "").strip()[:128]
    metadata = {
        "workspace_ui": True,
        "base_revision": int(current_response.get("revision") or 0),
        "base_digest": str(current_response.get("digest") or ""),
        "manifest_digest": manifest_digest(candidate),
    }
    if actor_id:
        metadata["actor_id"] = actor_id
    try:
        proposal = create_proposal(
            paths,
            kind="core_config_patch",
            summary=summary or "Customize dashboard workspace UI",
            rationale=rationale,
            diff=diff_text,
            test_plan=(
                "# Test plan\n\n"
                "- Load GET /workspace/ui and verify the manifest schema.\n"
                "- Confirm every widget kind is in the read-only catalog.\n"
                "- Open the affected page in the dashboard before enabling any external skill.\n"
            ),
            rollback="Reject the proposal or use the existing evolution rollback route before adding another UI mutation.",
            extra_files={f"after/{UI_RELATIVE_PATH}": after_yaml},
            initial_state="pending_review",
            target=UI_RELATIVE_PATH,
            metadata=metadata,
        )
    except Exception as exc:  # pragma: no cover - filesystem/defensive guard
        return _proposal_error(paths, f"could not create UI proposal: {type(exc).__name__}: {exc}", status=500)

    response = dict(current_response)
    response.update(
        {
            "ok": True,
            "status": "pending_review",
            "source": "proposal",
            "path": UI_RELATIVE_PATH,
            "revision": candidate_revision,
            "manifest": candidate,
            "proposal_id": proposal.id,
            "state": proposal.state,
            "diff": {
                "format": "unified",
                "path": UI_RELATIVE_PATH,
                "text": diff_text,
                "changed": True,
                "before_digest": current_response.get("digest"),
                "after_digest": manifest_digest(candidate),
            },
            "warnings": list(validated.warnings),
            "errors": [],
        }
    )
    return response


def _find_ui_proposal(paths: WorkspacePaths, proposal_id: str):
    from ..evolution.patch_proposal import list_proposals

    for proposal in list_proposals(paths):
        if proposal.id != proposal_id:
            continue
        target = str(proposal.target or "")
        metadata = proposal.metadata or {}
        if proposal.kind == "core_config_patch" and (
            target in {UI_RELATIVE_PATH, UI_LEGACY_RELATIVE_PATH}
            or bool(metadata.get("workspace_ui"))
        ):
            return proposal
        # Keep scanning: unrelated proposals commonly precede the UI proposal
        # in the evolution journal.
    return None


def apply(paths_value: WorkspacePaths | Path | str, proposal_id: Any) -> dict[str, Any]:
    """Apply an approved dashboard proposal through the promotion pipeline."""

    paths = _as_paths(paths_value)
    pid = str(proposal_id or "").strip()
    if not pid or "/" in pid or "\\" in pid:
        return _proposal_error(paths, "proposal_id is invalid")
    proposal = _find_ui_proposal(paths, pid)
    if proposal is None:
        return _proposal_error(paths, "UI proposal not found", status=404)
    if proposal.state != "approved":
        return _proposal_error(paths, f"UI proposal is not approved (state={proposal.state})", status=409, detail={"proposal_id": pid, "state": proposal.state})

    from ..evolution.promotion import apply_proposal

    try:
        result = apply_proposal(paths, pid)
    except Exception as exc:  # pragma: no cover - promotion guard
        return _proposal_error(paths, f"UI proposal apply failed: {type(exc).__name__}: {exc}", status=500)
    if not result.get("ok"):
        response = _proposal_error(paths, str(result.get("reason") or "UI proposal was blocked"), status=409, detail=result)
        response["proposal_id"] = pid
        return response
    current = read(paths)
    current.update({"proposal_id": pid, "applied": True, "state": "applied"})
    return current


def nav_pages(response: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Convert validated page declarations into safe operator-nav entries."""

    pages = response.get("manifest", {}).get("pages", []) if isinstance(response.get("manifest"), Mapping) else []
    if not isinstance(pages, list):
        return [], []
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, page in enumerate(pages):
        if not isinstance(page, Mapping):
            continue
        page_id = str(page.get("id") or "")
        if not _SLUG_RE.fullmatch(page_id) or page_id in _RESERVED_PAGE_IDS:
            warnings.append(f"page {index} has an unsafe navigation id")
            continue
        nav = page.get("nav") if isinstance(page.get("nav"), Mapping) else {}
        if nav.get("hidden") is True:
            continue
        label = str(nav.get("label") or page.get("title") or _title_from_id(page_id)).strip()[:120]
        section = str(nav.get("section") or "advanced").strip().lower()
        if section not in {"primary", "advanced"}:
            section = "advanced"
        order = nav.get("order", index)
        if isinstance(order, bool) or not isinstance(order, int):
            order = index
        entry = {
            # Keep the manifest page id as the nav id so deep-linking,
            # telemetry, and sidebar keys remain stable across reloads.
            "id": page_id,
            "page_id": page_id,
            "label": label,
            "href": f"/workspace/pages/{page_id}",
            "icon": str(page.get("icon") or "layout-dashboard"),
            "tagline": str(page.get("description") or "Operator-customized workspace page")[:240],
            "always_visible": True,
            "workspace_ui": True,
            "order": order,
            "_section": section,
        }
        entries.append(entry)
    entries.sort(key=lambda item: (item.get("order", 0), item.get("label", "")))
    return entries, warnings


__all__ = [
    "DEFAULT_MANIFEST",
    "UI_LEGACY_RELATIVE_PATH",
    "UI_MAX_BYTES",
    "UI_RELATIVE_PATH",
    "WIDGET_KIND_CATALOG",
    "WIDGET_KINDS",
    "ValidationResult",
    "WorkspaceUiConflict",
    "WorkspaceUiError",
    "apply",
    "catalog",
    "manifest_digest",
    "nav_pages",
    "propose",
    "read",
    "validate_manifest",
]
