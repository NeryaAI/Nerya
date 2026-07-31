"""externalised workspace prompt bundles.

The previous design encoded every default workspace prompt
(``agents/system.md``, ``agents/main.agent.md`` and every
``subagents/*.agent.md``) as a Python literal inside
:mod:`nerya.workspace.manager`.  runtime prompt provenance is hard
to do that way: operators editing a prompt cannot diff against the
"factory" version, migrations cannot detect operator edits, and ``nerya
init`` cannot pick a different profile (``trading_paper`` vs
``general_operator`` …) without forking Python source.

This module ships the prompts as actual ``.md`` files inside
``nerya/workspace/_prompt_bundles/<bundle_id>/`` and gives the workspace
manager a small, profile-aware seeder:

- :func:`load_bundle` reads the bundle manifest (``bundle.yml``) plus
  every prompt file.  Returns a :class:`PromptBundle`.
- :func:`seed_bundle` writes the prompts into a fresh workspace and
  records provenance into ``agents/_provenance.yml``.  Re-running
  :func:`seed_bundle` on an existing workspace never overwrites
  operator-edited prompts; the operator's hash diverges from the
  recorded ``installed_sha256`` and the seeder leaves the file alone.
- :func:`detect_drift` reports operator-edited prompts so a future
  ``nerya doctor`` / migration UX can surface them.

The bundle layout is intentionally simple: one ``bundle.yml`` manifest
and one ``.md`` per prompt.  Anyone can add a new bundle by dropping a
sibling directory next to ``default/``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import yaml_io
from ..core.paths import WorkspacePaths
from ..core.time import now_iso

__all__ = [
    "PromptBundle",
    "PromptDriftEntry",
    "BUNDLE_VERSION",
    "DEFAULT_BUNDLE_ID",
    "available_bundles",
    "bundles_root",
    "load_bundle",
    "seed_bundle",
    "detect_drift",
    "load_provenance",
    "provenance_path",
]

#: Layout version of the on-disk provenance ledger.  Bump when changing
#: the schema of ``agents/_provenance.yml``.
BUNDLE_VERSION = 1

#: Default bundle id shipped with the package.
DEFAULT_BUNDLE_ID = "default"


def bundles_root() -> Path:
    """Return the package directory holding all prompt bundles."""

    return Path(__file__).resolve().parent / "_prompt_bundles"


def available_bundles() -> List[str]:
    """List bundle ids shipped with the package."""

    root = bundles_root()
    if not root.is_dir():
        return []
    out: List[str] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        manifest = entry / "bundle.yml"
        if manifest.is_file():
            out.append(entry.name)
    return out


@dataclass
class PromptBundle:
    """A loaded prompt bundle (manifest + file contents)."""

    bundle_id: str
    version: int
    profile: str
    description: str
    source: Path
    #: Mapping of agent slot ("system" | "policies" | "main") to body.
    agents: Dict[str, str] = field(default_factory=dict)
    #: Mapping of subagent id to prompt body.
    subagents: Dict[str, str] = field(default_factory=dict)
    #: Declarative runtime policy per subagent. These values shape budgets,
    #: tool exposure, argument defaults, and tier locks without branching on
    #: role names inside the agent loop.
    subagent_policies: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: Mapping of slot/subagent id to the relative bundle path.  Used
    #: when recording provenance.
    sources: Dict[str, str] = field(default_factory=dict)

    def all_prompts(self) -> Dict[str, str]:
        """Return every prompt keyed by ``"agents/<slot>"`` /
        ``"subagents/<id>"``.  Useful for tests and provenance."""

        out: Dict[str, str] = {}
        for slot, body in self.agents.items():
            out[f"agents/{slot}"] = body
        for sid, body in self.subagents.items():
            out[f"subagents/{sid}"] = body
        return out


def _resolve_bundle_dir(bundle_id: str) -> Path:
    root = bundles_root()
    candidate = root / bundle_id
    if not candidate.is_dir():
        raise FileNotFoundError(
            f"prompt bundle '{bundle_id}' not found under {root}"
        )
    if not (candidate / "bundle.yml").is_file():
        raise FileNotFoundError(
            f"prompt bundle '{bundle_id}' is missing bundle.yml"
        )
    return candidate


def load_bundle(bundle_id: str = DEFAULT_BUNDLE_ID) -> PromptBundle:
    """Load a prompt bundle from the package data directory."""

    root = _resolve_bundle_dir(bundle_id)
    manifest_raw = yaml_io.load(root / "bundle.yml") or {}
    if not isinstance(manifest_raw, dict):
        raise ValueError(
            f"prompt bundle '{bundle_id}' has invalid bundle.yml (must be a mapping)"
        )

    version = int(manifest_raw.get("version", BUNDLE_VERSION))
    profile = str(manifest_raw.get("profile", "trading_paper"))
    description = str(manifest_raw.get("description", "")).strip()

    bundle = PromptBundle(
        bundle_id=str(manifest_raw.get("id", bundle_id)),
        version=version,
        profile=profile,
        description=description,
        source=root,
    )

    agents = manifest_raw.get("agents") or {}
    if not isinstance(agents, dict):
        raise ValueError(
            f"prompt bundle '{bundle_id}': 'agents' must be a mapping"
        )
    for slot, rel in sorted(agents.items()):
        body = _read_relative(root, str(rel))
        bundle.agents[str(slot)] = body
        bundle.sources[f"agents/{slot}"] = str(rel)

    subagents = manifest_raw.get("subagents") or {}
    if not isinstance(subagents, dict):
        raise ValueError(
            f"prompt bundle '{bundle_id}': 'subagents' must be a mapping"
        )
    for sid, rel in sorted(subagents.items()):
        body = _read_relative(root, str(rel))
        bundle.subagents[str(sid)] = body
        bundle.sources[f"subagents/{sid}"] = str(rel)

    policy_config: Dict[str, Any] = manifest_raw
    policy_ref = str(manifest_raw.get("execution_policies") or "").strip()
    if policy_ref:
        policy_text = _read_relative(root, policy_ref)
        try:
            parsed_policy_config = json.loads(policy_text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"prompt bundle '{bundle_id}': invalid execution policy JSON: {exc}"
            ) from exc
        if not isinstance(parsed_policy_config, dict):
            raise ValueError(
                f"prompt bundle '{bundle_id}': execution policies must be a mapping"
            )
        policy_config = parsed_policy_config
        bundle.sources["execution_policies"] = policy_ref

    policy_profiles = policy_config.get("subagent_policy_profiles") or {}
    if not isinstance(policy_profiles, dict):
        raise ValueError(
            f"prompt bundle '{bundle_id}': 'subagent_policy_profiles' must be a mapping"
        )
    subagent_policies = policy_config.get("subagent_policies") or {}
    if not isinstance(subagent_policies, dict):
        raise ValueError(
            f"prompt bundle '{bundle_id}': 'subagent_policies' must be a mapping"
        )
    for sid, raw_policy in sorted(subagent_policies.items()):
        if not isinstance(raw_policy, dict):
            raise ValueError(
                f"prompt bundle '{bundle_id}': policy for {sid!r} must be a mapping"
            )
        resolved: Dict[str, Any] = {}
        extends = raw_policy.get("extends") or []
        if isinstance(extends, str):
            extends = [extends]
        if not isinstance(extends, list):
            raise ValueError(
                f"prompt bundle '{bundle_id}': policy extends for {sid!r} must be a list"
            )
        for profile_name in extends:
            profile = policy_profiles.get(str(profile_name))
            if not isinstance(profile, dict):
                raise ValueError(
                    f"prompt bundle '{bundle_id}': unknown policy profile {profile_name!r}"
                )
            resolved = _deep_merge(resolved, profile)
        resolved = _deep_merge(
            resolved,
            {key: value for key, value in raw_policy.items() if key != "extends"},
        )
        bundle.subagent_policies[str(sid)] = resolved

    return bundle


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge prompt-bundle policy mappings."""

    out: Dict[str, Any] = dict(base)
    for key, value in override.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = _deep_merge(current, value)
        else:
            out[key] = value
    return out


def _read_relative(root: Path, rel: str) -> str:
    rel_path = (root / rel).resolve()
    try:
        rel_path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"prompt bundle '{root.name}' references a path outside the bundle: {rel}"
        ) from exc
    if not rel_path.is_file():
        raise FileNotFoundError(
            f"prompt bundle '{root.name}' is missing referenced file: {rel}"
        )
    return rel_path.read_text(encoding="utf-8")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def provenance_path(paths: WorkspacePaths) -> Path:
    """Return the on-disk path of the prompt-provenance ledger."""

    return paths.agents / "_provenance.yml"


def load_provenance(paths: WorkspacePaths) -> Dict[str, Any]:
    """Load the provenance ledger or return an empty skeleton."""

    p = provenance_path(paths)
    if not p.is_file():
        return {"version": BUNDLE_VERSION, "entries": {}}
    raw = yaml_io.load(p) or {}
    if not isinstance(raw, dict):
        return {"version": BUNDLE_VERSION, "entries": {}}
    if "entries" not in raw or not isinstance(raw.get("entries"), dict):
        raw["entries"] = {}
    raw.setdefault("version", BUNDLE_VERSION)
    return raw


def _save_provenance(paths: WorkspacePaths, ledger: Dict[str, Any]) -> None:
    p = provenance_path(paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    yaml_io.dump(p, ledger)


@dataclass
class PromptDriftEntry:
    """Diff between an installed prompt and the operator-edited version."""

    key: str
    target_path: str
    installed_sha256: str
    on_disk_sha256: Optional[str]
    edited: bool
    missing: bool


def _entry(
    *, bundle: PromptBundle, key: str, target: Path, body: str,
) -> Dict[str, Any]:
    return {
        "bundle_id": bundle.bundle_id,
        "bundle_version": bundle.version,
        "profile": bundle.profile,
        "source_path": bundle.sources.get(key, ""),
        "target_path": str(target),
        "installed_sha256": _sha256(body),
        "installed_at": now_iso(),
    }


def seed_bundle(
    paths: WorkspacePaths,
    bundle: Optional[PromptBundle] = None,
    *,
    bundle_id: str = DEFAULT_BUNDLE_ID,
    overwrite_operator_edits: bool = False,
) -> Dict[str, Any]:
    """Seed the workspace ``agents/`` + ``subagents/`` from a bundle.

    Parameters
    ----------
    paths:
        Resolved workspace paths.
    bundle:
        Optional pre-loaded bundle; ``load_bundle(bundle_id)`` is used
        otherwise.
    bundle_id:
        Bundle id when ``bundle`` is not supplied.
    overwrite_operator_edits:
        If ``False`` (default), prompts whose on-disk hash differs from
        the recorded ``installed_sha256`` are skipped — the operator
        clearly edited them and we do not want ``nerya init`` /
        re-bootstrap to silently revert their changes.  If ``True``,
        the bundle file overwrites the on-disk file unconditionally.
    """

    bundle = bundle or load_bundle(bundle_id)
    paths.agents.mkdir(parents=True, exist_ok=True)
    paths.subagents.mkdir(parents=True, exist_ok=True)

    ledger = load_provenance(paths)
    entries: Dict[str, Any] = ledger.setdefault("entries", {})
    summary: Dict[str, Any] = {
        "bundle_id": bundle.bundle_id,
        "bundle_version": bundle.version,
        "profile": bundle.profile,
        "written": [],
        "skipped_existing": [],
        "skipped_operator_edits": [],
    }

    # ---- agents ---- #
    for slot, body in bundle.agents.items():
        if slot == "main":
            target = paths.agents / "main.agent.md"
        else:
            target = paths.agents / f"{slot}.md"
        key = f"agents/{slot}"
        _seed_one(
            target=target,
            body=body,
            key=key,
            bundle=bundle,
            entries=entries,
            summary=summary,
            overwrite_operator_edits=overwrite_operator_edits,
        )

    # ---- subagents ---- #
    for sid, body in bundle.subagents.items():
        target = paths.subagents / f"{sid}.agent.md"
        key = f"subagents/{sid}"
        _seed_one(
            target=target,
            body=body,
            key=key,
            bundle=bundle,
            entries=entries,
            summary=summary,
            overwrite_operator_edits=overwrite_operator_edits,
        )

    ledger["version"] = BUNDLE_VERSION
    ledger["entries"] = entries
    _save_provenance(paths, ledger)
    summary["provenance_path"] = str(provenance_path(paths))
    return summary


def _seed_one(
    *,
    target: Path,
    body: str,
    key: str,
    bundle: PromptBundle,
    entries: Dict[str, Any],
    summary: Dict[str, Any],
    overwrite_operator_edits: bool,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)

    if not target.exists():
        target.write_text(body, encoding="utf-8")
        entries[key] = _entry(bundle=bundle, key=key, target=target, body=body)
        summary["written"].append(key)
        return

    on_disk = target.read_text(encoding="utf-8")
    on_disk_sha = _sha256(on_disk)
    recorded = entries.get(key) or {}
    recorded_sha = recorded.get("installed_sha256")

    if recorded_sha and on_disk_sha != recorded_sha and not overwrite_operator_edits:
        # Operator edited the file after the previous seed — leave it
        # alone but refresh the recorded "last seen on disk" hash so
        # ``detect_drift`` can still report it.
        recorded["last_observed_sha256"] = on_disk_sha
        recorded["last_observed_at"] = now_iso()
        entries[key] = recorded
        summary["skipped_operator_edits"].append(key)
        return

    if on_disk_sha == _sha256(body):
        entries[key] = _entry(bundle=bundle, key=key, target=target, body=body)
        summary["skipped_existing"].append(key)
        return

    if overwrite_operator_edits:
        target.write_text(body, encoding="utf-8")
        entries[key] = _entry(bundle=bundle, key=key, target=target, body=body)
        summary["written"].append(key)
        return

    # File exists, has not been recorded (legacy workspace), and content
    # differs from the bundle.  Treat as an operator edit and record a
    # provenance entry referencing the bundle so future migrations have
    # a fixed point of reference.
    entries[key] = {
        "bundle_id": bundle.bundle_id,
        "bundle_version": bundle.version,
        "profile": bundle.profile,
        "source_path": bundle.sources.get(key, ""),
        "target_path": str(target),
        "installed_sha256": _sha256(body),
        "installed_at": entries.get(key, {}).get("installed_at", now_iso()),
        "last_observed_sha256": on_disk_sha,
        "last_observed_at": now_iso(),
    }
    summary["skipped_operator_edits"].append(key)


def detect_drift(
    paths: WorkspacePaths,
    bundle: Optional[PromptBundle] = None,
    *,
    bundle_id: str = DEFAULT_BUNDLE_ID,
) -> List[PromptDriftEntry]:
    """Return the list of prompt files whose content drifted from the
    installed bundle.  Missing files are also reported."""

    bundle = bundle or load_bundle(bundle_id)
    ledger = load_provenance(paths)
    entries: Dict[str, Any] = ledger.get("entries", {}) if isinstance(
        ledger, dict
    ) else {}
    out: List[PromptDriftEntry] = []
    for key, expected_body in bundle.all_prompts().items():
        slot_kind, slot_name = key.split("/", 1)
        if slot_kind == "agents":
            target = paths.agents / (
                "main.agent.md" if slot_name == "main" else f"{slot_name}.md"
            )
        else:
            target = paths.subagents / f"{slot_name}.agent.md"
        installed_sha = (
            entries.get(key, {}).get("installed_sha256") or _sha256(expected_body)
        )
        if not target.exists():
            out.append(
                PromptDriftEntry(
                    key=key,
                    target_path=str(target),
                    installed_sha256=installed_sha,
                    on_disk_sha256=None,
                    edited=False,
                    missing=True,
                )
            )
            continue
        on_disk_sha = _sha256(target.read_text(encoding="utf-8"))
        out.append(
            PromptDriftEntry(
                key=key,
                target_path=str(target),
                installed_sha256=installed_sha,
                on_disk_sha256=on_disk_sha,
                edited=on_disk_sha != installed_sha,
                missing=False,
            )
        )
    return out
