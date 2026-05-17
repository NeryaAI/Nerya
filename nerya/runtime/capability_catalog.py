"""Runtime Capability Catalog.

Merges Nerya's many capability surfaces (skills, native tools, MCP, ACP,
gateway commands, trading actions, memory providers, LLM providers, and
dashboard-visible readiness checks) into a single operator-facing list.

Each catalog entry uses the canonical runtime shape: id, name, domain,
kind, status, source, entrypoint, dashboard_path,
required_config, required_secrets, permissions, approval, live_trading_impact,
data_boundary, last_verified_at, last_error, operator_hint.

Contributors are pluggable: any module that can describe a slice of the
runtime returns a list of catalog entries. The catalog builder unions them,
sorts by domain + id, and is consumed by ``/capabilities/catalog`` and
``/capabilities/readiness`` HTTP routes.

This module is deliberately read-only and side-effect free. It does not
import the agent kernel or trading runtime at module import time so that
the routes endpoint stays cheap even when called per dashboard page load.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Public schema
# ---------------------------------------------------------------------------

CatalogStatus = str  # "ready" | "degraded" | "blocked" | "unavailable"
CatalogKind = str  # "skill" | "native_tool" | "mcp_tool" | "acp_method" | ...
ApprovalKind = str  # "none" | "operator" | "approval_gate" | "live_trading_gate"
LiveTradingImpact = str  # "none" | "read_only" | "paper" | "shadow" | "live"


@dataclass
class DataBoundary:
    secrets_visible_to_agent: bool = False
    external_network: bool = False
    data_leaves_device: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CapabilityEntry:
    id: str
    name: str
    domain: str
    kind: CatalogKind
    status: CatalogStatus = "ready"
    source: str = "native"
    entrypoint: str = ""
    dashboard_path: str = ""
    required_config: list[str] = field(default_factory=list)
    required_secrets: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    approval: ApprovalKind = "none"
    live_trading_impact: LiveTradingImpact = "none"
    data_boundary: DataBoundary = field(default_factory=DataBoundary)
    last_verified_at: str = ""
    last_error: Optional[str] = None
    operator_hint: str = ""
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        # data_boundary is a dataclass; asdict already unwrapped it but make
        # sure the schema is stable for older readers.
        out["data_boundary"] = self.data_boundary.as_dict()
        # never serialise None entry values that confuse downstream JSON
        if out.get("last_error") is None:
            out["last_error"] = None
        return out


CatalogContributor = Callable[[Any], list[CapabilityEntry]]
"""Contributor protocol: callable(client) -> list[CapabilityEntry].

Contributors are passed the :class:`InternalClient` so they can read live
runtime state (config, skills registry, gateway commands, etc.) and must
swallow their own errors so a broken sub-domain does not prevent the rest
of the catalog from rendering.
"""


# ---------------------------------------------------------------------------
# Built-in contributors
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # pragma: no cover - defensive
        return default


def _contributors_skills(client) -> list[CapabilityEntry]:
    """skill manifests -> catalog entries.

    A skill is a "ready" capability when the SkillKernel has loaded it
    without raising. We do not try to inspect each script's prerequisites
    here — that would be expensive on a per-page-load endpoint.
    """

    skills = getattr(client, "skills", None)
    if skills is None:
        return []
    out: list[CapabilityEntry] = []
    entries = _safe(lambda: list(skills.registry.list()), [])
    for entry in entries:
        manifest = getattr(entry, "manifest", None)
        if manifest is None:
            continue
        sid = getattr(manifest, "id", "") or ""
        if not sid:
            continue
        out.append(CapabilityEntry(
            id=f"skill.{sid}",
            name=getattr(manifest, "title", "") or sid,
            domain="skills",
            kind="skill",
            status="ready",
            source="skill",
            entrypoint=f"nerya.skills.builtin.{sid}",
            dashboard_path=f"/skills?id={sid}",
            last_verified_at=_now_iso(),
            operator_hint=(getattr(manifest, "description", "") or "").strip(),
            tags=["skill", sid],
        ))
    return out


def _contributors_gateway_commands(client) -> list[CapabilityEntry]:
    try:
        from ..api.gateway_commands import DEFAULT_REGISTRY
    except Exception:
        return []
    out: list[CapabilityEntry] = []
    for cmd in _safe(lambda: DEFAULT_REGISTRY.menu(), []):
        cid = cmd.get("name") or cmd.get("id") or ""
        if not cid:
            continue
        out.append(CapabilityEntry(
            id=f"gateway.{cid}",
            name=cmd.get("title") or cid,
            domain="gateway",
            kind="gateway_command",
            status="ready",
            source="gateway",
            entrypoint=f"gateway.commands.{cid}",
            dashboard_path="/gateway",
            operator_hint=(cmd.get("description") or "").strip(),
            tags=["gateway", cid],
            last_verified_at=_now_iso(),
        ))
    return out


def _contributors_trading_actions(client) -> list[CapabilityEntry]:
    """Surface the core trading lifecycle actions.

    These are not skills; they are native runtime entrypoints. The catalog
    reports them with their approval level + live trading impact so the
    operator overview can render the correct remediation hints.
    """

    cfg = getattr(client, "config", None)
    if cfg is None:
        return []
    live_enabled = _safe(lambda: cfg.live_trading_enabled(), False)
    kill = _safe(lambda: cfg.kill_switch(), False)
    base: list[CapabilityEntry] = [
        CapabilityEntry(
            id="trading.submit_order",
            name="Submit order",
            domain="trading",
            kind="native_action",
            status="blocked" if kill else ("ready" if live_enabled else "degraded"),
            source="native",
            entrypoint="nerya.trading.submit",
            dashboard_path="/portfolio",
            permissions=["trade:paper"],
            approval="approval_gate",
            live_trading_impact="live" if live_enabled else "paper",
            data_boundary=DataBoundary(
                secrets_visible_to_agent=False,
                external_network=True,
                data_leaves_device=True,
            ),
            last_verified_at=_now_iso(),
            operator_hint=(
                "Kill switch engaged." if kill else
                ("Live trading enabled." if live_enabled
                 else "Paper trading only; live submit is gated.")
            ),
            tags=["trading", "submit"],
        ),
        CapabilityEntry(
            id="trading.cancel_order",
            name="Cancel order",
            domain="trading",
            kind="native_action",
            status="blocked" if kill else "ready",
            source="native",
            entrypoint="nerya.trading.cancel",
            dashboard_path="/portfolio",
            permissions=["trade:paper"],
            approval="operator",
            live_trading_impact="paper",
            data_boundary=DataBoundary(
                external_network=True, data_leaves_device=True),
            last_verified_at=_now_iso(),
            tags=["trading", "cancel"],
        ),
        CapabilityEntry(
            id="trading.account_snapshot",
            name="Account snapshot",
            domain="trading",
            kind="native_action",
            status="ready",
            source="native",
            entrypoint="nerya.trading.accounts",
            dashboard_path="/accounts",
            permissions=["read:runtime"],
            approval="none",
            live_trading_impact="read_only",
            data_boundary=DataBoundary(external_network=True),
            last_verified_at=_now_iso(),
            tags=["trading", "account"],
        ),
    ]
    return base


def _contributors_llm_providers(client) -> list[CapabilityEntry]:
    cfg = getattr(client, "config", None)
    if cfg is None:
        return []
    tiers = _safe(lambda: cfg.get("llm.tiers") or {}, {})
    out: list[CapabilityEntry] = []
    for name, raw in tiers.items():
        spec = raw or {}
        provider = (spec.get("provider") or "").lower()
        model = spec.get("model") or ""
        has_key = bool(
            spec.get("provider_key_ref") or spec.get("provider_key_env")
        )
        ready = bool(provider and (has_key or provider == "mock"))
        out.append(CapabilityEntry(
            id=f"llm.tier.{name}",
            name=f"LLM tier '{name}'",
            domain="llm",
            kind="llm_tier",
            status="ready" if ready else "unavailable",
            source="native",
            entrypoint="nerya.llm.model_router",
            dashboard_path=f"/settings?section=integrations&tier={name}",
            required_secrets=(
                [spec["provider_key_ref"]]
                if isinstance(spec.get("provider_key_ref"), str) else []
            ),
            permissions=["read:runtime"],
            data_boundary=DataBoundary(external_network=True, data_leaves_device=True),
            last_verified_at=_now_iso(),
            operator_hint=(
                f"{provider}/{model} configured."
                if ready else
                f"Tier '{name}' missing provider key."
            ),
            tags=["llm", provider, name],
        ))
    return out


def _contributors_memory(client) -> list[CapabilityEntry]:
    cfg = getattr(client, "config", None)
    if cfg is None:
        return []
    backend = _safe(lambda: cfg.get("memory.backend") or "filesystem", "filesystem")
    return [CapabilityEntry(
        id=f"memory.{backend}",
        name=f"Memory backend ({backend})",
        domain="memory",
        kind="memory_provider",
        status="ready",
        source="native",
        entrypoint="nerya.memory.provider",
        dashboard_path="/memory",
        last_verified_at=_now_iso(),
        operator_hint=f"Active backend: {backend}.",
        tags=["memory", backend],
    )]


def _flag_enabled(client, key: str, default: bool = True) -> bool:
    """Single source of truth for runtime feature flags.

    Reads through ``nerya.runtime.feature_flags`` so the capability catalog
    and the HTTP route gating agree on which surfaces are live. Falls back
    to ``default`` if the flag module is not importable (e.g. mid-build).
    """

    try:
        from . import feature_flags as ff
        return bool(ff.is_enabled(client, key))
    except Exception:  # pragma: no cover - defensive
        return default


def _contributors_evidence_vault(client) -> list[CapabilityEntry]:
    enabled = _flag_enabled(client, "runtime.evidence_vault", default=True)
    return [CapabilityEntry(
        id="evidence.vault",
        name="Trading Evidence Vault",
        domain="memory",
        kind="evidence_store",
        status="ready" if enabled else "degraded",
        source="native",
        entrypoint="nerya.evidence.store",
        dashboard_path="/memory?tab=evidence",
        last_verified_at=_now_iso(),
        operator_hint=(
            "Evidence vault active." if enabled
            else "Evidence vault is disabled (flag runtime.evidence_vault off)."
        ),
        tags=["memory", "evidence"],
    )]


def _contributors_runtime_flag_surfaces(client) -> list[CapabilityEntry]:
    """One catalog entry per remaining runtime surface.

    Each surface uses :func:`_flag_enabled` so the catalog and HTTP route
    gating agree. ``evidence.vault`` is handled by its own contributor.
    """

    surfaces = (
        # (capability_id, name, kind, dashboard_path, flag_key)
        ("memory.operator_profile", "Operator preference profile", "profile_store",
         "/settings?section=memory&sub=profile", "runtime.operator_profile"),
        ("security.prompt_guard_review", "Prompt guard review queue", "review_queue",
         "/inbox", "runtime.prompt_guard_review_queue"),
        ("runtime.tool_result_compaction", "Tool result compaction", "tool_middleware",
         "/settings?section=runtime", "runtime.tool_result_compaction"),
        ("ops.e2e_artifact_capture", "End-to-end artifact capture", "ops_artifact",
         "/ops/e2e", "runtime.e2e_artifact_capture"),
        ("runtime.capability_catalog_v2", "Capability catalog (operator view)",
         "catalog_view", "/settings?section=runtime",
         "runtime.capability_catalog_v2"),
        ("runtime.data_source_sync_state", "Data-source sync state",
         "sync_state", "/settings?section=integrations",
         "runtime.data_source_sync_state"),
    )
    out: list[CapabilityEntry] = []
    for cap_id, name, kind, path, flag_key in surfaces:
        enabled = _flag_enabled(client, flag_key, default=True)
        domain = (
            "memory" if cap_id.startswith("memory.") else
            "security" if cap_id.startswith("security.") else
            "ops" if cap_id.startswith("ops.") else
            "runtime"
        )
        out.append(CapabilityEntry(
            id=cap_id,
            name=name,
            domain=domain,
            kind=kind,
            status="ready" if enabled else "degraded",
            source="native",
            entrypoint=f"feature_flag:{flag_key}",
            dashboard_path=path,
            last_verified_at=_now_iso(),
            operator_hint=(
                f"{name} active." if enabled
                else f"{name} is disabled (flag {flag_key} off)."
            ),
            tags=["runtime_flag", flag_key],
        ))
    return out


def _contributors_data_sources(client) -> list[CapabilityEntry]:
    try:
        from ..data_sources import sync_state as ss
        status = ss.summarize(client)
    except Exception:
        return []
    out: list[CapabilityEntry] = []
    for src in status.get("sources", []):
        sid = src.get("source_id") or ""
        if not sid:
            continue
        stale = src.get("stale", False)
        enabled = src.get("enabled", True)
        if not enabled:
            sstatus = "unavailable"
        elif stale:
            sstatus = "degraded"
        else:
            sstatus = "ready"
        out.append(CapabilityEntry(
            id=f"data_source.{sid}",
            name=f"Data source {sid}",
            domain="data",
            kind="data_source",
            status=sstatus,
            source=src.get("provider", "native"),
            entrypoint="nerya.data_sources",
            dashboard_path="/settings?section=integrations",
            last_verified_at=src.get("last_success_at") or _now_iso(),
            last_error=src.get("last_error"),
            operator_hint=(
                f"Stale (sla={src.get('freshness_sla_seconds')}s); refresh recommended."
                if stale else f"Fresh; next due at {src.get('next_due_at', '?')}."
            ),
            tags=["data_source", src.get("kind", "")],
        ))
    return out


# Order is significant — domains render in this order in the catalog.
_BUILTIN_CONTRIBUTORS: tuple[CatalogContributor, ...] = (
    _contributors_skills,
    _contributors_gateway_commands,
    _contributors_trading_actions,
    _contributors_llm_providers,
    _contributors_memory,
    _contributors_evidence_vault,
    _contributors_runtime_flag_surfaces,
    _contributors_data_sources,
)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def build_catalog(
    client,
    *,
    contributors: tuple[CatalogContributor, ...] = _BUILTIN_CONTRIBUTORS,
) -> list[CapabilityEntry]:
    """Build the full capability catalog from registered contributors."""

    out: list[CapabilityEntry] = []
    seen: set[str] = set()
    for contrib in contributors:
        for entry in _safe(lambda c=contrib: c(client), []):
            if entry.id in seen:
                continue
            seen.add(entry.id)
            out.append(entry)
    out.sort(key=lambda e: (e.domain, e.id))
    return out


def readiness(
    client,
    *,
    contributors: tuple[CatalogContributor, ...] = _BUILTIN_CONTRIBUTORS,
) -> dict[str, Any]:
    """Roll up the catalog into a small "what is blocked" view."""

    catalog = build_catalog(client, contributors=contributors)
    counts: dict[str, int] = {"ready": 0, "degraded": 0, "blocked": 0, "unavailable": 0}
    blocked_items: list[dict[str, Any]] = []
    degraded_items: list[dict[str, Any]] = []
    for entry in catalog:
        counts[entry.status] = counts.get(entry.status, 0) + 1
        if entry.status == "blocked":
            blocked_items.append({
                "id": entry.id, "name": entry.name,
                "operator_hint": entry.operator_hint,
                "dashboard_path": entry.dashboard_path,
            })
        elif entry.status in ("degraded", "unavailable"):
            degraded_items.append({
                "id": entry.id, "name": entry.name,
                "status": entry.status,
                "operator_hint": entry.operator_hint,
                "dashboard_path": entry.dashboard_path,
            })
    return {
        "total": len(catalog),
        "counts": counts,
        "blocked": blocked_items,
        "degraded": degraded_items,
    }


def find(client, capability_id: str) -> Optional[CapabilityEntry]:
    """Return a single catalog entry by id (or None)."""
    for entry in build_catalog(client):
        if entry.id == capability_id:
            return entry
    return None
