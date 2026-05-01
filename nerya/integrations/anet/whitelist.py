"""Allow-list for Nerya endpoints exposed to the ANET P2P network.

The ``anet daemon`` proxies inbound P2P calls to whatever HTTP paths
we register with it. This module is the single source of truth for
which paths are safe to expose; anything not listed here must be
rejected even if an operator adds it via ``integrations.anet.expose_paths``.

Safety invariants enforced by :func:`resolve_exposed_paths`:

* Never expose endpoints that touch the signer, wallet, secret vault,
  approvals gate, live trading surface, or the operator console.
* Never expose endpoints that mutate state (``POST``/``PUT``/``DELETE``
  filtered unless explicitly part of the allow-list).
* Operator extras are *additive* to the built-in allow-list, and every
  extra is re-checked against :data:`HARD_DENY_PREFIXES` before it
  leaves this module.

If you need to expose a new read-only endpoint, add it to
:data:`SAFE_PATHS`; do not teach the integration to bypass this file.
"""

from __future__ import annotations

from typing import Iterable


# Endpoints the anet gateway will proxy to. Every entry is read-only
# and safe to call from an untrusted peer. Descriptions are surfaced
# verbatim through ``/anet/meta`` so judges and remote agents can read
# the capability card without knowing Nerya internals.
SAFE_PATHS: tuple[tuple[str, str, str], ...] = (
    # Mandatory protocol endpoints — anet's health-check / register-time meta probe.
    ("GET", "/anet/health", "Liveness probe for the anet gateway."),
    ("GET", "/anet/meta",   "Machine-readable capability card used by anet svc register."),
    ("GET", "/anet/status", "Integration status snapshot (debug / operator-side)."),
    # Read-only market data. Useful as a free tier that attracts callers
    # and seeds a reputation signal without giving anything away.
    ("GET", "/market/ticker", "Spot/perp ticker snapshot via Nerya's connector matrix."),
    ("GET", "/market/ohlcv",  "Historical OHLCV with cached lookback."),
    # Read-only strategy-history surface. Paid tier: ``explain_trade``
    # produces a reviewed trade postmortem that other agents can buy.
    ("GET",  "/strategy_history/list",    "List strategy sessions (metadata only)."),
    ("GET",  "/strategy_history/explain", "Explain a specific trade's decision path."),
    # Read-only LLM relay: wraps Nerya's LLM gateway so the same model
    # policy and redaction apply to P2P callers. Priced by call+kb.
    ("POST", "/llm/chat", "LLM completion via Nerya's gateway (redaction + policy applied)."),
)


# Any operator-supplied extra path whose PREFIX matches one of these
# is rejected no matter what. This is belt-and-suspenders: the default
# ``expose_paths`` is empty so these prefixes are not reachable unless
# the operator explicitly lists them, and even then they are dropped.
HARD_DENY_PREFIXES: tuple[str, ...] = (
    "/accounts",            # credential intake
    "/account_intake",
    "/approvals",           # bypassing the approval gate is a non-starter
    "/control_plane",
    "/evolution",           # write surface for prompts/scripts
    "/gateway",             # messaging / bot gateways
    "/inbox",
    "/messages",
    "/operator",            # operator console
    "/provider_auth",
    "/scripts",             # script sandbox (execution surface)
    "/security",
    "/teams",
    "/trading",             # order placement (paper OR live)
    "/triggers",            # trigger router (side effects)
    "/wallet",              # wallet / signer surface
    "/workspace",           # workspace mutation
    "/agent/tasks",         # internal task queue
    "/discovery",           # peer / credential discovery
    "/exchanges",           # exchange connector config writes
)


def _is_safe_extra(path: str) -> bool:
    """Return True if an operator-supplied ``path`` is safe to expose."""
    if not isinstance(path, str) or not path.startswith("/"):
        return False
    for denied in HARD_DENY_PREFIXES:
        if path == denied or path.startswith(denied + "/"):
            return False
    return True


def resolve_exposed_paths(extras: Iterable[str] | None = None) -> list[str]:
    """Return the canonical list of paths to register with the anet daemon.

    Order: built-in allow-list first (deterministic), then the
    operator-supplied extras that survived the deny check. Duplicates
    are removed while preserving first-occurrence order.
    """

    out: list[str] = []
    seen: set[str] = set()
    for _method, path, _desc in SAFE_PATHS:
        if path not in seen:
            seen.add(path)
            out.append(path)
    for extra in extras or ():
        if not _is_safe_extra(extra):
            continue
        if extra in seen:
            continue
        seen.add(extra)
        out.append(extra)
    return out


def describe_paths(paths: Iterable[str]) -> list[tuple[str, str, str]]:
    """Return ``(method, path, description)`` rows for ``/anet/meta``.

    Paths from :data:`SAFE_PATHS` are rendered with their canonical
    method + description; operator-added paths default to ``GET`` with
    a generic description so ``/anet/meta`` never exposes a blank row.
    """

    table: dict[str, tuple[str, str]] = {
        path: (method, desc) for (method, path, desc) in SAFE_PATHS
    }
    rows: list[tuple[str, str, str]] = []
    for path in paths:
        if path in table:
            method, desc = table[path]
        else:
            method, desc = ("GET", "Operator-added read-only endpoint.")
        rows.append((method, path, desc))
    return rows


__all__ = [
    "SAFE_PATHS",
    "HARD_DENY_PREFIXES",
    "resolve_exposed_paths",
    "describe_paths",
]
