"""operator-grade diagnostics surface.

The legacy ``nerya doctor`` printed a flat blob (Python version,
package versions, env vars, workspace root) which is fine for "did
the install work" but is *not* an operator-grade diagnosis. the runtime
``runtime status`` / ``runtime doctor`` cover provider auth states,
gateway platform readiness, dashboard proxy connectivity, DB
schema/version, process registry, plugin/skill availability,
sandbox mode, token/auth mode warnings, profile isolation, and
stale service detection — and each row carries a machine-readable
severity + remediation.

This module ships the Nerya equivalent. Each check is a
``DiagnosticCheck`` registered against the global ``REGISTRY``;
calling :func:`run_diagnostics(client)` walks the registry, runs
every check, and returns a :class:`DiagnosticReport` aggregating
results. The same report backs both ``nerya doctor`` (deep
diagnostic) and ``nerya status`` (concise operator status).

Each :class:`Diagnosis` carries:

* ``id`` — stable identifier (``packages.yaml``, ``provider_auth``,
  ``profile.isolation``, ...).
* ``title`` — short human-readable summary.
* ``severity`` — one of ``ok`` / ``warn`` / ``error``.
* ``detail`` — long-form explanation, may include observed values.
* ``remediation`` — optional suggested fix; printed as the next
  step in ``nerya doctor``.
* ``category`` — bucket for the dashboard (``runtime``,
  ``packages``, ``security``, ``profiles``, ``services``,
  ``capabilities``).
* ``metadata`` — structured payload for the dashboard / capability
  matrix (e.g. installed package versions, env var names).
"""

from __future__ import annotations

import importlib
import os
import platform
import shutil
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

Severity = str  # "ok" / "warn" / "error"


# --------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class Diagnosis:
    """One row in a diagnostic report."""

    id: str
    title: str
    severity: Severity
    detail: str = ""
    remediation: str | None = None
    category: str = "runtime"
    metadata: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "severity": self.severity,
            "detail": self.detail,
            "remediation": self.remediation,
            "category": self.category,
            "metadata": dict(self.metadata),
        }


@dataclass
class DiagnosticReport:
    """Aggregate output of :func:`run_diagnostics`."""

    diagnoses: list[Diagnosis]
    summary: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.summary.get("error", 0) == 0

    @property
    def has_warnings(self) -> bool:
        return self.summary.get("warn", 0) > 0

    def by_category(self) -> dict[str, list[Diagnosis]]:
        out: dict[str, list[Diagnosis]] = {}
        for d in self.diagnoses:
            out.setdefault(d.category, []).append(d)
        return out

    def by_severity(self, severity: Severity) -> list[Diagnosis]:
        return [d for d in self.diagnoses if d.severity == severity]

    def asdict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "diagnoses": [d.asdict() for d in self.diagnoses],
            "metadata": dict(self.metadata),
        }


# --------------------------------------------------------------------- #
# Check registry
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class DiagnosticCheck:
    """One pluggable diagnostic check.

    The handler receives the runtime ``client`` (may be ``None`` if
    the workspace failed to boot) and returns one or more
    :class:`Diagnosis` rows. Returning an empty iterable is fine —
    the runner will still record the check in the metadata.
    """

    id: str
    handler: Callable[[Any], Iterable[Diagnosis]]
    title: str = ""
    category: str = "runtime"
    requires_client: bool = False
    tags: tuple[str, ...] = ()


class DiagnosticRegistry:
    """Thread-safe registry of :class:`DiagnosticCheck` rows."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: dict[str, DiagnosticCheck] = {}

    def add(self, check: DiagnosticCheck, *, override: bool = False) -> DiagnosticCheck:
        with self._lock:
            if not override and check.id in self._by_id:
                raise ValueError(
                    f"diagnostic {check.id!r} already registered"
                    " (pass override=True to replace)"
                )
            self._by_id[check.id] = check
            return check

    def remove(self, check_id: str) -> bool:
        with self._lock:
            return self._by_id.pop(check_id, None) is not None

    def get(self, check_id: str) -> DiagnosticCheck | None:
        with self._lock:
            return self._by_id.get(check_id)

    def ids(self) -> list[str]:
        with self._lock:
            return sorted(self._by_id)

    def checks(self) -> list[DiagnosticCheck]:
        with self._lock:
            return [self._by_id[i] for i in sorted(self._by_id)]


REGISTRY = DiagnosticRegistry()


def register_check(check: DiagnosticCheck) -> DiagnosticCheck:
    """Convenience wrapper for module-level registration."""
    return REGISTRY.add(check)


# --------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------- #


def run_diagnostics(
    client: Any | None,
    *,
    only: Iterable[str] | None = None,
    skip: Iterable[str] | None = None,
    registry: DiagnosticRegistry | None = None,
) -> DiagnosticReport:
    """Walk the registry and aggregate a :class:`DiagnosticReport`."""

    reg = registry or REGISTRY
    only_set = set(only) if only else None
    skip_set = set(skip) if skip else set()

    diagnoses: list[Diagnosis] = []
    summary = {"ok": 0, "warn": 0, "error": 0}
    ran: list[str] = []
    for check in reg.checks():
        if only_set is not None and check.id not in only_set:
            continue
        if check.id in skip_set:
            continue
        if check.requires_client and client is None:
            diagnoses.append(Diagnosis(
                id=check.id,
                title=check.title or check.id,
                severity="warn",
                detail="check skipped — no client available",
                remediation="run 'nerya init' to initialise the workspace",
                category=check.category,
            ))
            summary["warn"] += 1
            ran.append(check.id)
            continue
        try:
            results = list(check.handler(client))
        except Exception as exc:  # pragma: no cover - defensive
            results = [Diagnosis(
                id=check.id,
                title=check.title or check.id,
                severity="error",
                detail=f"check raised {type(exc).__name__}: {exc}",
                remediation="open an issue or rerun with --skip "
                            f"{check.id}",
                category=check.category,
            )]
        for d in results:
            diagnoses.append(d)
            summary[d.severity] = summary.get(d.severity, 0) + 1
        ran.append(check.id)

    return DiagnosticReport(
        diagnoses=diagnoses,
        summary=summary,
        metadata={
            "checks_ran": ran,
            "client_available": client is not None,
        },
    )


# --------------------------------------------------------------------- #
# Default checks
# --------------------------------------------------------------------- #


_REQUIRED_PACKAGES = (
    "yaml", "pydantic", "cryptography", "httpx",
    "jsonschema", "msgpack",
)
_OPTIONAL_PACKAGES = ("eth_account", "nacl", "websockets")


def _check_runtime_basics(_client: Any) -> Iterable[Diagnosis]:
    """Python version + platform + executable presence."""

    py = sys.version.split()[0]
    yield Diagnosis(
        id="runtime.python",
        title="Python runtime",
        severity="ok",
        detail=f"python {py} on {platform.platform()}",
        category="runtime",
        metadata={"python": py, "platform": platform.platform(),
                  "executable": sys.executable},
    )

    expected = ("uv", "git", "node", "npm", "pnpm")
    found: dict[str, str | None] = {}
    missing: list[str] = []
    for name in expected:
        path = shutil.which(name)
        found[name] = path
        if path is None:
            missing.append(name)
    if missing:
        yield Diagnosis(
            id="runtime.binaries",
            title="External binaries",
            severity="warn",
            detail=f"missing binaries: {', '.join(missing)}",
            remediation=("install the missing tools — uv/git are required, "
                         "node/npm/pnpm are needed for the dashboard"),
            category="runtime",
            metadata={"found": {k: bool(v) for k, v in found.items()}},
        )
    else:
        yield Diagnosis(
            id="runtime.binaries",
            title="External binaries",
            severity="ok",
            detail="all expected binaries found",
            category="runtime",
            metadata={"found": {k: bool(v) for k, v in found.items()}},
        )


_PACKAGE_DIST_NAMES: dict[str, str] = {
    "yaml": "PyYAML",
    "nacl": "PyNaCl",
}


def _resolve_version(import_name: str) -> str:
    """Best-effort version resolution that avoids deprecated
    ``__version__`` attribute access (jsonschema, etc.)."""

    from importlib import metadata as md

    dist_name = _PACKAGE_DIST_NAMES.get(import_name, import_name)
    try:
        return md.version(dist_name)
    except md.PackageNotFoundError:
        try:
            return md.version(import_name)
        except md.PackageNotFoundError:
            return "ok"


def _check_packages(_client: Any) -> Iterable[Diagnosis]:
    """Required + optional Python package presence + version."""

    versions: dict[str, str] = {}
    missing_required: list[str] = []
    for name in _REQUIRED_PACKAGES:
        try:
            importlib.import_module(name)
            versions[name] = _resolve_version(name)
        except Exception as exc:
            versions[name] = f"missing ({type(exc).__name__})"
            missing_required.append(name)
    optional_versions: dict[str, str] = {}
    optional_missing: list[str] = []
    for name in _OPTIONAL_PACKAGES:
        try:
            importlib.import_module(name)
            optional_versions[name] = _resolve_version(name)
        except Exception:
            optional_versions[name] = "missing"
            optional_missing.append(name)

    if missing_required:
        yield Diagnosis(
            id="packages.required",
            title="Required Python packages",
            severity="error",
            detail=f"missing required packages: {', '.join(missing_required)}",
            remediation="run `pip install -e .` (or the equivalent uv command)",
            category="packages",
            metadata={"versions": versions, "missing": missing_required},
        )
    else:
        yield Diagnosis(
            id="packages.required",
            title="Required Python packages",
            severity="ok",
            detail=f"all {len(_REQUIRED_PACKAGES)} required packages installed",
            category="packages",
            metadata={"versions": versions},
        )

    if optional_missing:
        yield Diagnosis(
            id="packages.optional",
            title="Optional Python packages",
            severity="warn",
            detail=f"optional packages missing: {', '.join(optional_missing)}",
            remediation=("install eth_account/nacl for on-chain wallets, "
                         "websockets for streaming gateways"),
            category="packages",
            metadata={"versions": optional_versions,
                      "missing": optional_missing},
        )
    else:
        yield Diagnosis(
            id="packages.optional",
            title="Optional Python packages",
            severity="ok",
            detail="all optional packages installed",
            category="packages",
            metadata={"versions": optional_versions},
        )


_TRACKED_ENV = (
    "NERYA_WORKSPACE", "NERYA_HOME", "NERYA_DEV_MODE",
    "NERYA_PORT", "NERYA_AUTH_MODE", "NERYA_API_TOKEN",
    "NERYA_DASHBOARD_URL", "NERYA_ALLOW_MOCK_DATA",
    "NERYA_LOCK_SIGNING_KEY",
)
_SECRET_ENV = ("NERYA_API_TOKEN", "NERYA_LOCK_SIGNING_KEY")


def _check_env_vars(_client: Any) -> Iterable[Diagnosis]:
    """Tracked NERYA_* env vars + redaction for secrets."""

    snapshot: dict[str, str] = {}
    for name in _TRACKED_ENV:
        val = os.environ.get(name)
        if val is None:
            snapshot[name] = ""
        elif name in _SECRET_ENV:
            snapshot[name] = f"<set:{len(val)}b>"
        else:
            snapshot[name] = val
    yield Diagnosis(
        id="env.tracked",
        title="Tracked environment variables",
        severity="ok",
        detail=(f"observed {sum(1 for v in snapshot.values() if v)} "
                f"of {len(_TRACKED_ENV)} tracked variables"),
        category="runtime",
        metadata={"snapshot": snapshot},
    )


def _check_workspace(client: Any) -> Iterable[Diagnosis]:
    """Workspace boot + path presence."""

    paths = client.config.paths
    root_exists = paths.root.exists()
    config_exists = paths.config.exists()
    if not root_exists:
        yield Diagnosis(
            id="workspace.root",
            title="Workspace root",
            severity="error",
            detail=f"workspace root missing: {paths.root}",
            remediation="run 'nerya init' to bootstrap the workspace",
            category="profiles",
            metadata={"root": str(paths.root)},
        )
        return
    severity: Severity = "ok" if config_exists else "warn"
    detail = (f"root: {paths.root}; config: {paths.config} "
              f"({'present' if config_exists else 'missing'})")
    yield Diagnosis(
        id="workspace.root",
        title="Workspace root",
        severity=severity,
        detail=detail,
        category="profiles",
        metadata={
            "root": str(paths.root),
            "config": str(paths.config),
            "config_exists": config_exists,
            "dev_mode": bool(client.config.get("runtime.dev_mode")),
        },
    )


def _check_profile_isolation(client: Any) -> Iterable[Diagnosis]:
    """profile isolation under ``$NERYA_HOME``."""

    try:
        from ..core.paths import _resolve_home, list_profiles
    except Exception:
        return
    home = _resolve_home()
    profiles = list_profiles(home)
    active_root = client.config.paths.root
    in_profile = active_root.is_relative_to(home) if profiles else False
    if not profiles:
        yield Diagnosis(
            id="profile.isolation",
            title="Profile isolation",
            severity="warn",
            detail=(f"no profiles in {home}; running with the default "
                    "workspace only"),
            remediation="run 'nerya profile init --name <name>' to create "
                        "an isolated profile",
            category="profiles",
            metadata={"home": str(home), "profiles": profiles},
        )
        return
    yield Diagnosis(
        id="profile.isolation",
        title="Profile isolation",
        severity="ok" if in_profile else "warn",
        detail=(f"home: {home}; {len(profiles)} profile(s); "
                f"active root inside home: {in_profile}"),
        remediation=None if in_profile else (
            "consider running with --profile to isolate state under "
            f"{home}"
        ),
        category="profiles",
        metadata={"home": str(home), "profiles": profiles,
                  "active_root": str(active_root),
                  "active_in_home": in_profile},
    )


def _check_db_schema(client: Any) -> Iterable[Diagnosis]:
    """DB schema version + ledger consistency."""

    try:
        import sqlite3

        from ..db import migrations as db_mig
    except Exception:
        return
    paths = client.config.paths
    db_path = getattr(paths, "db", None)
    if db_path is None or not db_path.exists():
        yield Diagnosis(
            id="db.schema_version",
            title="DB schema version",
            severity="ok",
            detail="no SQLite DB yet — schema will initialise on first use",
            category="capabilities",
        )
        return
    try:
        latest = max(m.version for m in db_mig.MIGRATIONS) if db_mig.MIGRATIONS else 0
        with sqlite3.connect(str(db_path)) as con:
            current = db_mig.current_version(con)
    except Exception as exc:
        yield Diagnosis(
            id="db.schema_version",
            title="DB schema version",
            severity="warn",
            detail=f"could not read schema ledger: {type(exc).__name__}: {exc}",
            remediation="run 'nerya db migrate' to apply pending migrations",
            category="capabilities",
        )
        return
    if current < latest:
        yield Diagnosis(
            id="db.schema_version",
            title="DB schema version",
            severity="warn",
            detail=f"applied={current} latest={latest}; "
                   f"{latest - current} migration(s) pending",
            remediation="run 'nerya db migrate'",
            category="capabilities",
            metadata={"applied": current, "latest": latest},
        )
    else:
        yield Diagnosis(
            id="db.schema_version",
            title="DB schema version",
            severity="ok",
            detail=f"schema at version {current}",
            category="capabilities",
            metadata={"applied": current, "latest": latest},
        )


def _check_skills_lock(client: Any) -> Iterable[Diagnosis]:
    """skills lockfile + signature freshness."""

    try:
        from ..skills import lockfile as lock_mod
    except Exception:
        return
    paths = client.config.paths
    lock_path = getattr(paths, "skills_lock", None)
    if lock_path is None or not lock_path.exists():
        yield Diagnosis(
            id="skills.lock",
            title="Skills lockfile",
            severity="warn",
            detail="no skills.lock.yml — supply chain trust is not enforced",
            remediation="run 'nerya skill lock refresh'",
            category="security",
        )
        return
    try:
        entries = lock_mod.load_lock(paths)
        report = lock_mod.verify_lock(paths)
        if report.ok:
            yield Diagnosis(
                id="skills.lock",
                title="Skills lockfile",
                severity="ok",
                detail=f"{len(entries)} skill(s) locked, all hashes match",
                category="security",
                metadata={"entries": len(entries)},
            )
        else:
            problems: list[str] = []
            if report.missing:
                problems.append(f"{len(report.missing)} missing")
            if report.untracked:
                problems.append(f"{len(report.untracked)} untracked")
            if report.mismatches:
                problems.append(f"{len(report.mismatches)} mismatched")
            yield Diagnosis(
                id="skills.lock",
                title="Skills lockfile",
                severity="warn",
                detail=f"lock drift: {', '.join(problems)}",
                remediation="run 'nerya skill lock refresh' after reviewing "
                            "the diff",
                category="security",
                metadata={
                    "entries": len(entries),
                    "missing": list(report.missing),
                    "untracked": list(report.untracked),
                    "mismatches": list(report.mismatches),
                },
            )
    except Exception as exc:
        yield Diagnosis(
            id="skills.lock",
            title="Skills lockfile",
            severity="warn",
            detail=f"could not verify lock: {type(exc).__name__}: {exc}",
            category="security",
        )


def _check_provider_auth(client: Any) -> Iterable[Diagnosis]:
    """provider auth records (OpenAI, Anthropic, ...)."""

    try:
        from ..security.provider_auth import ProviderAuthStore
    except Exception:
        return
    paths = client.config.paths
    store_path = getattr(paths, "provider_auth", None)
    if store_path is None or not store_path.exists():
        yield Diagnosis(
            id="provider_auth.records",
            title="Provider auth",
            severity="warn",
            detail="no provider auth records — LLM/MCP calls may fail",
            remediation="run 'nerya security provider-auth register' or set "
                        "OPENAI_API_KEY/ANTHROPIC_API_KEY",
            category="security",
        )
        return
    try:
        store = ProviderAuthStore.open(store_path)
        records = store.list()
    except Exception as exc:
        yield Diagnosis(
            id="provider_auth.records",
            title="Provider auth",
            severity="warn",
            detail=f"could not load store: {type(exc).__name__}: {exc}",
            category="security",
        )
        return
    if not records:
        yield Diagnosis(
            id="provider_auth.records",
            title="Provider auth",
            severity="warn",
            detail="store is empty — no providers configured",
            remediation="run 'nerya security provider-auth register'",
            category="security",
        )
        return
    expired = [r for r in records if r.is_expired()]
    if expired:
        names = ", ".join(f"{r.provider}:{r.actor}" for r in expired[:3])
        yield Diagnosis(
            id="provider_auth.records",
            title="Provider auth",
            severity="warn",
            detail=f"{len(expired)} record(s) expired: {names}",
            remediation="run 'nerya security provider-auth refresh "
                        "--provider <id>'",
            category="security",
            metadata={"records": len(records), "expired": len(expired)},
        )
    else:
        yield Diagnosis(
            id="provider_auth.records",
            title="Provider auth",
            severity="ok",
            detail=f"{len(records)} provider record(s), none expired",
            category="security",
            metadata={"records": len(records)},
        )


def _check_auth_mode(client: Any) -> Iterable[Diagnosis]:
    """API auth mode warnings."""

    try:
        mode = str(client.config.get("api.auth.mode") or "local").lower()
    except Exception:
        mode = "local"
    if mode == "off":
        yield Diagnosis(
            id="api.auth_mode",
            title="API auth mode",
            severity="warn",
            detail="auth mode is 'off' — API is unauthenticated",
            remediation="set api.auth.mode to 'local' or 'token' for "
                        "anything beyond local dev",
            category="security",
            metadata={"mode": mode},
        )
    elif mode == "local":
        yield Diagnosis(
            id="api.auth_mode",
            title="API auth mode",
            severity="ok",
            detail="auth mode 'local' — only loopback hosts trusted",
            category="security",
            metadata={"mode": mode},
        )
    elif mode == "token":
        token = os.environ.get("NERYA_API_TOKEN") or ""
        if not token:
            yield Diagnosis(
                id="api.auth_mode",
                title="API auth mode",
                severity="error",
                detail="auth mode 'token' but NERYA_API_TOKEN is unset",
                remediation="export NERYA_API_TOKEN=<token> before serving",
                category="security",
                metadata={"mode": mode},
            )
        else:
            yield Diagnosis(
                id="api.auth_mode",
                title="API auth mode",
                severity="ok",
                detail="auth mode 'token' configured",
                category="security",
                metadata={"mode": mode, "token_len": len(token)},
            )
    else:
        yield Diagnosis(
            id="api.auth_mode",
            title="API auth mode",
            severity="warn",
            detail=f"unknown auth mode: {mode!r}",
            remediation="set api.auth.mode to 'local', 'token', or 'off'",
            category="security",
            metadata={"mode": mode},
        )


def _check_token_scopes(client: Any) -> Iterable[Diagnosis]:
    """token grant audit.

    The route authorization matrix only helps if tokens are issued with
    least-privilege scopes. This check looks at every configured token
    and flags ones that:

    * still hold the wildcard ``api:all`` while ``auth.mode == 'token'``
      (operators usually want narrower scopes for service tokens), or
    * declare a scope name that is not in the canonical catalog (likely
      a typo that would silently never be granted).

    Local-mode workspaces are always given a pass; loopback callers
    keep the wildcard by design.
    """

    try:
        from ..api import route_scopes as rs
    except Exception:
        return
    try:
        mode = str(client.config.get("runtime.auth.mode") or "local").lower()
    except Exception:
        mode = "local"
    try:
        configured = client.config.get("runtime.auth.tokens") or []
    except Exception:
        configured = []
    if not isinstance(configured, list) or not configured:
        # No tokens configured — nothing to audit. Local mode covers
        # the operator on loopback.
        return

    wildcards: list[str] = []
    unknown_scopes: dict[str, list[str]] = {}
    rows: list[dict[str, Any]] = []
    for entry in configured:
        if isinstance(entry, dict):
            actor = str(entry.get("actor") or "token:user")
            raw = entry.get("scope")
        elif isinstance(entry, str):
            actor = "token:user"
            raw = None
        else:
            continue
        scopes = rs.parse_scopes(raw)
        if not scopes:
            scopes = frozenset({rs.WILDCARD_SCOPE})
        rows.append({
            "actor": actor,
            "scopes": sorted(scopes),
            "wildcard": rs.WILDCARD_SCOPE in scopes,
        })
        if rs.WILDCARD_SCOPE in scopes and mode == "token":
            wildcards.append(actor)
        unknown = scopes - rs.ALL_SCOPES
        if unknown:
            unknown_scopes[actor] = sorted(unknown)

    if unknown_scopes:
        details = ", ".join(
            f"{actor}={scopes}" for actor, scopes in unknown_scopes.items()
        )
        yield Diagnosis(
            id="api.token_scopes",
            title="Token scope grants",
            severity="warn",
            detail=f"unknown scope(s): {details}",
            remediation="check the canonical scope catalog (read:runtime, "
                        "write:chat, write:tools, write:secrets, trade:paper, "
                        "trade:live, gateway:webhook, gateway:send, admin:ops, "
                        "api:all) and fix typos in runtime.auth.tokens",
            category="security",
            metadata={"tokens": rows, "unknown": unknown_scopes},
        )
        return

    if wildcards:
        yield Diagnosis(
            id="api.token_scopes",
            title="Token scope grants",
            severity="warn",
            detail=(f"{len(wildcards)} token(s) hold wildcard 'api:all' in "
                    f"token mode: {', '.join(wildcards)}"),
            remediation="narrow each token's scope to the routes it needs "
                        "(see /runtime/capability_matrix.route_scopes)",
            category="security",
            metadata={"tokens": rows, "wildcards": wildcards},
        )
        return

    yield Diagnosis(
        id="api.token_scopes",
        title="Token scope grants",
        severity="ok",
        detail=f"{len(rows)} token(s) all hold narrow scopes",
        category="security",
        metadata={"tokens": rows},
    )


def _check_skills_availability(client: Any) -> Iterable[Diagnosis]:
    """action availability probes."""

    try:
        from ..skills.availability import build_availability_table
    except Exception:
        return
    try:
        table = build_availability_table(
            client.config, client.skills.registry,
        )
    except Exception as exc:
        yield Diagnosis(
            id="skills.availability",
            title="Skill availability",
            severity="warn",
            detail=f"could not probe availability: {type(exc).__name__}: {exc}",
            category="capabilities",
        )
        return
    total = sum(len(actions) for actions in table.values())
    available = 0
    unavailable_examples: list[str] = []
    for skill_id, actions in table.items():
        for action_name, verdict in actions.items():
            if verdict.available:
                available += 1
            elif len(unavailable_examples) < 5:
                reason = verdict.reason or "unavailable"
                unavailable_examples.append(
                    f"{skill_id}.{action_name} ({reason})"
                )
    missing = total - available
    if missing == 0:
        yield Diagnosis(
            id="skills.availability",
            title="Skill availability",
            severity="ok",
            detail=f"all {total} actions available",
            category="capabilities",
            metadata={"available": available, "total": total},
        )
    else:
        yield Diagnosis(
            id="skills.availability",
            title="Skill availability",
            severity="warn",
            detail=(f"{missing}/{total} actions unavailable "
                    f"(e.g. {', '.join(unavailable_examples)})"),
            remediation=("set the missing env vars / secrets — see "
                         "/runtime/capability_matrix for details"),
            category="capabilities",
            metadata={"available": available, "total": total,
                      "missing": missing,
                      "examples": unavailable_examples},
        )


def _check_model_registry(client: Any) -> Iterable[Diagnosis]:
    """LLM model metadata coverage."""

    try:
        from ..llm import model_registry as mr
    except Exception:
        return
    try:
        configured_tiers = client.config.get("llm.tiers") or {}
    except Exception:
        return
    if not configured_tiers:
        yield Diagnosis(
            id="model_registry.coverage",
            title="Model registry coverage",
            severity="warn",
            detail="no LLM tiers configured",
            remediation="set llm.tiers in nerya.yml",
            category="capabilities",
        )
        return
    unknown: list[str] = []
    known: list[str] = []
    for tier, cfg in configured_tiers.items():
        if not isinstance(cfg, dict):
            continue
        model_id = cfg.get("model") or cfg.get("name")
        provider = cfg.get("provider") or ""
        if not model_id:
            continue
        try:
            meta = mr.lookup(str(provider), str(model_id))
        except Exception:
            unknown.append(f"{tier}={provider}/{model_id}")
            continue
        if meta is None or getattr(meta, "source", "unknown") == "unknown":
            unknown.append(f"{tier}={provider}/{model_id}")
        else:
            known.append(f"{tier}={provider}/{model_id}")
    if unknown:
        yield Diagnosis(
            id="model_registry.coverage",
            title="Model registry coverage",
            severity="warn",
            detail=f"{len(unknown)} configured model(s) lack metadata: "
                   + ", ".join(unknown),
            remediation="refresh the models.dev cache or add a builtin "
                        "snapshot for these models",
            category="capabilities",
            metadata={"known": known, "unknown": unknown},
        )
    else:
        yield Diagnosis(
            id="model_registry.coverage",
            title="Model registry coverage",
            severity="ok",
            detail=f"{len(known)} configured tier(s) all have metadata",
            category="capabilities",
            metadata={"known": known},
        )


def _check_service_status(_client: Any) -> Iterable[Diagnosis]:
    """stale service detection."""

    try:
        from ..install import service as svc
    except Exception:
        return
    try:
        status = svc.status()
    except Exception:
        return
    installed = bool(status.get("installed"))
    running = bool(status.get("running"))
    if installed and not running:
        yield Diagnosis(
            id="service.status",
            title="Background service",
            severity="warn",
            detail="service is installed but not running",
            remediation="run 'nerya service status' for details or "
                        "restart via your platform's service manager",
            category="services",
            metadata=status,
        )
    elif installed and running:
        yield Diagnosis(
            id="service.status",
            title="Background service",
            severity="ok",
            detail="service installed and running",
            category="services",
            metadata=status,
        )
    else:
        yield Diagnosis(
            id="service.status",
            title="Background service",
            severity="ok",
            detail="service not installed (foreground-only mode)",
            category="services",
            metadata=status,
        )


def _check_operator_preset(client: Any) -> Iterable[Diagnosis]:
    """active operator preset advisory."""

    try:
        preset_id = str(client.config.get("agent.operator.preset") or "")
        live = bool(client.config.get("runtime.live_trading_enabled"))
    except Exception:
        return
    if not preset_id:
        yield Diagnosis(
            id="agent.preset",
            title="Operator preset",
            severity="warn",
            detail="no operator preset selected",
            remediation="set agent.operator.preset to 'read_only', "
                        "'dev', 'deploy', or 'live_trading'",
            category="capabilities",
        )
        return
    if preset_id == "live_trading" and not live:
        yield Diagnosis(
            id="agent.preset",
            title="Operator preset",
            severity="warn",
            detail="preset is 'live_trading' but runtime.live_trading_enabled "
                   "is False — trading actions will still be gated",
            remediation="set runtime.live_trading_enabled: true to allow "
                        "trading, or switch to 'deploy' if undesired",
            category="capabilities",
            metadata={"preset": preset_id, "live_trading_enabled": live},
        )
    else:
        yield Diagnosis(
            id="agent.preset",
            title="Operator preset",
            severity="ok",
            detail=f"preset '{preset_id}' active "
                   f"(live trading {'enabled' if live else 'disabled'})",
            category="capabilities",
            metadata={"preset": preset_id, "live_trading_enabled": live},
        )


# --------------------------------------------------------------------- #
# Default-check registration
# --------------------------------------------------------------------- #


def register_default_checks(registry: DiagnosticRegistry | None = None) -> None:
    """Populate ``registry`` (or the global ``REGISTRY``) with the
    canonical Nerya diagnostic checks. Called once at module import.
    """

    reg = registry or REGISTRY
    builtin = [
        DiagnosticCheck(
            id="runtime.python",
            handler=_check_runtime_basics,
            title="Runtime basics",
            category="runtime",
        ),
        DiagnosticCheck(
            id="packages",
            handler=_check_packages,
            title="Python packages",
            category="packages",
        ),
        DiagnosticCheck(
            id="env.tracked",
            handler=_check_env_vars,
            title="Environment variables",
            category="runtime",
        ),
        DiagnosticCheck(
            id="workspace.root",
            handler=_check_workspace,
            title="Workspace",
            category="profiles",
            requires_client=True,
        ),
        DiagnosticCheck(
            id="profile.isolation",
            handler=_check_profile_isolation,
            title="Profile isolation",
            category="profiles",
            requires_client=True,
        ),
        DiagnosticCheck(
            id="api.auth_mode",
            handler=_check_auth_mode,
            title="API auth mode",
            category="security",
            requires_client=True,
        ),
        DiagnosticCheck(
            id="api.token_scopes",
            handler=_check_token_scopes,
            title="Token scope grants",
            category="security",
            requires_client=True,
        ),
        DiagnosticCheck(
            id="db.schema_version",
            handler=_check_db_schema,
            title="DB schema version",
            category="capabilities",
            requires_client=True,
        ),
        DiagnosticCheck(
            id="skills.lock",
            handler=_check_skills_lock,
            title="Skills lockfile",
            category="security",
            requires_client=True,
        ),
        DiagnosticCheck(
            id="provider_auth.records",
            handler=_check_provider_auth,
            title="Provider auth",
            category="security",
            requires_client=True,
        ),
        DiagnosticCheck(
            id="skills.availability",
            handler=_check_skills_availability,
            title="Skill availability",
            category="capabilities",
            requires_client=True,
        ),
        DiagnosticCheck(
            id="model_registry.coverage",
            handler=_check_model_registry,
            title="Model registry coverage",
            category="capabilities",
            requires_client=True,
        ),
        DiagnosticCheck(
            id="agent.preset",
            handler=_check_operator_preset,
            title="Operator preset",
            category="capabilities",
            requires_client=True,
        ),
        DiagnosticCheck(
            id="service.status",
            handler=_check_service_status,
            title="Background service",
            category="services",
        ),
    ]
    for check in builtin:
        reg.add(check, override=True)


register_default_checks()


# --------------------------------------------------------------------- #
# Convenience view: status (concise) vs doctor (verbose)
# --------------------------------------------------------------------- #


def render_status(report: DiagnosticReport) -> str:
    """Concise one-line-per-check render for ``nerya status``."""

    lines = []
    counts = report.summary
    header = (f"status: {'OK' if report.ok else 'PROBLEMS'} "
              f"({counts.get('error', 0)} error, "
              f"{counts.get('warn', 0)} warn, "
              f"{counts.get('ok', 0)} ok)")
    lines.append(header)
    for d in report.diagnoses:
        if d.severity == "ok":
            continue
        prefix = "!" if d.severity == "error" else "?"
        lines.append(f"  {prefix} {d.id}: {d.detail}")
    return "\n".join(lines)


def render_doctor(report: DiagnosticReport) -> str:
    """Verbose multi-line render for ``nerya doctor``."""

    lines = []
    counts = report.summary
    lines.append(f"=== nerya doctor — {'OK' if report.ok else 'PROBLEMS'} ===")
    lines.append(f"summary: {counts.get('error', 0)} error, "
                 f"{counts.get('warn', 0)} warn, "
                 f"{counts.get('ok', 0)} ok")
    for category, items in report.by_category().items():
        lines.append(f"\n[{category}]")
        for d in items:
            badge = {"ok": "OK ", "warn": "WARN", "error": "FAIL"}[d.severity]
            lines.append(f"  {badge}  {d.id}: {d.title}")
            if d.detail:
                lines.append(f"        {d.detail}")
            if d.remediation:
                lines.append(f"        -> {d.remediation}")
    return "\n".join(lines)
