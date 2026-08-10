"""Route → minimum-scope authorization matrix for the local API.

Until now every authenticated request implicitly received ``scope='api:all'``,
which meant a service token issued for "read-only inspection" could in fact
mutate workspace config, pull secrets, or call ``/trading/submit``. That is
fine for a single-operator loopback box but unsafe the moment the same HTTP
entrypoint is reused by gateways, dashboards behind the proxy, or remote
clients with a non-admin token.

This module introduces a small, declarative route → scope mapping that the
HTTP server consults *after* :func:`nerya.api.auth.check_request` has
authenticated the caller. The mapping uses three lookup levels:

1. **Exact match** on ``(method, path)`` — wins over any prefix rule.
2. **Method-aware prefix** like ``("GET", "/agent/")`` — matches any path
   starting with that prefix.
3. **Method-agnostic prefix** like ``(None, "/skills/lock/")`` — matches any
   verb under that prefix, used when read/write share the same scope.

Of all matching rules the *longest* path wins. That gives us the natural
"override the family default with a specific path" pattern (e.g. the whole
``/security/`` family is gated on ``read:runtime``, but
``/security/secrets/*`` requires ``write:secrets``).

The catalog below intentionally errs on the conservative side: an unknown
or new route falls back to ``admin:*`` so adding a route without thinking
about its scope cannot accidentally widen the surface.

The wildcard scope ``api:all`` (granted automatically to local loopback in
``local`` mode) bypasses every check — owners on the dev box keep working
as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


# ---- canonical scope catalog ----

ALL_SCOPES: frozenset[str] = frozenset({
    "read:runtime",      # health, capabilities, listings, discovery
    "read:sessions",     # transcript / event / approval inspection
    "write:chat",        # start a turn, send a message, queue an inbound
    "write:tools",       # execute a tool with side effects (operator/script)
    "write:memory",      # mutate persisted memory
    "write:skills",      # install / promote / lock-sign skills, evolution
    "write:config",      # workspace config / models / wallet / schedules
    "write:secrets",     # secrets vault, provider auth credentials
    "trade:paper",       # submit/cancel paper trades, swaps, wallets
    "trade:live",        # live exchange trade submit/cancel
    "approve:trade",     # respond to trade approvals
    "approve:tool",      # respond to tool approvals
    "gateway:webhook",   # inbound webhook (signed by platform)
    "gateway:send",      # outbound gateway send
    "admin:ops",         # ops/evidence/record + dev surface
    "api:all",           # wildcard, granted to loopback / owner tokens
})

WILDCARD_SCOPE = "api:all"

# Endpoints that never require auth (mirrors check_request behaviour).
ANONYMOUS_PATHS: frozenset[str] = frozenset({
    "/",
    "/health",
    "/auth/status",
    "/auth/login",
})


@dataclass(frozen=True)
class RouteRule:
    """A single declarative auth rule.

    Attributes
    ----------
    method:
        HTTP verb in upper-case, or ``None`` to match any verb.
    path:
        Exact path or path prefix. A trailing ``"/"`` flags this as a
        prefix rule; an exact match (no trailing slash, e.g. ``"/skills"``)
        only fires for that single path.
    scope:
        The minimum scope required. ``None`` means "no scope check
        beyond authentication" (used for endpoints we explicitly
        publish to every authenticated caller).
    note:
        Optional human-readable rationale, surfaced via
        :func:`describe_matrix` for the capability matrix endpoint.
    """

    method: Optional[str]
    path: str
    scope: Optional[str]
    note: str = ""


# ---- the matrix itself ----
#
# Order does not matter functionally because we pick the longest path
# match, but we group by feature for readability and audit. New routes
# default to ``admin:ops`` if no rule matches them.

_RULES: tuple[RouteRule, ...] = (
    # health / ops
    RouteRule("GET", "/health", None, "always anonymous"),
    RouteRule("GET", "/", None, "service banner"),
    RouteRule("GET", "/auth/status", None, "auth bootstrap status"),
    RouteRule("POST", "/auth/status", None, "auth bootstrap status"),
    RouteRule("POST", "/auth/login", None, "password-to-JWT exchange"),
    RouteRule("POST", "/auth/admin/password", "write:config", "set or rotate admin password"),
    RouteRule("POST", "/auth/logout", "read:runtime", "stateless JWT logout"),
    RouteRule("GET", "/ops/preflight", "read:runtime", ""),
    RouteRule("GET", "/ops/certify", "read:runtime", ""),
    RouteRule("GET", "/ops/evidence", "read:runtime", ""),
    RouteRule("POST", "/ops/evidence/record", "admin:ops", ""),

    # workspace
    RouteRule(None, "/workspace", "read:runtime", ""),
    RouteRule(None, "/workspace/", "read:runtime", "writes still pass through specific routes"),
    RouteRule("POST", "/workspace/sync/config", "write:config", "configure Git/WebDAV workspace sync"),
    RouteRule("POST", "/workspace/sync/run", "admin:ops", "operator-only workspace snapshot restore/publish"),
    RouteRule("POST", "/workspace/file/save", "write:config", "dashboard files drawer save"),
    RouteRule("POST", "/workspace/file/delete", "write:config", "dashboard files drawer delete"),
    RouteRule("POST", "/workspace/file/create", "write:config", "dashboard files drawer create"),
    RouteRule("POST", "/workspace/file/rename", "write:config", "dashboard files drawer rename"),

    # agent / sessions / streaming
    RouteRule("POST", "/agent/run_turn", "write:chat", ""),
    RouteRule("POST", "/agent/attachments/upload", "write:chat", ""),
    RouteRule("POST", "/agent/trace", "read:sessions", ""),
    RouteRule("POST", "/agent/explain", "read:sessions", ""),
    RouteRule("POST", "/agent/turn_state", "read:sessions", ""),
    RouteRule("GET", "/agent/open_turns", "read:sessions", ""),
    RouteRule("GET", "/agent/sessions", "read:sessions", ""),
    RouteRule("GET", "/agent/session", "read:sessions", ""),
    RouteRule("GET", "/agent/session/events", "read:sessions", ""),
    RouteRule("POST", "/agent/session/search", "read:sessions", ""),
    RouteRule("POST", "/agent/session/skill_state", "write:chat", ""),
    RouteRule("POST", "/agent/session/rename", "write:chat", ""),
    RouteRule("POST", "/agent/session/message/edit", "write:chat", ""),
    RouteRule("POST", "/agent/session/message/delete", "write:chat", ""),
    RouteRule("POST", "/agent/session/delete", "write:config", ""),
    RouteRule("GET", "/agent/stream/events", "read:sessions", ""),
    RouteRule("POST", "/agent/interrupt", "write:chat", ""),
    RouteRule("POST", "/agent/steer", "write:chat", ""),
    # workspace-native tool registry view.
    RouteRule("GET", "/agent/tools", "read:runtime", ""),

    # charts
    RouteRule("GET", "/charts/get", "read:runtime", "fetch chart bulk artifact by id"),
    RouteRule(
        "POST",
        "/charts/publish",
        "write:tools",
        "persist a dynamic-code chart_block; returns chart_id + bulk_data_uri",
    ),

    # skills
    RouteRule("GET", "/skills", "read:runtime", ""),
    RouteRule("GET", "/skills/detail", "read:runtime", ""),
    RouteRule("GET", "/skills/installed", "read:runtime", ""),
    RouteRule("POST", "/skills/call", "write:tools", ""),
    RouteRule("POST", "/skills/create", "write:skills", ""),
    RouteRule("POST", "/skills/update", "write:skills", ""),
    RouteRule("POST", "/skills/install", "write:skills", ""),
    RouteRule("POST", "/skills/promote", "write:skills", ""),
    RouteRule(None, "/skills/lock/status", "read:runtime", ""),
    RouteRule(None, "/skills/lock/inspect", "read:runtime", ""),
    RouteRule("POST", "/skills/lock/verify", "read:runtime", ""),
    RouteRule("POST", "/skills/lock/sign", "write:skills", ""),
    RouteRule("POST", "/skills/lock/clear_signature", "write:skills", ""),

    # browser engines / browser sessions
    RouteRule("GET", "/browsers/registry", "read:runtime", ""),
    RouteRule("GET", "/browsers/status", "read:runtime", ""),
    RouteRule("POST", "/browsers/select", "write:config", ""),
    RouteRule("POST", "/browsers/configure", "write:config", ""),
    RouteRule("POST", "/browsers/install", "admin:ops",
              "installs optional browser dependencies"),
    RouteRule("POST", "/browsers/uninstall", "admin:ops",
              "removes optional browser dependencies"),
    RouteRule("POST", "/browsers/probe", "read:runtime", ""),
    RouteRule("GET", "/browsers/session/list", "read:runtime", ""),
    RouteRule("GET", "/browsers/session/get", "read:runtime", ""),
    RouteRule(None, "/browsers/session/", "write:tools",
              "browser navigation/actions may execute page code or network requests"),

    # security / secrets
    RouteRule(None, "/security/secrets/", "write:secrets", "no reveal route"),
    RouteRule(None, "/security/web/", "read:runtime", ""),
    RouteRule(None, "/security/provider_auth/list", "read:runtime", ""),
    RouteRule(None, "/security/provider_auth/status", "read:runtime", ""),
    RouteRule("POST", "/security/provider_auth/register", "write:secrets", ""),
    RouteRule("POST", "/security/provider_auth/revoke", "write:secrets", ""),
    RouteRule("POST", "/security/provider_auth/refresh", "write:secrets", ""),
    RouteRule("POST", "/security/provider_auth/reauth", "write:secrets", ""),

    # approvals
    RouteRule(None, "/approvals/pending", "read:sessions", ""),
    RouteRule(None, "/approvals/prompt", "read:sessions", ""),
    RouteRule("POST", "/approvals/callback", "approve:tool", ""),

    # trading / portfolio / strategy
    RouteRule("POST", "/trading/submit", "trade:paper",
              "live submit still requires risk_gate + approval_gate"),
    RouteRule("POST", "/trading/cancel", "trade:paper", ""),
    RouteRule("POST", "/trading/history", "read:runtime", ""),
    RouteRule("POST", "/trading/recent_trades", "read:runtime", ""),
    RouteRule("POST", "/accounts/test_balance", "read:runtime", ""),
    RouteRule(None, "/portfolio/", "read:runtime", ""),
    RouteRule("POST", "/strategy/list_all", "read:runtime", ""),
    RouteRule("POST", "/strategy/get", "read:runtime", ""),
    RouteRule("POST", "/strategy/create", "write:config", ""),
    RouteRule("POST", "/strategy/update", "write:config", ""),
    RouteRule("POST", "/strategy/close_positions", "trade:paper",
              "live close still requires risk_gate + approval_gate"),
    RouteRule("POST", "/strategy/delete", "write:config", ""),
    RouteRule("POST", "/strategy/set_status", "write:config", ""),
    RouteRule("POST", "/strategy/bind_wallet", "write:config", ""),
    RouteRule("POST", "/strategy/bind_account", "write:config", ""),
    RouteRule("POST", "/strategy/resolve_runtime", "read:runtime", ""),
    RouteRule("POST", "/strategy/versions", "read:runtime", ""),
    RouteRule("POST", "/strategy/files_list", "read:runtime", ""),
    RouteRule("POST", "/strategy/files_write", "write:config", ""),
    RouteRule("POST", "/strategy/backtests", "read:runtime", ""),
    RouteRule("POST", "/strategy/backtests/chart", "read:runtime", ""),
    RouteRule("POST", "/strategy/backtests/file", "read:runtime", ""),
    RouteRule(None, "/strategy/", "read:runtime", ""),

    # gateway
    RouteRule("GET", "/gateway/platforms", "read:runtime", ""),
    RouteRule("GET", "/gateway/config", "read:runtime", ""),
    RouteRule("POST", "/gateway/config/upsert", "write:config", ""),
    RouteRule("POST", "/gateway/config/delete", "write:config", ""),
    RouteRule("POST", "/gateway/config/test", "gateway:send", ""),
    RouteRule("GET", "/gateway/status", "read:runtime", ""),
    RouteRule(None, "/gateway/commands", "read:runtime", ""),
    RouteRule("POST", "/gateway/inbound", "gateway:webhook",
              "actor resolved per platform"),
    RouteRule("POST", "/gateway/send", "gateway:send",
              "should usually be internal-only"),
    RouteRule("POST", "/gateway/telegram/setup", "write:config", ""),
    RouteRule("POST", "/gateway/telegram/poll", "read:runtime", ""),
    RouteRule("POST", "/gateway/telegram/send", "gateway:send", ""),

    # messages
    RouteRule("POST", "/messages/send", "write:chat", ""),
    RouteRule("POST", "/messages/list", "read:sessions", ""),

    # llm
    RouteRule("POST", "/llm/classify", "write:chat", ""),
    RouteRule("POST", "/llm/extract_json", "write:chat", ""),
    RouteRule("POST", "/llm/messages/probe", "write:chat", ""),
    RouteRule(None, "/llm/capabilities", "read:runtime", ""),
    RouteRule("GET", "/llm/providers", "read:runtime", ""),
    RouteRule("GET", "/llm/tiers", "read:runtime", ""),
    RouteRule("GET", "/llm/config", "read:runtime", ""),
    RouteRule("POST", "/llm/config", "write:config", ""),
    RouteRule("GET", "/llm/models", "read:runtime", ""),
    RouteRule("POST", "/llm/models/refresh", "write:config", ""),
    RouteRule("POST", "/llm/models/validate", "read:runtime", ""),
    RouteRule("GET", "/llm/provider_routing", "read:runtime", ""),
    RouteRule("POST", "/llm/provider_routing", "write:config", ""),

    # scripts
    RouteRule("POST", "/scripts/run", "write:tools", ""),
    RouteRule("POST", "/scripts/analyze", "read:runtime", ""),

    # evolution
    RouteRule("GET", "/evolution/signals", "read:runtime", ""),
    RouteRule("POST", "/evolution/signals", "read:runtime", ""),
    RouteRule("GET", "/evolution/events", "read:runtime", ""),
    RouteRule("POST", "/evolution/events", "read:runtime", ""),
    RouteRule("GET", "/evolution/timeline", "read:runtime", ""),
    RouteRule("POST", "/evolution/timeline", "read:runtime", ""),
    RouteRule("GET", "/evolution/assets", "read:runtime", ""),
    RouteRule("POST", "/evolution/assets", "read:runtime", ""),
    RouteRule(
        "POST",
        "/evolution/validation/run",
        "admin:ops",
        "executes validation commands in a subprocess",
    ),
    RouteRule("GET", "/evolution/auto_apply/status", "read:runtime", ""),
    RouteRule(
        "POST",
        "/evolution/auto_apply/tick",
        "admin:ops",
        "may apply and roll back proposals",
    ),
    # Proposal browsing is read-only, including concrete proposal ids.
    RouteRule("GET", "/evolution/proposals", "read:runtime", ""),
    RouteRule("POST", "/evolution/proposals", "read:runtime", ""),
    RouteRule("GET", "/evolution/proposals/{proposal_id}", "read:runtime", ""),
    RouteRule("POST", "/evolution/proposals/{proposal_id}", "read:runtime", ""),
    RouteRule("POST", "/evolution/proposals/{proposal_id}/approve", "write:skills", ""),
    RouteRule("POST", "/evolution/proposals/{proposal_id}/reject", "write:skills", ""),
    RouteRule("POST", "/evolution/post_apply_observation", "write:skills", ""),
    RouteRule("POST", "/evolution/proposals/{proposal_id}/post_apply_observation", "write:skills", ""),
    RouteRule(None, "/evolution/", "write:skills", ""),

    # market / discovery
    RouteRule(None, "/market/", "read:runtime", ""),
    RouteRule(None, "/discovery/", "read:runtime", ""),
    RouteRule(None, "/discovery", "read:runtime", ""),

    # dev
    RouteRule(None, "/dev/", "admin:ops", ""),

    # runtime / capabilities
    RouteRule(None, "/runtime/", "read:runtime", ""),
    # Runtime feature-flag endpoints.
    RouteRule("GET", "/runtime/flags", "read:runtime", ""),
    RouteRule("POST", "/runtime/flags/set", "write:config", ""),
    RouteRule("POST", "/runtime/flags/refresh", "write:config", ""),
    # Durable raw tool-result store.
    RouteRule("GET", "/runtime/tool_raw", "read:runtime", ""),
    RouteRule("GET", "/runtime/tool_raw/list", "read:runtime", ""),
    # Capability catalog.
    RouteRule(None, "/capabilities/", "read:runtime", ""),
    # Data-source sync state.
    RouteRule(None, "/data-sources", "read:runtime", ""),
    RouteRule("GET", "/data-sources/status", "read:runtime", ""),
    RouteRule("GET", "/data-sources/events", "read:runtime", ""),
    RouteRule("POST", "/data-sources/sync-now", "write:config", ""),
    # Trading evidence vault.
    RouteRule("GET", "/evidence/sources", "read:runtime", ""),
    RouteRule("GET", "/evidence/topics", "read:runtime", ""),
    RouteRule("GET", "/evidence/search", "read:runtime", ""),
    RouteRule("GET", "/evidence/get", "read:runtime", ""),
    RouteRule("GET", "/evidence/topic", "read:runtime", ""),
    RouteRule("POST", "/evidence/ingest/run", "write:memory", ""),
    # Prompt-guard review queue.
    RouteRule("GET", "/security/prompt_guard/items", "read:runtime", ""),
    RouteRule("POST", "/security/prompt_guard/resolve", "approve:tool", ""),
    RouteRule("POST", "/security/prompt_guard/classify", "read:runtime", ""),
    # Operator preference profile.
    RouteRule("GET", "/memory/profile", "read:runtime", ""),
    RouteRule("POST", "/memory/profile/set", "write:memory", ""),
    RouteRule("POST", "/memory/profile/pin", "write:memory", ""),
    RouteRule("POST", "/memory/profile/forget", "write:memory", ""),
    RouteRule("POST", "/memory/profile/rebuild", "write:memory", ""),
    RouteRule("POST", "/memory/forget", "write:memory", ""),
    # Memory backend installer + tester (Selected backend settings UX)
    RouteRule("POST", "/memory/external/install/run", "admin:ops", ""),
    RouteRule("POST", "/memory/test", "read:runtime", ""),
    # E2E artifact capture.
    RouteRule("GET", "/ops/e2e/runs", "read:runtime", ""),
    RouteRule("GET", "/ops/e2e/run", "read:runtime", ""),
    RouteRule("POST", "/ops/e2e/run/start", "admin:ops", ""),
    RouteRule("POST", "/ops/e2e/run/record", "admin:ops", ""),
    RouteRule("POST", "/ops/e2e/run/finalize", "admin:ops", ""),
    RouteRule("POST", "/ops/e2e/auto-capture", "admin:ops", ""),
    RouteRule("GET", "/network/proxy", "read:runtime", ""),
    RouteRule("POST", "/network/proxy", "write:config", ""),
    RouteRule("POST", "/network/proxy/test", "read:runtime", ""),
    RouteRule("GET", "/network/dashboard", "read:runtime", ""),
    RouteRule("POST", "/network/dashboard", "write:config", ""),
    RouteRule("GET", "/network/tunnels", "read:runtime", ""),
    RouteRule("POST", "/network/tunnels/config", "write:config", ""),
    RouteRule("POST", "/network/tunnels/install", "admin:ops",
              "installs optional tunnel binaries"),
    RouteRule("POST", "/network/tunnels/start", "admin:ops",
              "starts an external tunnel process"),
    RouteRule("POST", "/network/tunnels/stop", "admin:ops",
              "stops an external tunnel process"),

    # triggers
    RouteRule("POST", "/triggers/emit", "write:chat", ""),
    RouteRule("POST", "/triggers/dry_run", "read:runtime", ""),
    RouteRule("GET", "/triggers/routes", "read:runtime", ""),
    RouteRule("GET", "/triggers/result", "read:sessions", ""),
    RouteRule("GET", "/triggers/schedules", "read:runtime", ""),
    RouteRule("POST", "/triggers/schedules/tick", "read:runtime", ""),
    RouteRule("POST", "/triggers/schedules/run_now", "write:chat", ""),
    RouteRule("GET", "/triggers/schedules/status", "read:runtime", ""),
    RouteRule(None, "/triggers/schedules/", "write:config", ""),
    RouteRule(None, "/triggers/routes/", "write:config", ""),
    RouteRule("POST", "/triggers/explain", "read:sessions", ""),
    RouteRule("POST", "/triggers/replay", "write:chat", ""),
    RouteRule("GET", "/triggers/stats", "read:runtime", ""),

    # wallet
    RouteRule(None, "/wallet/providers", "read:runtime", ""),
    RouteRule(None, "/wallet/configured", "read:runtime", ""),
    RouteRule("POST", "/wallet/status", "read:runtime", ""),
    RouteRule("POST", "/wallet/install_hint", "read:runtime", ""),
    RouteRule(None, "/wallet/credential_schema", "read:runtime", ""),
    RouteRule("POST", "/wallet/install", "write:config", ""),
    RouteRule("POST", "/wallet/auth/start", "write:config", ""),
    RouteRule("POST", "/wallet/auth/verify", "write:config", ""),
    RouteRule("POST", "/wallet/auth/status", "read:runtime", ""),
    RouteRule(None, "/wallet/installed", "read:runtime", ""),
    RouteRule("POST", "/wallet/configure", "write:config", ""),
    RouteRule("POST", "/wallet/quote", "read:runtime", ""),
    RouteRule("POST", "/wallet/balance", "read:runtime", ""),
    RouteRule("POST", "/wallet/klines", "read:runtime", ""),
    RouteRule("POST", "/wallet/swap", "trade:paper",
              "wallet swap is a side-effect; live exchanges still gate at risk layer"),

    # exchanges
    RouteRule(None, "/exchanges/providers", "read:runtime", ""),
    RouteRule("POST", "/exchanges/ping", "read:runtime", ""),
)


# ---- helpers ----

def _matches(rule: RouteRule, method: str, path: str) -> bool:
    if rule.method is not None and rule.method.upper() != method.upper():
        return False
    if rule.path.endswith("/"):
        return path.startswith(rule.path)
    if "{" not in rule.path:
        return path == rule.path
    pattern = rule.path.strip("/").split("/")
    actual = path.strip("/").split("/")
    if len(pattern) != len(actual):
        return False
    return all(
        (segment.startswith("{") and segment.endswith("}"))
        or segment == value
        for segment, value in zip(pattern, actual)
    )


def _best_match(method: str, path: str) -> Optional[RouteRule]:
    """Return the rule with the longest matching path (None if no rule)."""
    best: Optional[RouteRule] = None
    best_len = -1
    for rule in _RULES:
        if not _matches(rule, method, path):
            continue
        # Prefer method-specific over method-agnostic at same length so
        # an explicit ``GET /skills/lock/inspect`` beats a generic
        # ``None /skills/lock/inspect`` when both are listed.
        path_len = len(rule.path)
        score = path_len * 2 + (1 if rule.method is not None else 0)
        if score > best_len:
            best = rule
            best_len = score
    return best


def required_scope(method: str, path: str) -> Optional[str]:
    """Return the scope name required for ``(method, path)``.

    Returns ``None`` when the route is anonymous (e.g. ``/health``) or
    has explicitly opted out of scope checking. Returns ``'admin:ops'``
    for unknown routes — the conservative default.
    """
    if path in ANONYMOUS_PATHS:
        return None
    rule = _best_match(method, path)
    if rule is None:
        return "admin:ops"  # unknown → treat as admin
    return rule.scope


def parse_scopes(raw: object) -> frozenset[str]:
    """Parse a scope grant from config into a frozenset.

    Accepts:
    - ``None`` / empty → ``frozenset()``
    - A single string ``"write:chat"`` → ``frozenset({"write:chat"})``
    - A comma/space separated string ``"read:runtime, write:chat"``
    - A list/tuple/set of strings.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset(
            str(item).strip()
            for item in raw
            if item is not None and str(item).strip()
        )
    if isinstance(raw, str):
        cleaned = raw.replace(",", " ").split()
        return frozenset(part.strip() for part in cleaned if part.strip())
    return frozenset({str(raw)})


def authorize(scopes: Iterable[str], method: str, path: str) -> tuple[bool, Optional[str]]:
    """Check whether the granted scope set satisfies the route.

    Returns ``(ok, reason)`` where ``reason`` is ``None`` on success or
    a short explanation suitable for the security-events journal.
    """
    granted = frozenset(scopes)
    if WILDCARD_SCOPE in granted:
        return True, None
    needed = required_scope(method, path)
    if needed is None:
        return True, None
    if needed in granted:
        return True, None
    return False, f"insufficient_scope:needed={needed}"


def describe_matrix() -> list[dict[str, object]]:
    """Return a JSON-serialisable view of the matrix.

    Used by the capability-matrix endpoint and the operator docs so the
    UI can show "what scope does this route require".
    """
    out: list[dict[str, object]] = []
    for rule in _RULES:
        out.append({
            "method": rule.method or "*",
            "path": rule.path,
            "scope": rule.scope,
            "note": rule.note,
        })
    return out
