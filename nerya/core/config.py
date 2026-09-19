"""Runtime config loader.

Config precedence:
  CLI flag > env var > workspace nerya.yml > built-in defaults.

Typos in ``nerya.yml`` used to disappear silently into
:meth:`Config.get` defaults. :func:`load_config` now warns on unknown
top-level keys and unknown ``agent.*`` sub-keys the operator actually
wrote (silence with ``NERYA_CONFIG_QUIET=1``) — the first step toward
a fully schema-validated config catalogue; see
``docs/extensibility-upgrade.md``.
"""

from __future__ import annotations

import logging
import os
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import yaml_io
from .paths import WorkspacePaths, resolve_workspace

log = logging.getLogger("nerya.core.config")

DEFAULT_CONFIG: dict[str, Any] = {
    # Workspace plugins (nerya.harness.loader). Same trust model as
    # hooks/ and providers/: enabled by default, opt-out here, and a
    # broken plugin is journaled to journals/plugins.jsonl — never fatal.
    "plugins": {
        "enabled": True,
        # Directory names under <workspace>/plugins/ to skip.
        "disabled": [],
    },
    "runtime": {
        "live_trading_enabled": False,
        "paper_trading_enabled": True,
        "kill_switch": False,
        # When True, data/LLM/connector fetchers are allowed to fall back to
        # deterministic mock data on upstream failure. Default is False —
        # production runtime must surface degraded envelopes instead of
        # silently fabricating results. See :mod:`nerya.core.truth`.
        "mock_mode": False,
        # Unattended authoring/testing; dangerous actions and domain gates remain.
        "permission_mode": "auto",
        "intent_gate": {"enabled": False, "fail_closed": True},
    },
    "network": {
        "proxy": {
            "enabled": False,
            "mode": "direct",
            "preset": "custom",
            "http_url": "",
            "https_url": "",
            "all_url": "",
            "pool_url": "",
            "pool_format": "auto",
            "no_proxy": "127.0.0.1,localhost,::1",
        },
        "tunnels": {
            "providers": {},
        },
    },
    "dashboard": {
        "host": "127.0.0.1",
        "port": 18380,
    },
    "llm": {
        "default_tier": "medium",
        # Classification / intent-recognition calls use this tier when a
        # caller does not request one explicitly. Operators can point it at a
        # dedicated "intent" tier from the dashboard.
        "intent_tier": "light",
        # Provider-level credentials and base URLs. Tiers may inherit these so
        # an operator adds a provider key once, imports models, then assigns
        # provider+model pairs without pasting the same secret into every tier.
        "providers": {},
        # Request-context logging mode for LLM calls. The default keeps normal
        # journals compact. Set to "full" or NERYA_CONTEXT_FULL_LOG=1 to append
        # redacted canonical prompt/messages/tools payloads to dev_logs.
        "context_log_mode": "summary",
        # Opt-in provider-owned web search. Local Nerya tools
        # web_search/web_fetch/web_search_fetch remain available by default.
        # Operators can set this globally, per provider profile, or per tier:
        #   provider_native_web_search: true
        #   provider_native_web_search:
        #     enabled: true
        #     max_uses: 5
        #     search_context_size: medium
        "provider_native_web_search": {"enabled": False},
        "tiers": {
            "light": {
                "provider": "mock",
                "model": "light-model",
                "max_tokens": 2048,
                "temperature": 0.1,
                "timeout_s": 30,
                "allowed_tasks": [
                    "news_filtering", "compress", "classify", "intent_classification",
                    "trigger_triage", "extract_json",
                    "auto_session_title",
                    # Cheap subagent lanes (web_researcher, sentiment_analyst,
                    # news_interpreter, …) run their reasoning loop on the
                    # light tier. Without this entry a light-tier role was
                    # silently upgraded to medium by TierPolicy.
                    "subagent_analysis",
                ],
                # Capability families the tier advertises. A tier matches a
                # task when the task normalises to any of these classes
                # (see :mod:`nerya.llm.task_classes`). Additive with
                # allowed_tasks so exact-string matches keep working.
                "allowed_classes": [
                    "classification",
                    "structured_extraction",
                    "content_compression",
                ],
            },
            "medium": {
                "provider": "mock",
                "model": "medium-model",
                "max_tokens": 8192,
                "temperature": 0.2,
                "timeout_s": 90,
                "allowed_tasks": [
                    "normal_agent_loop",
                    "subagent_analysis",
                    "strategy_review",
                    "trade_explanation",
                ],
                "allowed_classes": [
                    "agent_loop",
                    "subagent_reasoning",
                    "strategy_review",
                ],
            },
            "high": {
                "provider": "mock",
                "model": "high-model",
                "max_tokens": 32768,
                "temperature": 0.2,
                "timeout_s": 240,
                # Reasoning controls.
                # ``reasoning_effort``: minimal | low | medium | high — opt-in;
                # only honoured when the configured model is reasoning-
                # capable (gpt-5*/o1*/o3*/o4*, claude-opus-4*/sonnet-4*/3-7,
                # gemini-2.5+/3+, deepseek-r1, qwen-qwq).
                # ``reasoning_summary``: auto | concise | detailed (OpenAI
                # responses-style; ignored on adapters that don't support it).
                "reasoning_effort": "",
                "reasoning_summary": "",
                "allowed_tasks": [
                    "script_generation",
                    "skill_generation",
                    "complex_signal_analysis",
                    "large_loss_postmortem",
                    "strategy_evolution",
                ],
                "allowed_classes": [
                    "proposal_generation",
                    "complex_reasoning",
                ],
            },
        },
    },
    "trading": {
        "dedupe_window_seconds": 300,
        "max_stale_seconds": 30,
    },
    "memory": {
        "legacy_owner_actor": "default",
        "external": {
            "enabled": False,
            "provider": "",
            "agentmemory": {
                "base_url": "http://127.0.0.1:3111",
                "secret_ref": "",
                "secret_env": "AGENTMEMORY_SECRET",
                "project": "",
                "session_id": "",
                "context_budget": 2000,
                "timeout_s": 1.5,
                "install_command": "npx @agentmemory/agentmemory",
                "mcp_command": "npx -y @agentmemory/mcp",
                "viewer_url": "http://127.0.0.1:3113",
            },
        },
        "vector_search": {
            "enabled": False,
            "backend": "memsearch",
            "install_package": "memsearch",
            "watch_enabled": False,
            "paths": ["memory", "strategies"],
            "embedding": {
                "provider": "openai",
                "model": "text-embedding-3-small",
                "base_url": "",
                "api_key_ref": "",
            },
            "milvus": {
                "uri": "~/.memsearch/milvus.db",
                "token": "",
                "collection": "memsearch_chunks",
            },
        },
    },
    "workspace_preferences": {
        # Runtime-facing defaults that used to be hardcoded to
        # ``binance``/``BTCUSDT``. Operators can change these without
        # editing Python; hot paths read through ``core.market_defaults``.
        "market_defaults": {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "quote": "USDT",
            # Extra natural-language token aliases. Keys are lower-case
            # and values are upper-case symbols (with or without quote
            # suffix). Empty by default.
            "aliases": {},
            # Order matters: UI discovery / default picks follow this.
            "preferred_venues": ["binance", "bybit", "okx", "hyperliquid"],
        },
    },
    "agent": {
        "native": {
            # Match the Workspace chat budget for SDK/API callers too.
            "max_iterations": 120,
            "max_total_tool_calls": 400,
            "max_wall_seconds": 1800.0,
            "max_tokens": 16384,
            "wall_time_final_synthesis_seconds": 30.0,
            "action_tool_wall_reserve_seconds": 15.0,
            "result_overflow_threshold_bytes": 65_536,
        },
        # operator-mode preset.  Picks the coarse policy
        # for the LLM-visible action catalog: ``read_only`` /
        # ``dev`` (default) / ``deploy`` / ``live_trading``.  Workspaces
        # can override and add per-action allow/deny globs.
        "operator": {
            "preset": "dev",
            "extra_allow_actions": [],
            "extra_deny_actions": [],
        },
        "intent_defaults": {
            "account_id": "paper_main",
            "size": 100.0,
            "size_unit": "usd",
            "side": "buy",
            "order_type": "market",
            # ``market`` is intentionally left unset here so the
            # runtime falls back to ``workspace_preferences.market_defaults``
            # via ``default_market_id``. Operators that want to pin a
            # specific market can still override it per-workspace.
            "confidence": 0.6,
            "source": "agent",
        },
        "planner": {
            # Route presets are declarative YAML resources under
            # ``nerya/agent/route_manifest_presets`` or workspace-level
            # ``route_manifests``. Keep Python config as a selector only.
            "manifest": "trading-v1",
            "routes": {},
            "fallback": "generic",
        },
    },
    "approvals": {
        "expire_seconds": 600,
    },
    # Trigger router caps and policies. Operators can override per-route limits
    # in workspace nerya.yml under ``triggers.router.policies`` (keyed by
    # source/channel/actor/event-kind). compatibility audit.
    "triggers": {
        "router": {
            # Hard payload cap when no route declares ``max_payload_bytes``.
            "default_max_payload_bytes": 65_536,
            # Optional per-source / per-channel overrides:
            # policies:
            #   by_source:
            #     telegram: { max_payload_bytes: 16384 }
            #   by_channel:
            #     "chat:#ops": { max_payload_bytes: 32768 }
            #   by_kind:
            #     "news.breaking": { max_payload_bytes: 131072 }
            "policies": {
                "by_source": {},
                "by_channel": {},
                "by_kind": {},
                "by_actor": {},
            },
        },
    },
    "wallet": {
        # Which on-chain wallet provider to use. Leave null / unset to
        # disable all on-chain execution. Supported values:
        #   "self_custody"    — goat-sdk / eth_account / solders (local keys)
        #   "okx_os"          — OKX On-Chain OS (DEX aggregator REST API)
        #   "bitget"          — bitget-wallet-skill (Node subprocess)
        #   "binance_agentic" — binance-web3/binance-agentic-wallet (Node)
        #   "coinbase"        — Coinbase CDP (cdp-sdk python or node skill)
        # Dependencies for each provider are *not* auto-installed.
        "provider": None,
        "self_custody": {
            "signer_ref": "",
            "chains": ["ethereum", "bsc", "arbitrum", "polygon", "base", "solana"],
            "rpc_urls": {},
        },
        "okx_os": {
            "api_key_ref": "",
            "api_secret_ref": "",
            "api_passphrase_ref": "",
            "api_project_id": "",
        },
        "bitget": {
            "skill_path": "",
            "entry": "dist/nerya.js",
        },
        "binance_agentic": {
            "skill_path": "",
            "entry": "dist/index.js",
        },
        "coinbase": {
            "api_key_name_ref": "",
            "api_private_key_ref": "",
            "network_id": "base-mainnet",
            "skill_path": "",
            "entry": "dist/index.js",
        },
    },
    "api": {
        "host": "127.0.0.1",
        "port": 7878,
    },
    # Promotion gate for the research promotion workflow.
    # consumes validation reports + shadow runs (where present) to
    # decide whether ``paper -> canary -> live`` promotions can land.
    # Defaults are intentionally safe: validation is required to
    # promote out of paper, shadow is required to promote into live.
    "research": {
        "validation_enabled": True,
        "validation_required_for_canary": True,
        "shadow_required_for_live": True,
        "allow_warn_promotion": False,
        "allow_fixture_data": True,
        "default_initial_capital_usd": 10000.0,
        "gates": {
            "min_bars": 20,
            "min_trades": 1,
            "max_drawdown_pct": 30.0,
            "min_sharpe": 0.0,
            "cost_stress_multiplier": 2.0,
        },
    },
    # Optional third-party integrations. Every block below is OFF by
    # default; turning one on is the *only* way its code path gets
    # imported, its routes get mounted, or its skill becomes visible to
    # the agent.
    #
    # ``NERYA_DISABLE_INTEGRATIONS=1`` short-circuits every block back
    # to the default (disabled) regardless of what the workspace yaml
    # says.
    "integrations": {},
    # manifest-driven MCP tool surface. The legacy
    # ``NeryaTools`` registry stays on by default; the dynamic layer
    # generates a tool per manifest action that passes the policy
    # below. Set ``mcp.dynamic_tools.enabled: false`` to revert to the
    # legacy-only surface.
    "mcp": {
        # Server exposure is separate from the local tools CLI. Never auto-start.
        "enabled": False,
        "auth_mode": "oauth2",
        "public_url": "",  # blank inherits an active Nerya public tunnel
        "openai_tunnel": {"enabled": False, "tunnel_id": "", "api_key_ref": ""},
        "transport": "stdio",
        "host": "127.0.0.1",
        "port": 8765,
        "token_env": "NERYA_MCP_TOKEN",
        "allowed_hosts": [],
        "allowed_origins": [],
        "allow_mutating": False,  # legacy strategy generation / trigger emission
        "allow_tools": None,  # public names, applied after every layer's safety policy
        "deny_tools": [],
        "include_legacy": True,
        "native_tools": {
            "enabled": True,
            "mode": "default",
            "allow_mutating": False,
            "allow_exec": False,
            # Management only. Enabling writes must not also expose shell,
            # trade execution, approval or live-promotion endpoints.
            "allow_tools": [
                "role_list", "role_get", "role_save", "role_delete", "subagent_list",
                "strategy_list", "strategy_view", "strategy_history",
                "strategy_draft_proposal", "strategy_submit_proposal", "strategy_validate",
                "strategy_run_history", "strategy_tuning_generate",
                "strategy_tuning_status", "strategy_tuning_snapshot",
                "evolve_core_config_patch", "evolve_proposals",
                "workspace_ui_inspect", "workspace_ui_propose",
            ],
            "deny_tools": [],
        },
        "dynamic_tools": {
            "enabled": True,
            # Defaults to the workspace's active operator preset (see
            # ``agent.operator.preset``). Set to a specific value to
            # decouple MCP exposure from the planner preset.
            "preset": None,
            # Read-only by default — only actions whose name pattern
            # looks like a query (see
            # :func:`nerya.skills.manifest.action_is_read_only`) are
            # exposed. Set ``allow_mutating: true`` to expose
            # risk-gated mutating actions (they still go through the
            # runtime's risk / approval / availability gates).
            "allow_mutating": False,
            "include_unimplemented": False,
            # Skill / action allow-deny lists. Allow-lists are nullable
            # ("None" = no restriction); deny-lists are simple sequences
            # of ``"skill_id"`` or ``"skill_id.action"`` strings.
            "allow_skills": None,
            "deny_skills": [],
            "allow_actions": None,
            "deny_actions": [],
        },
    },
}


@dataclass
class Config:
    paths: WorkspacePaths
    data: dict[str, Any] = field(default_factory=dict)

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def live_trading_enabled(self) -> bool:
        return bool(self.get("runtime.live_trading_enabled", False))

    def paper_trading_enabled(self) -> bool:
        return bool(self.get("runtime.paper_trading_enabled", True))

    def kill_switch(self) -> bool:
        if os.environ.get("NERYA_KILL_SWITCH", "").lower() in ("1", "true", "yes"):
            return True
        return bool(self.get("runtime.kill_switch", False))

    def integration_enabled(self, name: str) -> bool:
        """True iff ``integrations.<name>.enabled`` is set AND the
        ``NERYA_DISABLE_INTEGRATIONS`` kill-switch is not active.

        Every optional third-party integration
        must be gated by this helper so a single env flag can disable
        all of them during CI, audits, or incident response.
        """
        if os.environ.get("NERYA_DISABLE_INTEGRATIONS", "").lower() in (
            "1", "true", "yes", "on",
        ):
            return False
        return bool(self.get(f"integrations.{name}.enabled", False))


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def _warn_unknown_keys(user: dict[str, Any]) -> None:
    """Warn on operator-written keys the runtime would silently ignore.

    Only keys present in the *user* config are checked (not the merged
    tree), so defaults drifting from ``DEFAULT_CONFIG`` never spam the
    log. Coverage is deliberately narrow for this first pass — top
    level plus ``agent.*`` — because those are where a typo costs the
    most (a misspelled top-level section silently disables a whole
    subsystem). Deep schema validation is tracked in
    ``docs/extensibility-upgrade.md``.
    """

    if os.environ.get("NERYA_CONFIG_QUIET", "").lower() in ("1", "true", "yes"):
        return
    if not isinstance(user, dict) or not user:
        return
    try:
        for key in sorted(user):
            if key not in DEFAULT_CONFIG:
                log.warning(
                    "nerya.yml: unknown top-level key %r (typo? this key is "
                    "ignored; set NERYA_CONFIG_QUIET=1 to silence)",
                    key,
                )
        agent_user = user.get("agent")
        agent_known = DEFAULT_CONFIG.get("agent", {})
        if isinstance(agent_user, dict) and isinstance(agent_known, dict):
            for key in sorted(agent_user):
                if key not in agent_known:
                    log.warning(
                        "nerya.yml: unknown agent.%s key (typo? this key is "
                        "ignored; set NERYA_CONFIG_QUIET=1 to silence)",
                        key,
                    )
    except Exception:
        # Config warnings must never break config loading.
        log.debug("unknown-key check failed", exc_info=True)


def load_config(
    workspace: Path | str | None = None,
    *,
    profile: str | None = None,
) -> Config:
    """Load the merged config for ``workspace`` (or the active profile).

    compatibility audit added an explicit ``profile``
    selector so callers can dispatch ``nerya --profile dev`` style
    commands without mutating the global env.
    """
    paths = resolve_workspace(workspace, profile=profile)
    user = yaml_io.load(paths.config, default={}) or {}
    merged = _merge(DEFAULT_CONFIG, user)
    _warn_unknown_keys(user)
    # env overrides
    if os.environ.get("NERYA_LIVE_TRADING", "").lower() in ("true", "1", "yes"):
        merged.setdefault("runtime", {})["live_trading_enabled"] = True

    config = Config(paths=paths, data=merged)

    # Workspace proxy settings are process-wide because urllib, requests,
    # httpx, ccxt, subprocess tools, and skill scripts all honor the standard
    # proxy environment variables. Keep failures non-fatal so a stale pool
    # endpoint never prevents the API from booting.
    try:
        from .proxy import apply_network_proxy
        apply_network_proxy(config)
    except Exception:
        pass

    # Dev mode toggle — activate the recorder so every downstream HTTP / tool
    # call is captured. Env var wins over yaml so operators can flip it on
    # for a single process without editing config.
    _activate_dev_mode(merged, paths)
    return config


def _activate_dev_mode(merged: dict[str, Any], paths: WorkspacePaths) -> None:
    want = bool((merged.get("runtime") or {}).get("dev_mode"))
    if os.environ.get("NERYA_DEV_MODE", "").lower() in ("1", "true", "yes", "on"):
        want = True
    if not want:
        return
    try:
        from . import devmode  # avoid import cycle when configs are loaded early
        devmode.enable(True)
        devmode.get_recorder(paths)  # pre-warm so the dir exists for ops
    except Exception:
        pass
