"""Production preflight / startup health checks (Phases 5 & 6).

This module consolidates the runtime preconditions that an operator
needs to verify before a Nerya deployment can honestly claim to be
``prod_paper``, ``canary_live`` or ``full_live``:

* ``TA-Lib`` must be installed when production policy requires native
  indicators (``runtime.require_talib``);
* LLM providers for each configured tier must have real API keys (via
  env refs) when the tier is declared as a production tier;
* Workspace filesystem layout must be writable;
* Live-trading must not be enabled simultaneously with ``mock_mode``.

The checks are expressed as individual :class:`Check` objects so the
output can drive either:

* a CLI ``nerya preflight`` command,
* an HTTP ``/ops/preflight`` endpoint,
* or a unit-test assertion.

Operators choose how strict the result is:

* ``run_preflight(mode="prod_paper")`` — must be clean to boot paper,
* ``run_preflight(mode="canary_live")`` — tightens checks for live,
* ``run_preflight(mode="full_live")`` — strictest profile.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from ..core.config import Config

Mode = Literal["local_dev", "prod_paper", "canary_live", "full_live"]


@dataclass
class Check:
    name: str
    status: str  # "pass" | "fail" | "warn" | "skip"
    detail: str = ""
    required_for: tuple[str, ...] = ()

    def asdict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "required_for": list(self.required_for),
        }


@dataclass
class PreflightReport:
    mode: Mode
    checks: list[Check] = field(default_factory=list)

    def ok(self) -> bool:
        return all(c.status != "fail" for c in self.checks)

    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == "warn"]

    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]

    def asdict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "ok": self.ok(),
            "checks": [c.asdict() for c in self.checks],
            "warnings": [c.asdict() for c in self.warnings()],
            "failures": [c.asdict() for c in self.failures()],
        }


# ---------------------------------------------------------------- indicators
def _check_talib(cfg: Config, mode: Mode) -> Check:
    required = bool(cfg.get("runtime.require_talib", False))
    from ..data import indicators as ind  # noqa: E402 — ops is allowed to reach into data
    installed = ind.has_talib()
    if not required:
        return Check(
            name="indicators.talib",
            status="pass" if installed else "warn",
            detail=(
                "TA-Lib installed" if installed
                else "TA-Lib not installed — pure-Python fallback in use "
                     "(set runtime.require_talib=true to enforce)."
            ),
        )
    return Check(
        name="indicators.talib",
        status="pass" if installed else "fail",
        detail=(
            "TA-Lib native backend available"
            if installed
            else "runtime.require_talib=true but TA-Lib is not installed"
        ),
        required_for=("prod_paper", "canary_live", "full_live"),
    )


# ------------------------------------------------------------------- llm keys
def _env_ref_resolved(ref: str | None) -> bool:
    if not ref:
        return False
    if isinstance(ref, str) and ref.startswith("env:"):
        var = ref[len("env:"):].strip()
        return bool(os.environ.get(var))
    return True  # literal secret assumed present


def _check_llm_keys(cfg: Config, mode: Mode) -> list[Check]:
    """Validate route-aware provider credentials for every configured tier."""

    from ..llm.ops import effective_tiers, provider_readiness
    from ..llm.route_candidates import configured_routes

    tiers = effective_tiers(cfg)
    readiness = {
        str(row.get("provider") or "").strip().lower(): row
        for row in provider_readiness(cfg).get("providers", [])
    }
    out: list[Check] = []
    for name, tier in tiers.items():
        routes = configured_routes(tier)
        if not routes:
            out.append(Check(
                name=f"llm.tier.{name}",
                status="warn" if mode == "local_dev" else "fail",
                detail=f"tier {name} has no provider/model routes",
                required_for=("prod_paper", "canary_live", "full_live"),
            ))
            continue

        route_states: list[str] = []
        ready_count = 0
        for route in routes:
            provider = str(route.get("provider") or "mock").strip().lower()
            provider_row = readiness.get(provider) or {}
            env_name = str(route.get("provider_key_env") or "").strip()
            env_ready = bool(env_name and os.environ.get(env_name))
            route_ready = (
                provider != "mock"
                and (
                    bool(provider_row.get("ready"))
                    or bool(route.get("provider_key_ref"))
                    or env_ready
                )
            )
            ready_count += int(route_ready)
            route_states.append(f"{provider}:{'ready' if route_ready else 'missing_key'}")

        status = "pass" if ready_count else ("warn" if mode == "local_dev" else "fail")
        out.append(Check(
            name=f"llm.tier.{name}",
            status=status,
            detail=(
                f"tier {name} ready_routes={ready_count}/{len(routes)} "
                + ", ".join(route_states)
            ),
            required_for=("prod_paper", "canary_live", "full_live") if not ready_count else (),
        ))
    return out


# -------------------------------------------------------------- mock mode vs live
def _check_live_mock_conflict(cfg: Config, mode: Mode) -> Check:
    mock_on = (
        bool(cfg.get("runtime.mock_mode", False))
        or os.environ.get("NERYA_ALLOW_MOCK_DATA", "").lower()
        in ("1", "true", "yes", "on")
    )
    live_on = cfg.live_trading_enabled()
    if live_on and mock_on:
        return Check(
            name="runtime.live_vs_mock",
            status="fail",
            detail="live_trading_enabled=true while mock_mode is authorised",
            required_for=("canary_live", "full_live"),
        )
    return Check(
        name="runtime.live_vs_mock",
        status="pass",
        detail=f"live={live_on} mock={mock_on}",
    )


def _check_live_enabled(cfg: Config, mode: Mode) -> Check:
    live_on = cfg.live_trading_enabled()
    strict = mode in ("canary_live", "full_live")
    return Check(
        name="runtime.live_enabled",
        status="pass" if live_on or not strict else "fail",
        detail=f"live_trading_enabled={live_on}",
        required_for=("canary_live", "full_live") if strict else (),
    )


def _check_live_accounts(cfg: Config, mode: Mode) -> Check:
    """Require at least one real-money account that can actually trade."""

    if mode not in ("canary_live", "full_live"):
        return Check(
            name="accounts.live_ready",
            status="skip",
            detail=f"not required in mode={mode}",
        )
    from ..trading.accounts import load_account_profiles

    try:
        profiles = load_account_profiles(cfg.paths)
    except Exception as exc:
        return Check(
            name="accounts.live_ready",
            status="fail",
            detail=f"cannot load account profiles: {type(exc).__name__}:{exc}",
            required_for=("canary_live", "full_live"),
        )

    real_accounts = [profile for profile in profiles.values() if profile.is_real_money]
    if not real_accounts:
        return Check(
            name="accounts.live_ready",
            status="fail",
            detail="no canary/live account configured",
            required_for=("canary_live", "full_live"),
        )

    ready: list[str] = []
    blocked: list[str] = []
    for profile in real_accounts:
        reasons: list[str] = []
        if profile.status != "active":
            reasons.append(f"status={profile.status}")
        if not profile.live_trading_enabled:
            reasons.append("live_disabled")
        if not profile.permissions.read_balances:
            reasons.append("read_balances=false")
        if not profile.permissions.place_order:
            reasons.append("place_order=false")
        if profile.kind == "cex" and not profile.permissions.cancel_order:
            reasons.append("cancel_order=false")
        if reasons:
            blocked.append(f"{profile.id}({','.join(reasons)})")
        else:
            ready.append(profile.id)

    return Check(
        name="accounts.live_ready",
        status="pass" if ready else "fail",
        detail=(
            f"ready={ready or []}; blocked={blocked or []}"
        ),
        required_for=("canary_live", "full_live"),
    )


# ---------------------------------------------------------------- workspace
def _check_workspace(cfg: Config, mode: Mode) -> Check:
    root: Path = cfg.paths.root
    missing: list[str] = []
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return Check(
            name="workspace.root",
            status="fail",
            detail=f"cannot create workspace {root}: {exc}",
            required_for=("prod_paper", "canary_live", "full_live"),
        )
    candidates = [
        cfg.paths.memory,
        cfg.paths.strategies,
        cfg.paths.triggers_dir,
    ]
    # journals directory sits under the root; use a throwaway sentinel
    try:
        journal_parent = cfg.paths.journal("triggers").parent
        candidates.append(journal_parent)
    except Exception:
        pass
    for p in candidates:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            missing.append(f"{p}:{exc}")
    if missing:
        return Check(
            name="workspace.root",
            status="fail",
            detail="missing or unwritable: " + "; ".join(missing),
            required_for=("prod_paper", "canary_live", "full_live"),
        )
    return Check(name="workspace.root", status="pass", detail=str(root))


# ---------------------------------------------------------------- kill switch
def _check_kill_switch(cfg: Config, mode: Mode) -> Check:
    ks = cfg.kill_switch()
    if ks and mode in ("canary_live", "full_live"):
        return Check(
            name="runtime.kill_switch",
            status="fail",
            detail="kill_switch is active — live trading would be blocked",
            required_for=("canary_live", "full_live"),
        )
    return Check(
        name="runtime.kill_switch",
        status="pass",
        detail=f"kill_switch={ks}",
    )


# ---------------------------------------------------------------- capability gaps
def _check_capability_gaps(cfg: Config, mode: Mode) -> Check:
    from ..llm.capability_matrix import capability_of
    tiers = cfg.get("llm.tiers") or {}
    gaps: list[str] = []
    for name, tier in (tiers or {}).items():
        provider = (tier or {}).get("provider") or "mock"
        cap = capability_of(provider)
        for capability_name, level in cap.tiers.items():
            if level == "experimental" and mode in ("canary_live", "full_live"):
                gaps.append(f"{name}:{capability_name}={level}")
            elif level == "unsupported" and capability_name == "schema_json_mode":
                gaps.append(f"{name}:{capability_name}=unsupported")
    status = "pass"
    if gaps and mode in ("canary_live", "full_live"):
        status = "warn"
    return Check(
        name="llm.capability_gaps",
        status=status,
        detail="; ".join(gaps) if gaps else "no flagged gaps",
    )


# ---------------------------------------------------------------- connector reachability
def _check_connector_reachability(cfg: Config, mode: Mode) -> list[Check]:
    """Probe each account using the transport appropriate to its account kind."""

    if mode == "local_dev":
        return []
    from ..connectors.registry import build_connector
    from ..trading.accounts import load_account_profiles

    try:
        profiles = load_account_profiles(cfg.paths)
    except Exception as exc:
        return [Check(
            name="connectors.registry",
            status="fail",
            detail=f"cannot load accounts: {exc}",
            required_for=("prod_paper", "canary_live", "full_live"),
        )]
    if not profiles:
        return [Check(
            name="connectors.registry",
            status="warn" if mode == "prod_paper" else "fail",
            detail="no accounts configured",
            required_for=("canary_live", "full_live"),
        )]

    out: list[Check] = []
    for profile in profiles.values():
        venue = (profile.venue or profile.provider_spec or "").lower()
        if venue in ("", "mock", "paper", "mock_chain"):
            out.append(Check(
                name=f"connectors.reachability.{profile.id}",
                status="skip",
                detail=f"account {profile.id!r} uses mock/paper venue",
            ))
            continue

        strict_account = (
            mode in ("canary_live", "full_live")
            and profile.is_real_money
        )
        wallet_bound = bool(
            profile.wallet_id and profile.kind in ("chain", "dex")
        )
        if wallet_bound:
            probed, detail = _probe_wallet_account(cfg, profile)
        else:
            probe_cfg = profile.to_connector_account(live=False).connector_cfg()
            probe_cfg["live"] = False
            try:
                conn = build_connector(probe_cfg, workspace=cfg.paths.root)
            except Exception as exc:
                probed = False
                detail = f"build_connector failed: {type(exc).__name__}:{exc}"
            else:
                probed, detail = _probe_connector_public(conn, profile)

        status = "pass" if probed else ("fail" if strict_account else "warn")
        out.append(Check(
            name=f"connectors.reachability.{profile.id}",
            status=status,
            detail=detail,
            required_for=("canary_live", "full_live") if strict_account and not probed else (),
        ))
    return out


def _probe_wallet_account(cfg: Config, profile: Any) -> tuple[bool, str]:
    """Read a non-persisted wallet snapshot; never signs or broadcasts."""

    try:
        from ..trading.account_snapshots import capture_snapshot

        snapshot = capture_snapshot(
            cfg,
            profile.id,
            profile=profile,
            persist=False,
        )
    except Exception as exc:
        return False, f"wallet snapshot failed: {type(exc).__name__}:{exc}"
    health = str(getattr(snapshot, "health", "") or "")
    source = str(getattr(snapshot, "source", "") or "")
    if health != "ok":
        meta = dict(getattr(snapshot, "meta", {}) or {})
        reason = meta.get("error") or meta.get("reason") or "unhealthy snapshot"
        return False, f"wallet snapshot health={health} source={source}: {reason}"
    return True, (
        f"wallet snapshot ok source={source} "
        f"nav_usd={float(getattr(snapshot, 'nav_usd', 0.0) or 0.0):.2f}"
    )


def _probe_connector_public(conn: Any, acc: Any) -> tuple[bool, str]:
    """Cheapest public read a connector can do — returns (ok, detail)."""

    client = getattr(conn, "client", None)
    if client is not None and hasattr(client, "fetch_time"):
        try:
            ts = client.fetch_time()
            return True, f"fetch_time ok (server_ts={ts})"
        except Exception as exc:
            return False, f"fetch_time failed: {type(exc).__name__}:{exc}"

    # Chain-native connectors often inherit a generic get_ticker that raises.
    # Prefer their actual RPC liveness method before trying market symbols.
    for meth_name in (
        "get_slot",
        "get_block_number",
        "block_number",
        "get_latest_block",
        "getblockcount",
    ):
        meth = getattr(conn, meth_name, None)
        if callable(meth):
            try:
                height = meth()
                return True, f"{meth_name}() ok (height={height})"
            except Exception as exc:
                return False, f"{meth_name}() failed: {type(exc).__name__}:{exc}"

    if hasattr(conn, "get_ticker"):
        raw = getattr(acc, "raw", None) or {}
        probe_market = raw.get("probe_market")
        if not probe_market:
            venue = str(
                getattr(acc, "venue", None)
                or getattr(acc, "provider_spec", None)
                or ""
            ).lower()
            probe_market = "BTC-USD" if venue == "yahoo" else "BTC/USDT"
        try:
            tick = conn.get_ticker(probe_market)
            return True, f"get_ticker {probe_market} ok (last={getattr(tick, 'last', '?')})"
        except Exception as exc:
            return False, (
                f"get_ticker {probe_market} failed: "
                f"{type(exc).__name__}:{exc}"
            )
    return False, f"connector {type(conn).__name__} exposes no probe method"


# ------------------------------------------------------------ account credentials
def _check_account_credentials(cfg: Config, mode: Mode) -> list[Check]:
    """Construct every real-money private client or wallet read path."""

    if mode == "local_dev":
        return []
    from ..connectors.registry import build_connector
    from ..trading.accounts import load_account_profiles

    try:
        profiles = load_account_profiles(cfg.paths)
    except Exception as exc:
        return [Check(
            name="accounts.credentials",
            status="fail",
            detail=f"cannot load accounts: {exc}",
            required_for=("canary_live", "full_live"),
        )]

    out: list[Check] = []
    vault_passphrase = (
        os.environ.get("NERYA_VAULT_PASSPHRASE")
        or getattr(cfg, "vault_passphrase", None)
    )
    for profile in profiles.values():
        if not profile.is_real_money:
            continue
        wallet_bound = bool(
            profile.wallet_id and profile.kind in ("chain", "dex")
        )
        if wallet_bound:
            ready, detail = _probe_wallet_account(cfg, profile)
            out.append(Check(
                name=f"accounts.credentials.{profile.id}",
                status="pass" if ready else "fail",
                detail=detail,
                required_for=("canary_live", "full_live") if not ready else (),
            ))
            continue

        private_cfg = profile.to_connector_account(live=True).connector_cfg()
        try:
            build_connector(
                private_cfg,
                workspace=cfg.paths.root,
                vault_passphrase=vault_passphrase,
            )
        except Exception as exc:
            out.append(Check(
                name=f"accounts.credentials.{profile.id}",
                status="fail",
                detail=f"private connector build failed: {type(exc).__name__}:{exc}",
                required_for=("canary_live", "full_live"),
            ))
            continue
        out.append(Check(
            name=f"accounts.credentials.{profile.id}",
            status="pass",
            detail=f"{profile.id} private connector credentials resolved",
        ))
    return out


# -------------------------------------------------------------- llm provider smoke
_SMOKE_TASK_BY_CLASS = {
    "classification": "classify",
    "structured_extraction": "extract_json",
    "subagent_reasoning": "subagent_analysis",
    "strategy_review": "strategy_review",
    "proposal_generation": "script_generation",
    "complex_reasoning": "complex_signal_analysis",
    "content_compression": "compress",
    "agent_loop": "normal_agent_loop",
}


def _smoke_task_for_tier(tier: dict[str, Any]) -> str | None:
    tasks = [str(task) for task in (tier.get("allowed_tasks") or []) if str(task)]
    preferred = (
        "classify",
        "extract_json",
        "compress",
        "subagent_analysis",
        "strategy_review",
        "normal_agent_loop",
        "complex_signal_analysis",
        "script_generation",
    )
    for task in preferred:
        if task in tasks:
            return task
    if tasks:
        return tasks[0]
    for class_name in tier.get("allowed_classes") or []:
        task = _SMOKE_TASK_BY_CLASS.get(str(class_name))
        if task:
            return task
    return None


def _check_llm_provider_smoke(cfg: Config, mode: Mode) -> list[Check]:
    """Tiny policy-valid ping per tier — proves a route and key are live."""

    opt_in = bool(cfg.get("runtime.preflight.smoke_llm", False))
    if mode not in ("canary_live", "full_live") and not opt_in:
        return [Check(
            name="llm.provider_smoke",
            status="skip",
            detail=(
                f"llm smoke skipped in mode={mode} "
                "(set runtime.preflight.smoke_llm=true to force)"
            ),
        )]
    from ..llm.gateway import LLMGateway
    from ..llm.ops import effective_tiers
    from ..llm.route_candidates import configured_routes

    tiers = effective_tiers(cfg)
    if not tiers:
        return [Check(
            name="llm.provider_smoke",
            status="warn",
            detail="no llm.tiers configured",
        )]
    out: list[Check] = []
    gateway = LLMGateway(config=cfg)
    for tier_name, tier in tiers.items():
        routes = configured_routes(tier)
        providers = [str(route.get("provider") or "mock") for route in routes]
        task = _smoke_task_for_tier(tier)
        if not task:
            out.append(Check(
                name=f"llm.provider_smoke.{tier_name}",
                status="fail",
                detail=f"tier={tier_name} advertises no smokeable task/class",
                required_for=("canary_live", "full_live"),
            ))
            continue
        try:
            call = gateway.call(
                task=task,
                prompt="Health probe: reply with a minimal successful response.",
                caller="preflight",
                tier=tier_name,
                caller_allowed_tiers=[tier_name],
            )
            ok = bool(getattr(call, "raw", "")) or getattr(call, "parsed", None) is not None
            out.append(Check(
                name=f"llm.provider_smoke.{tier_name}",
                status="pass" if ok else "warn",
                detail=(
                    f"tier={tier_name} task={task} providers={providers} "
                    f"selected={getattr(call, 'provider', '?')}"
                ),
            ))
        except Exception as exc:
            out.append(Check(
                name=f"llm.provider_smoke.{tier_name}",
                status="fail",
                detail=(
                    f"tier={tier_name} task={task} providers={providers} "
                    f"probe error: {type(exc).__name__}:{exc}"
                ),
                required_for=("canary_live", "full_live"),
            ))
    return out


# ---------------------------------------------------------------- route truth
def _check_route_truth(cfg: Config, mode: Mode) -> Check:
    """Live-path routes must not depend on proposal-only / disabled targets."""
    if mode == "local_dev":
        return Check(name="routes.truth", status="skip",
                     detail="skipped in local_dev")
    from ..triggers.routes import load_routes
    try:
        routes = load_routes(cfg.paths, include_inactive=True)
    except Exception as exc:
        return Check(
            name="routes.truth", status="fail",
            detail=f"cannot load routes: {type(exc).__name__}:{exc}",
            required_for=("prod_paper", "canary_live", "full_live"),
        )
    active = [r for r in routes if r.is_active()]
    if mode in ("canary_live", "full_live"):
        live_on = cfg.live_trading_enabled()
        if live_on and not active:
            return Check(
                name="routes.truth", status="fail",
                detail="live_trading_enabled=true but no active routes",
                required_for=("canary_live", "full_live"),
            )
    return Check(name="routes.truth", status="pass",
                 detail=f"active={len(active)} total={len(routes)}")


# --------------------------------------------------------------- public entrypoint
Checker = Callable[[Config, Mode], Check | list[Check]]

DEFAULT_CHECKERS: tuple[Checker, ...] = (
    _check_workspace,
    _check_talib,
    _check_llm_keys,
    _check_live_mock_conflict,
    _check_live_enabled,
    _check_live_accounts,
    _check_kill_switch,
    _check_capability_gaps,
    _check_connector_reachability,
    _check_account_credentials,
    _check_llm_provider_smoke,
    _check_route_truth,
)


def run_preflight(cfg: Config, *, mode: Mode = "prod_paper",
                  checkers: tuple[Checker, ...] | None = None) -> PreflightReport:
    """Execute the configured preflight checks and return the report.

    ``mode`` controls which checks are allowed to degrade into a
    ``fail`` status. ``local_dev`` is permissive; ``canary_live`` and
    ``full_live`` progressively tighten the requirements.
    """
    report = PreflightReport(mode=mode)
    for checker in checkers or DEFAULT_CHECKERS:
        try:
            outcome = checker(cfg, mode)
        except Exception as exc:
            report.checks.append(Check(
                name=getattr(checker, "__name__", "unknown"),
                status="fail",
                detail=f"{type(exc).__name__}: {exc}",
            ))
            continue
        if isinstance(outcome, list):
            report.checks.extend(outcome)
        else:
            report.checks.append(outcome)
    return report


def require_ready(cfg: Config, *, mode: Mode = "prod_paper") -> PreflightReport:
    """Run :func:`run_preflight` and raise ``RuntimeError`` on any fail."""
    report = run_preflight(cfg, mode=mode)
    failures = report.failures()
    if failures:
        lines = ", ".join(f"{c.name}:{c.detail}" for c in failures)
        raise RuntimeError(
            f"preflight(mode={mode!r}) failed with {len(failures)} "
            f"blocker(s): {lines}"
        )
    return report
