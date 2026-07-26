"""Versioned planner capability manifests loaded from declarative resources.

Bundled manifests describe capability bundles and fallback behavior only; they
must not ship prompt/event route-match tables. Workspaces can still opt into
explicit operator-owned routing by adding manifests under
``$workspace/route_manifests/<id>.yml`` without forking the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import yaml_io
from ..core.paths import WorkspacePaths


_BUILTIN_MANIFEST_DIR = Path(__file__).with_name("route_manifest_presets")


@dataclass(frozen=True)
class RouteManifest:
    """A named, versioned planner preset.

    ``routes`` is intentionally allowed to be empty. Builtin manifests use
    capabilities as a discoverability surface, not as hidden routing rules.
    """

    id: str
    name: str
    description: str
    version: int
    mode: str
    routes: dict[str, Any]
    fallback: str
    capabilities: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "mode": self.mode,
            "routes": dict(self.routes),
            "fallback": self.fallback,
            "capabilities": list(self.capabilities),
        }


def _manifest_from_path(path: Path, *, manifest_id: str) -> RouteManifest:
    payload = yaml_io.load(path, default={}) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"route manifest {manifest_id!r} must be a mapping")
    return _coerce_manifest_payload(payload, manifest_id=manifest_id)


def _builtin_manifest_paths() -> list[Path]:
    if not _BUILTIN_MANIFEST_DIR.is_dir():
        return []
    return sorted(
        entry
        for entry in _BUILTIN_MANIFEST_DIR.iterdir()
        if entry.is_file() and entry.suffix.lower() in {".yml", ".yaml"}
    )


def _load_builtin_manifest(manifest_id: str) -> RouteManifest:
    for suffix in (".yml", ".yaml"):
        path = _BUILTIN_MANIFEST_DIR / f"{manifest_id}{suffix}"
        if path.is_file():
            return _manifest_from_path(path, manifest_id=manifest_id)
    raise KeyError(f"unknown route manifest id: {manifest_id!r}")


def builtin_manifests() -> list[RouteManifest]:
    """Return all bundled manifests in stable id order."""

    return [
        _manifest_from_path(path, manifest_id=path.stem)
        for path in _builtin_manifest_paths()
    ]


def _external_manifest_dir(paths: WorkspacePaths) -> Path:
    return paths.root / "route_manifests"


def _coerce_manifest_payload(
    payload: dict[str, Any], *, manifest_id: str
) -> RouteManifest:
    routes = payload.get("routes") or {}
    if not isinstance(routes, dict):
        raise ValueError(
            f"route manifest {manifest_id!r} has non-mapping 'routes'"
        )
    fallback = payload.get("fallback") or "generic"
    return RouteManifest(
        id=str(payload.get("id") or manifest_id),
        name=str(payload.get("name") or manifest_id),
        description=str(payload.get("description") or ""),
        version=int(payload.get("version") or 1),
        mode=str(payload.get("mode") or "custom"),
        routes=dict(routes),
        fallback=str(fallback),
        capabilities=list(payload.get("capabilities") or []),
    )


def load_manifest(
    manifest_id: str, paths: WorkspacePaths | None = None
) -> RouteManifest:
    """Load a manifest by id.

    External manifests under ``$workspace/route_manifests`` win over bundled
    resources so operators can override a preset without forking Nerya.
    """

    if paths is not None:
        external = _external_manifest_dir(paths) / f"{manifest_id}.yml"
        if external.is_file():
            return _manifest_from_path(external, manifest_id=manifest_id)
        external_yaml = _external_manifest_dir(paths) / f"{manifest_id}.yaml"
        if external_yaml.is_file():
            return _manifest_from_path(external_yaml, manifest_id=manifest_id)
    return _load_builtin_manifest(manifest_id)


def resolve_routes(
    config: Any,
    paths: WorkspacePaths | None = None,
) -> tuple[dict[str, Any], str, str | None]:
    """Resolve the active route table for a config.

    Returns ``(routes, fallback, manifest_id)``. ``manifest_id`` is the
    selected manifest id when ``agent.planner.manifest`` is configured
    or ``None`` when the workspace is using a freeform route table.
    """

    manifest_id: str | None = None
    if config is not None and hasattr(config, "get"):
        manifest_id = config.get("agent.planner.manifest") or None

    if manifest_id:
        manifest = load_manifest(manifest_id, paths=paths)
        return dict(manifest.routes), manifest.fallback, manifest.id
    return {}, "generic", None


def manifest_summary(
    paths: WorkspacePaths | None = None,
) -> list[dict[str, Any]]:
    """Return a JSON-friendly list describing every available manifest."""

    seen: dict[str, RouteManifest] = {m.id: m for m in builtin_manifests()}
    if paths is not None:
        external = _external_manifest_dir(paths)
        if external.is_dir():
            for entry in sorted(external.iterdir()):
                if entry.suffix.lower() not in {".yml", ".yaml"}:
                    continue
                try:
                    seen[entry.stem] = _manifest_from_path(
                        entry,
                        manifest_id=entry.stem,
                    )
                except ValueError:
                    continue
    return [m.as_dict() for m in seen.values()]
