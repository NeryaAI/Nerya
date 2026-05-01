"""On-chain wallet HTTP endpoints.

These endpoints back the `Settings → On-chain Wallet` pane on the
dashboard and the `nerya wallet …` CLI. They *never* install any
optional dependency — requests against an unconfigured provider return
the exact commands the operator should run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import wallet as wallet_mod
from ..core import yaml_io
from ..data.onchain_klines import fetch_token_klines
from ..install.dep_installer import (
    DependencyInstallError,
    install as run_install,
    is_auto_install_allowed,
    list_node_skills as list_installed_node_skills,
    uninstall_node_skill as remove_node_skill,
)
from ..wallet.errors import (
    WalletDependencyError,
    WalletPolicyDenied,
    WalletProviderNotFound,
)


def _workspace(client) -> Path:
    return Path(client.config.paths.root)


def _wallet_cfg(client, name: str | None = None) -> dict[str, Any]:
    cfg = (client.config.data.get("wallet") or {})
    if name:
        return dict((cfg.get(name) or {}))
    return dict(cfg)


def _wallet_yaml_set(client, patch: dict[str, Any]) -> None:
    """Persist a wallet.* patch into workspace nerya.yml."""
    conf_path = client.config.paths.config
    existing = yaml_io.load(conf_path, default={}) or {}
    wallet = existing.get("wallet") or {}
    for k, v in patch.items():
        if v is None:
            wallet.pop(k, None)
        elif isinstance(v, dict) and isinstance(wallet.get(k), dict):
            wallet[k].update(v)
        else:
            wallet[k] = v
    existing["wallet"] = wallet
    yaml_io.dump(conf_path, existing)
    client.config.data.setdefault("wallet", {}).update(wallet)


def routes():
    def list_all(client, _payload):
        return {
            "providers": wallet_mod.readiness_report(
                client.config.data, workspace=_workspace(client),
            ),
            "active": ((client.config.data.get("wallet") or {}).get("provider") or "") or None,
        }

    def list_configured(client, _payload):
        """04-29 §11 P8 — every wallet binding declared in
        ``wallet.providers.<id>`` plus the legacy single
        ``wallet.provider`` selection. Used by the dashboard's
        Add-account form so operators can pick a wallet binding by id.
        """

        bindings = wallet_mod.list_configured_providers(client.config.data)
        return {"bindings": bindings, "count": len(bindings)}

    def status(client, payload):
        name = (payload.get("provider") or "").strip().lower() \
            or (client.config.data.get("wallet") or {}).get("provider")
        if not name:
            return {"provider": None, "ready": False,
                    "reason": "no wallet provider selected"}
        try:
            p = wallet_mod.build_provider(
                name, _wallet_cfg(client, name),
                workspace=_workspace(client),
            )
        except WalletProviderNotFound as exc:
            return {"provider": name, "ready": False, "reason": str(exc)}
        r = p.readiness().to_dict()
        r["active"] = (name == (client.config.data.get("wallet") or {}).get("provider"))
        try:
            caps = p.capabilities()
        except Exception:
            caps = None
        r["capabilities"] = caps.to_dict() if caps else None
        r["stability"] = (
            caps.execution_profile if caps is not None else "experimental"
        )
        return r

    def install_hint(_client, payload):
        name = (payload.get("provider") or "").strip().lower()
        entry = wallet_mod.PROVIDERS.get(name)
        if not entry:
            return {"error": "unknown_provider",
                    "known": sorted(wallet_mod.PROVIDERS.keys())}
        try:
            p = wallet_mod.build_provider(name, {})
            caps = p.capabilities()
            caps_dict = caps.to_dict()
            stability = caps.execution_profile
        except Exception:
            caps_dict = None
            stability = "experimental"
        return {
            "provider": name,
            "install_hint": entry.get("install_hint", ""),
            "links": entry.get("links", {}),
            "runtime": entry.get("runtime", "python"),
            "capabilities": caps_dict,
            "stability": stability,
        }

    def configure(client, payload):
        name = (payload.get("provider") or "").strip().lower() or None
        if name and name not in wallet_mod.PROVIDERS:
            return {"ok": False, "error": "unknown_provider",
                    "known": sorted(wallet_mod.PROVIDERS.keys())}
        patch: dict[str, Any] = {"provider": name}
        provider_cfg = payload.get("config")
        if name and isinstance(provider_cfg, dict):
            patch[name] = dict(provider_cfg)
        _wallet_yaml_set(client, patch)
        return {"ok": True, "provider": name,
                "config": _wallet_cfg(client, name) if name else {}}

    def quote(client, payload):
        name = (payload.get("provider") or "").strip().lower() \
            or (client.config.data.get("wallet") or {}).get("provider")
        if not name:
            return {"ok": False, "error": "no_provider_selected"}
        try:
            p = wallet_mod.build_provider(
                name, _wallet_cfg(client, name), workspace=_workspace(client),
            )
            result = p.quote(
                chain=str(payload.get("chain") or "ethereum"),
                token_in=str(payload.get("token_in") or ""),
                token_out=str(payload.get("token_out") or ""),
                amount_in=float(payload.get("amount_in") or 0.0),
                slippage_bps=int(payload.get("slippage_bps") or 50),
                **{k: v for k, v in payload.items()
                   if k not in {"provider", "chain", "token_in", "token_out",
                                 "amount_in", "slippage_bps"}},
            )
            return {"ok": True, "quote": result.to_dict()}
        except WalletDependencyError as exc:
            return {"ok": False, "error": "dependency_missing",
                    "provider": exc.provider, "missing": exc.missing,
                    "install_hint": exc.install_hint}
        except WalletPolicyDenied as exc:
            return {"ok": False, "error": "policy_denied", "reason": str(exc)}
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "error": "provider_error", "reason": str(exc)}

    def swap(client, payload):
        name = (payload.get("provider") or "").strip().lower() \
            or (client.config.data.get("wallet") or {}).get("provider")
        if not name:
            return {"ok": False, "error": "no_provider_selected"}
        if not client.config.live_trading_enabled():
            return {"ok": False, "error": "live_trading_disabled",
                    "reason": "enable runtime.live_trading_enabled to run swaps"}
        try:
            p = wallet_mod.build_provider(
                name, _wallet_cfg(client, name), workspace=_workspace(client),
            )
            result = p.swap(
                chain=str(payload.get("chain") or "ethereum"),
                token_in=str(payload.get("token_in") or ""),
                token_out=str(payload.get("token_out") or ""),
                amount_in=float(payload.get("amount_in") or 0.0),
                slippage_bps=int(payload.get("slippage_bps") or 50),
                receiver=payload.get("receiver") or None,
                live=True,
            )
            return {"ok": result.ok, "result": result.to_dict()}
        except WalletDependencyError as exc:
            return {"ok": False, "error": "dependency_missing",
                    "provider": exc.provider, "missing": exc.missing,
                    "install_hint": exc.install_hint}
        except WalletPolicyDenied as exc:
            return {"ok": False, "error": "policy_denied", "reason": str(exc)}
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "error": "provider_error", "reason": str(exc)}

    def balance(client, payload):
        name = (payload.get("provider") or "").strip().lower() \
            or (client.config.data.get("wallet") or {}).get("provider")
        if not name:
            return {"ok": False, "error": "no_provider_selected"}
        try:
            p = wallet_mod.build_provider(
                name, _wallet_cfg(client, name), workspace=_workspace(client),
            )
            result = p.get_balance(
                chain=str(payload.get("chain") or "ethereum"),
                address=str(payload.get("address") or ""),
                token=str(payload.get("token") or ""),
            )
            return {"ok": True, "balance": result.to_dict()}
        except WalletDependencyError as exc:
            return {"ok": False, "error": "dependency_missing",
                    "provider": exc.provider, "missing": exc.missing,
                    "install_hint": exc.install_hint}
        except WalletPolicyDenied as exc:
            return {"ok": False, "error": "policy_denied", "reason": str(exc)}
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "error": "provider_error", "reason": str(exc)}

    def klines(_client, payload):
        chain = str(payload.get("chain") or "")
        token = str(payload.get("token") or "")
        interval = str(payload.get("interval") or "1h")
        limit = int(payload.get("limit") or 100)
        if not chain or not token:
            return {"ok": False, "error": "chain_and_token_required"}
        candles = fetch_token_klines(chain, token, interval=interval, limit=limit)
        return {"ok": True, "chain": chain, "token": token,
                "interval": interval, "count": len(candles),
                "candles": candles, "source": "geckoterminal"}

    def credential_schema(_client, payload):
        name = str((payload or {}).get("provider") or "").strip().lower()
        if not name:
            return {"ok": False, "error": "provider_required"}
        entry = wallet_mod.PROVIDERS.get(name)
        if entry is None:
            return {"ok": False, "error": "unknown_provider",
                    "known": sorted(wallet_mod.PROVIDERS.keys())}
        return {
            "ok": True,
            "provider": name,
            "label": entry.get("label", name),
            "runtime": entry.get("runtime", "python"),
            "install_command": entry.get("install_command", ""),
            "install_alternatives": list(entry.get("install_alternatives") or []),
            "install_hint": entry.get("install_hint", ""),
            "credential_fields": list(entry.get("credential_fields") or []),
        }

    def install_endpoint(client, payload):
        body = payload or {}
        name = str(body.get("provider") or "").strip().lower()
        approve = bool(body.get("approve", False))
        if not name:
            return {"ok": False, "error": "provider_required"}
        entry = wallet_mod.PROVIDERS.get(name)
        if entry is None:
            return {"ok": False, "error": "unknown_provider"}
        # Operator/agent may override the catalog default install command
        # to pick a specific alternative (e.g. npm vs git-clone). The
        # override is still routed through dep_installer's allowlist so
        # only structured forms (pip / node-skill / npm) make it through.
        override_cmd = str(body.get("command") or "").strip()
        cmd = override_cmd or str(entry.get("install_command") or "").strip()
        if override_cmd:
            alts = entry.get("install_alternatives") or []
            allowed_cmds = {str(entry.get("install_command") or "").strip()}
            for alt in alts:
                if isinstance(alt, dict):
                    allowed_cmds.add(str(alt.get("command") or "").strip())
                elif isinstance(alt, str):
                    allowed_cmds.add(alt.strip())
            allowed_cmds.discard("")
            if cmd not in allowed_cmds:
                return {
                    "ok": False, "error": "install_command_not_in_catalog",
                    "provider": name, "requested": cmd,
                    "allowed": sorted(allowed_cmds),
                }
        if not cmd:
            return {"ok": True, "skipped": True, "reason": "no_install_needed",
                    "provider": name}
        allowed, reason = is_auto_install_allowed(
            client.config.data, approve=approve,
        )
        if not allowed:
            return {
                "ok": False, "error": "install_not_allowed", "reason": reason,
                "install_command": cmd,
                "install_hint": entry.get("install_hint", ""),
                "provider": name,
            }
        try:
            result = run_install(
                client.config.paths, cmd,
                config_data=client.config.data, approve=approve,
            )
        except DependencyInstallError as exc:
            return {"ok": False, "error": "install_refused", "detail": str(exc),
                    "install_command": cmd, "provider": name}
        # Surface the install_path for node/npm installs so the
        # operator/agent can pin it via /wallet/configure without a
        # second round-trip.
        configure_patch: dict[str, Any] = {}
        if result.kind in ("node-skill", "npm") and result.install_path:
            configure_patch = {
                name: {
                    "skill_path": result.install_path,
                    "entry": result.extra.get("entry") or "dist/nerya.js",
                },
            }
        return {
            "ok": result.ok, "provider": name, "result": result.asdict(),
            "configure_patch": configure_patch,
        }

    def installed_endpoint(client, _payload):
        rows = list_installed_node_skills(client.config.paths)
        return {"ok": True, "skills": rows, "count": len(rows)}

    def uninstall_endpoint(client, payload):
        name = str((payload or {}).get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name_required"}
        try:
            res = remove_node_skill(client.config.paths, name)
        except DependencyInstallError as exc:
            return {"ok": False, "error": "uninstall_refused", "detail": str(exc)}
        return res

    def portfolio(client, payload):
        """Aggregate balances across every configured wallet account.

        04-29 §11 P10 — once a wallet provider is installed
        and an account binds to it via ``wallet_id`` plus
        ``provider_config.balances``, the snapshot loop knows how to
        pull the live data. This route reuses that path so the
        dashboard / agent can fetch the wallet portfolio in a single
        call instead of looping through each address.
        """
        body = payload or {}
        only_account = str(body.get("account_id") or "").strip()
        try:
            from ..trading.accounts import load_account_profiles
            from ..trading.account_snapshots import (
                fresh_snapshot, latest_snapshot,
            )

            profiles = load_account_profiles(client.config.paths)
        except Exception as exc:  # pragma: no cover — defensive
            return {"ok": False, "error": "load_failed", "detail": str(exc)}
        out: list[dict[str, Any]] = []
        for profile in profiles.values():
            if not profile.wallet_id:
                continue
            if profile.kind not in ("chain", "dex"):
                continue
            if only_account and profile.id != only_account:
                continue
            snap = None
            try:
                snap = fresh_snapshot(client.config, profile.id, profile=profile)
            except Exception:
                snap = latest_snapshot(client.config.paths, profile.id)
            if snap is None:
                continue
            out.append({
                "account_id": profile.id,
                "wallet_id": profile.wallet_id,
                "venue": profile.venue,
                "mode": profile.mode,
                "ts": snap.ts,
                "source": snap.source,
                "health": snap.health,
                "nav_usd": snap.nav_usd,
                "free_by_asset": snap.free_by_asset,
                "cash_by_asset": snap.cash_by_asset,
                "meta": snap.meta,
            })
        if only_account and not out:
            return {"ok": False, "error": "no_wallet_account",
                    "detail": f"account {only_account!r} has no wallet binding"}
        return {"ok": True, "accounts": out, "count": len(out)}

    return [
        ("GET", "/wallet/providers", list_all),
        ("POST", "/wallet/providers", list_all),
        ("GET", "/wallet/configured", list_configured),
        ("POST", "/wallet/configured", list_configured),
        ("POST", "/wallet/status", status),
        ("POST", "/wallet/install_hint", install_hint),
        ("POST", "/wallet/credential_schema", credential_schema),
        ("GET", "/wallet/credential_schema", credential_schema),
        ("POST", "/wallet/install", install_endpoint),
        ("GET", "/wallet/installed", installed_endpoint),
        ("POST", "/wallet/installed", installed_endpoint),
        ("POST", "/wallet/uninstall", uninstall_endpoint),
        ("POST", "/wallet/configure", configure),
        ("POST", "/wallet/quote", quote),
        ("POST", "/wallet/swap", swap),
        ("POST", "/wallet/balance", balance),
        ("POST", "/wallet/klines", klines),
        ("POST", "/wallet/portfolio", portfolio),
        ("GET", "/wallet/portfolio", portfolio),
    ]
