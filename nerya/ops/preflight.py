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
from dataclasses import asdict, dataclass, field
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
    tiers = cfg.get("llm.tiers") or {}
    out: list[Check] = []
    for name, tier in (tiers or {}).items():
        provider = (tier or {}).get("provider") or "mock"
        if provider == "mock":
            out.append(Check(
                name=f"llm.tier.{name}",
                status="pass" if mode == "local_dev" else "fail",
                detail=f"tier {name} uses provider=mock",
                required_for=("prod_paper", "canary_live", "full_live"),
            ))
            continue
        key_ref = (
            (tier or {}).get("api_key_ref")
            or (tier or {}).get("api_key")
        )
        if _env_ref_resolved(key_ref):
            out.append(Check(
                name=f"llm.tier.{name}",
                status="pass",
                detail=f"tier {name} provider={provider} has key",
            ))
        else:
            out.append(Check(
                name=f"llm.tier.{name}",
                status="fail" if mode != "local_dev" else "warn",
                detail=(
                    f"tier {name} provider={provider} missing api_key_ref"
                    if not key_ref
                    else f"tier {name} env var {key_ref} is empty"
                ),
                required_for=("prod_paper", "canary_live", "full_live"),
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
    """Positive proof every configured venue can be reached.

    For every registered account we try the cheapest public read the
    venue exposes (``get_ticker`` on a conservative default market for
    CEX, ``eth_blockNumber`` for chains). The probe runs *without*
    credentials — it only proves network reachability + provider
    liveness. Credential smoke is a separate check.

    Skipped in ``local_dev`` because operators routinely boot Nerya
    offline. For ``prod_paper`` and stricter we insist on at least one
    reachable CEX venue so the rest of the preflight is meaningful.
    """
    if mode == "local_dev":
        return []
    from ..skills._connector_helpers import venue_of  # local — avoids cycle
    from ..trading.accounts import load_accounts
    try:
        accounts = load_accounts(cfg.paths)
    except Exception as exc:
        return [Check(
            name="connectors.registry",
            status="fail",
            detail=f"cannot load accounts: {exc}",
            required_for=("prod_paper", "canary_live", "full_live"),
        )]
    if not accounts:
        return [Check(
            name="connectors.registry",
            status="warn" if mode == "prod_paper" else "fail",
            detail="no accounts configured",
            required_for=("canary_live", "full_live"),
        )]
    out: list[Check] = []
    from ..connectors.registry import build_connector
    for acc in accounts.values():
        if (acc.venue or acc.exchange or "").lower() in ("", "mock", "paper"):
            out.append(Check(
                name=f"connectors.reachability.{acc.id}",
                status="skip",
                detail=f"account {acc.id!r} uses mock/paper venue",
            ))
            continue
        probe_cfg = dict(acc.connector_cfg())
        probe_cfg["live"] = False  # always public-path for reachability
        try:
            conn = build_connector(probe_cfg, workspace=cfg.paths.root)
        except Exception as exc:
            out.append(Check(
                name=f"connectors.reachability.{acc.id}",
                status="fail" if mode != "prod_paper" else "warn",
                detail=f"build_connector failed: {type(exc).__name__}:{exc}",
                required_for=("canary_live", "full_live"),
            ))
            continue
        probed, detail = _probe_connector_public(conn, acc)
        status = "pass" if probed else ("warn" if mode == "prod_paper" else "fail")
        out.append(Check(
            name=f"connectors.reachability.{acc.id}",
            status=status,
            detail=detail,
            required_for=("canary_live", "full_live") if not probed else (),
        ))
    return out


def _probe_connector_public(conn: Any, acc: Any) -> tuple[bool, str]:
    """Cheapest public read a connector can do — returns (ok, detail)."""
    # CEX: use the ccxt client directly if available to call fetch_time /
    # load_markets without placing a market-data order. Fall back to
    # connector-level get_ticker on a conservative market.
    client = getattr(conn, "client", None)
    if client is not None and hasattr(client, "fetch_time"):
        try:
            ts = client.fetch_time()
            return True, f"fetch_time ok (server_ts={ts})"
        except Exception as exc:
            return False, f"fetch_time failed: {type(exc).__name__}:{exc}"
    if hasattr(conn, "get_ticker"):
        # Pick a market: prefer a conservative default so we don't
        # accidentally hit a paused / delisted market on exotic venues.
        probe_market = (acc.raw.get("probe_market")
                        if getattr(acc, "raw", None) else None)
        if not probe_market:
            probe_market = "BTC/USDT"
        try:
            tick = conn.get_ticker(probe_market)
            return True, f"get_ticker {probe_market} ok (last={getattr(tick, 'last', '?')})"
        except Exception as exc:
            return False, (
                f"get_ticker {probe_market} failed: "
                f"{type(exc).__name__}:{exc}"
            )
    # Chain connectors: try block_number / get_block_number.
    for meth_name in ("get_block_number", "block_number", "get_latest_block"):
        meth = getattr(conn, meth_name, None)
        if callable(meth):
            try:
                bn = meth()
                return True, f"{meth_name}() ok (block={bn})"
            except Exception as exc:
                return False, (
                    f"{meth_name}() failed: {type(exc).__name__}:{exc}"
                )
    return False, f"connector {type(conn).__name__} exposes no probe method"


# ------------------------------------------------------------ account credentials
def _check_account_credentials(cfg: Config, mode: Mode) -> list[Check]:
    """Verify every live account has resolvable credentials.

    For ``canary_live``/``full_live`` we insist each account advertised
    as ``live=true`` can actually load its API key / secret from the
    vault or env. We do NOT perform a full authenticated call — that
    would cost money on some venues. The point is to fail fast on
    vault-misconfiguration drift.
    """
    if mode == "local_dev":
        return []
    from ..trading.accounts import load_accounts
    from ..connectors.registry import _resolve_ref
    try:
        accounts = load_accounts(cfg.paths)
    except Exception as exc:
        return [Check(
            name="accounts.credentials",
            status="fail",
            detail=f"cannot load accounts: {exc}",
            required_for=("canary_live", "full_live"),
        )]
    if not accounts:
        return []
    out: list[Check] = []
    vault_passphrase = getattr(cfg, "vault_passphrase", None)
    for acc in accounts.values():
        if (acc.venue or "").lower() in ("mock", "paper", ""):
            continue
        if not acc.is_live and mode == "prod_paper":
            out.append(Check(
                name=f"accounts.credentials.{acc.id}",
                status="skip",
                detail=f"{acc.id} runs in paper mode",
            ))
            continue
        key_ref = acc.raw.get("api_key_ref")
        sec_ref = acc.raw.get("api_secret_ref")
        if not (key_ref or sec_ref):
            out.append(Check(
                name=f"accounts.credentials.{acc.id}",
                status="fail" if acc.is_live else "warn",
                detail=f"{acc.id} has no api_key_ref/api_secret_ref",
                required_for=("canary_live", "full_live"),
            ))
            continue
        def _resolve_any(ref: str | None) -> str | None:
            if not ref:
                return None
            if isinstance(ref, str) and ref.startswith("env:"):
                return os.environ.get(ref[len("env:"):].strip()) or None
            # vault://... path
            return _resolve_ref(ref, cfg.paths.root, vault_passphrase,
                                scope="exchange")
        try:
            key = _resolve_any(key_ref)
            sec = _resolve_any(sec_ref)
        except Exception as exc:
            out.append(Check(
                name=f"accounts.credentials.{acc.id}",
                status="fail" if acc.is_live else "warn",
                detail=f"credential resolve failed: {type(exc).__name__}:{exc}",
                required_for=("canary_live", "full_live"),
            ))
            continue
        if not key or not sec:
            out.append(Check(
                name=f"accounts.credentials.{acc.id}",
                status="fail" if acc.is_live else "warn",
                detail=(
                    f"{acc.id}: key_present={bool(key)} secret_present={bool(sec)}"
                ),
                required_for=("canary_live", "full_live"),
            ))
            continue
        out.append(Check(
            name=f"accounts.credentials.{acc.id}",
            status="pass",
            detail=f"{acc.id} credentials resolved (live={acc.is_live})",
        ))
    return out


# -------------------------------------------------------------- llm provider smoke
def _check_llm_provider_smoke(cfg: Config, mode: Mode) -> list[Check]:
    """Tiny ping per configured tier — proves the key is live.

    Runs only for ``canary_live`` / ``full_live`` by default because a
    real provider call costs a few fractions of a cent. Operators can
    opt in earlier by setting ``runtime.preflight.smoke_llm=true``.

    The smoke prompt is deliberately tiny ("ping") and caps tokens; we
    just want proof the key is alive + the model route is working.
    """
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
    tiers = cfg.get("llm.tiers") or {}
    if not tiers:
        return [Check(
            name="llm.provider_smoke",
            status="warn",
            detail="no llm.tiers configured",
        )]
    out: list[Check] = []
    from ..llm.gateway import LLMGateway
    gw = LLMGateway(config=cfg)
    for tier_name in tiers.keys():
        provider = (tiers.get(tier_name) or {}).get("provider") or "mock"
        if provider == "mock" and mode != "local_dev":
            out.append(Check(
                name=f"llm.provider_smoke.{tier_name}",
                status="fail",
                detail=f"tier {tier_name} still on provider=mock",
                required_for=("prod_paper", "canary_live", "full_live"),
            ))
            continue
        try:
            call = gw.call(
                task="preflight",
                prompt="SYSTEM: You are a health probe.\n\nreply OK",
                caller="preflight",
                tier=tier_name,
                caller_allowed_tiers=[tier_name],
            )
            ok = bool(getattr(call, "text", "") or "")
            out.append(Check(
                name=f"llm.provider_smoke.{tier_name}",
                status="pass" if ok else "warn",
                detail=(
                    f"tier={tier_name} provider={provider} "
                    f"latency_ms={getattr(call, 'latency_ms', '?')}"
                ),
            ))
        except Exception as exc:
            out.append(Check(
                name=f"llm.provider_smoke.{tier_name}",
                status="fail",
                detail=(
                    f"tier={tier_name} provider={provider} "
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
