"""On-chain wallet HTTP endpoints.

These endpoints back the `Settings → On-chain Wallet` pane on the
dashboard and the `nerya wallet …` CLI. They *never* install any
optional dependency — requests against an unconfigured provider return
the exact commands the operator should run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
from typing import Any

from .. import wallet as wallet_mod
from ..core import yaml_io
from ..core.errors import TradingError
from ..data.onchain_klines import fetch_token_klines
from ..security.runtime_env import build_process_env
from ..security.secrets import SecretVault
from ..install.dep_installer import (
    DependencyInstallError,
    install as run_install,
    is_auto_install_allowed,
    list_node_skills as list_installed_node_skills,
    uninstall_node_skill as remove_node_skill,
)
from ..trading import accounts as accounts_mod
from ..trading.access_control import trusted_http_actor
from ..wallet.swap_approval import prepare_swap, request_approval as request_swap_approval
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
        out = dict((cfg.get(name) or {}))
        if _meaningful_wallet_cfg(out):
            return out
        for binding in wallet_mod.list_configured_providers(client.config.data):
            if binding.get("provider") == name:
                return dict(binding.get("config") or {})
        return out
    return dict(cfg)


def _meaningful_wallet_cfg(cfg: dict[str, Any]) -> bool:
    for key, value in (cfg or {}).items():
        if value in (None, "", [], {}):
            continue
        if key == "entry" and value in {
            "dist/nerya.js",
            "dist/index.js",
            "scripts/bitget-wallet-agent-api.py",
        }:
            continue
        return True
    return False


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


_VALID_ACCOUNT_MODES = ("paper", "shadow", "canary", "live")


def _sanitize_account_id(value: str) -> str:
    out = []
    for ch in value.lower():
        if ch.isalnum() or ch in ("_", "-"):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out).strip("_-") or "wallet"


def _next_available_account_id(client, candidate: str) -> str:
    try:
        existing = accounts_mod.load_accounts(client.config.paths)
    except Exception:
        existing = {}
    if candidate not in existing:
        return candidate
    idx = 2
    while True:
        next_id = f"{candidate}_{idx}"
        if next_id not in existing:
            return next_id
        idx += 1
        if idx > 99:
            raise TradingError("could not allocate a free wallet account id")


def _maybe_create_wallet_account(
    client,
    *,
    provider: str | None,
    wallet_id: str,
    label: str,
    config: dict[str, Any] | None,
    operator: str,
    auto_create: bool,
    account_mode: str | None,
    account_id_hint: str | None,
    initial_balance_usd: float | None,
    balances: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Create/refresh a chain account row that mirrors a wallet binding.

    The dashboard's "configure wallet" path used to require two
    round-trips: first ``/wallet/configure`` for ``wallet.providers.<id>``,
    then ``/accounts/upsert`` with ``kind=chain`` and ``wallet_id=<id>``
    so the snapshot loop could pick the wallet up. We now do both in
    one call when ``auto_create_account=true`` is passed.

    ``account_mode`` defaults to ``live`` because wallets touch real
    funds; the dashboard surfaces a picker so the operator can opt
    into ``shadow`` (read-only audit) or ``paper`` (sandbox) explicitly.
    The created row keeps ``permissions.place_order=false`` until the
    operator promotes it from the account driver page — same guard
    used elsewhere in P8.
    """

    if not auto_create:
        return None
    if not wallet_id:
        return {"ok": False, "error": "wallet_id_required_for_auto_account"}
    if not provider:
        return {"ok": False, "error": "provider_required_for_auto_account"}
    mode = (account_mode or "live").strip().lower()
    if mode not in _VALID_ACCOUNT_MODES:
        return {"ok": False, "error": "invalid_account_mode",
                "allowed": list(_VALID_ACCOUNT_MODES)}
    # CEX-style "live" gating: real-money modes require the operator
    # to also flip live_trading_enabled on the account from the
    # driver page. We mark the wallet as read-balances by default and
    # never opt into ``place_order`` here.
    is_real_money = mode in ("live", "canary")
    existing_profiles = accounts_mod.load_account_profiles(client.config.paths)
    existing_wallet_profile = next(
        (
            profile
            for profile in existing_profiles.values()
            if profile.wallet_id == wallet_id and profile.kind in ("chain", "dex")
        ),
        None,
    )
    aid_hint = (account_id_hint or "").strip().lower()
    if not aid_hint:
        aid_hint = _sanitize_account_id(f"{provider}_{wallet_id}")
    aid_hint = _sanitize_account_id(aid_hint)
    if existing_wallet_profile is not None:
        aid = existing_wallet_profile.id
    elif aid_hint in existing_profiles:
        aid = aid_hint
    else:
        try:
            aid = _next_available_account_id(client, aid_hint)
        except TradingError as exc:
            return {"ok": False, "error": "account_id_allocation_failed",
                    "detail": str(exc)}
    created = aid not in existing_profiles

    provider_cfg: dict[str, Any] = {}
    if isinstance(config, dict):
        for k, v in config.items():
            if isinstance(v, (str, int, float, bool, dict, list)):
                provider_cfg[str(k)] = v
    if isinstance(balances, list) and balances:
        provider_cfg["balances"] = [
            dict(row) for row in balances if isinstance(row, dict)
        ]

    payload = {
        "id": aid,
        "venue": provider,
        "kind": "chain",
        "mode": mode,
        "status": "active",
        "base_currency": "USDT",
        "live_trading_enabled": False,
        "initial_balance_usd": float(initial_balance_usd or 0.0),
        "permissions": {
            "read_balances": True,
            "place_order": False,
            "cancel_order": False,
            "withdraw": False,
        },
        "wallet_id": wallet_id,
        "provider_spec": provider,
        "provider_config": provider_cfg,
        "label": label or wallet_id,
        "last_modified_by": operator,
    }
    # Wallet accounts authenticate through the wallet provider binding
    # saved in ``nerya.yml``; account rows do not duplicate those
    # secrets. ``accounts.upsert_account`` explicitly allows
    # live/canary chain/dex rows with a wallet_id and empty
    # credentials.
    if is_real_money:
        payload["credentials"] = {}
    try:
        profile = accounts_mod.upsert_account(
            client.config.paths, payload, operator=operator,
        )
    except TradingError as exc:
        # Vault requirement tripped — drop back to paper so the
        # operator still gets the row and can promote it later.
        msg = str(exc).lower()
        if is_real_money and "credentials" in msg:
            payload["mode"] = "paper"
            payload["live_trading_enabled"] = False
            payload.pop("credentials", None)
            try:
                profile = accounts_mod.upsert_account(
                    client.config.paths, payload, operator=operator,
                )
            except TradingError as inner:
                return {"ok": False, "error": "upsert_failed",
                        "detail": str(inner), "requested_mode": mode}
            return {
                "ok": True,
                "account_id": profile.id,
                "wallet_id": wallet_id,
                "mode": profile.mode,
                "created": created,
                "demoted_from": mode,
                "demote_reason": "live_account_requires_vault_credentials",
            }
        return {"ok": False, "error": "upsert_failed", "detail": str(exc)}
    return {
        "ok": True,
        "account_id": profile.id,
        "wallet_id": wallet_id,
        "mode": profile.mode,
        "created": created,
    }


def _configure_wallet_binding(
    client,
    *,
    provider: str | None,
    wallet_id: str,
    label: str,
    config: dict[str, Any] | None,
    activate: bool,
    operator: str,
    replace_existing: bool = False,
    auto_create_account: bool = False,
    account_mode: str | None = None,
    account_id_hint: str | None = None,
    initial_balance_usd: float | None = None,
    balances: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    name = (provider or "").strip().lower() or None
    if name and name not in wallet_mod.PROVIDERS:
        return {
            "ok": False,
            "error": "unknown_provider",
            "known": sorted(wallet_mod.PROVIDERS.keys()),
        }
    provider_cfg = config if isinstance(config, dict) else {}
    patch: dict[str, Any] = {}
    stored: list[dict[str, Any]] = []
    clean_cfg: dict[str, Any] = {}
    if name and isinstance(provider_cfg, dict):
        clean_cfg, stored = _vaultify_wallet_config(
            client,
            provider=name,
            wallet_id=wallet_id or name,
            config=dict(provider_cfg),
            operator=operator,
        )
    if wallet_id:
        if not name:
            return {"ok": False, "error": "provider_required"}
        providers = dict((client.config.data.get("wallet") or {}).get("providers") or {})
        existing = providers.get(wallet_id)
        existing_provider = ""
        if isinstance(existing, dict):
            existing_provider = str(existing.get("provider") or "").strip().lower()
        if existing_provider and existing_provider != name and not replace_existing:
            return {
                "ok": False,
                "error": "wallet_id_provider_conflict",
                "wallet_id": wallet_id,
                "provider": name,
                "existing_provider": existing_provider,
            }
        providers[wallet_id] = {
            "provider": name,
            "label": label or wallet_id,
            "config": clean_cfg,
        }
        patch["providers"] = providers
        if activate:
            patch["provider"] = name
            patch[name] = clean_cfg
    else:
        patch["provider"] = name
        if name and isinstance(provider_cfg, dict):
            patch[name] = clean_cfg
    _wallet_yaml_set(client, patch)
    account_result: dict[str, Any] | None = None
    if auto_create_account and name and wallet_id:
        account_result = _maybe_create_wallet_account(
            client,
            provider=name,
            wallet_id=wallet_id,
            label=label,
            config=clean_cfg or {},
            operator=operator,
            auto_create=True,
            account_mode=account_mode,
            account_id_hint=account_id_hint,
            initial_balance_usd=initial_balance_usd,
            balances=balances,
        )
    return {
        "ok": True,
        "provider": name,
        "wallet_id": wallet_id or None,
        "label": label or None,
        "stored_refs": stored,
        "config": clean_cfg if name else {},
        "bindings": wallet_mod.list_configured_providers(client.config.data),
        "account": account_result,
    }


def _safe_secret_part(value: str) -> str:
    out = []
    for ch in str(value or "").lower():
        if ch.isalnum() or ch in ("_", "-", "."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out).strip("._-") or "default"


def _wallet_schema(provider: str) -> list[dict[str, Any]]:
    entry = wallet_mod.PROVIDERS.get(provider) or {}
    fields = list(entry.get("credential_fields") or [])
    fields.extend(list(entry.get("advanced_credential_fields") or []))
    return [dict(f) for f in fields]


def _vaultify_wallet_config(
    client,
    *,
    provider: str,
    wallet_id: str,
    config: dict[str, Any],
    operator: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Store plaintext wallet credentials and return config with vault refs.

    Wallet providers accept ``<field>_ref`` keys for sensitive fields.
    Public fields (project ids, skill paths, RPC URLs) stay in the config
    block so the dashboard can keep rendering them.
    """

    schema = {f.get("name"): f for f in _wallet_schema(provider)}
    out: dict[str, Any] = {}
    stored: list[dict[str, Any]] = []
    vault: SecretVault | None = None
    for key, raw in (config or {}).items():
        field = str(key).strip()
        if not field:
            continue
        value = raw
        spec = schema.get(field)
        sensitive = bool(spec.get("sensitive", True)) if spec else field.endswith(("_key", "_secret", "_passphrase", "_token"))
        if value in (None, ""):
            continue
        if isinstance(value, (dict, list)):
            out[field] = value
            continue
        text = str(value).strip()
        if not text:
            continue
        if sensitive:
            ref_key = field if field.endswith("_ref") else f"{field}_ref"
            if text.startswith("vault://"):
                out[ref_key] = text
                continue
            if vault is None:
                vault = SecretVault.open(client.config.paths.vault_enc)
            secret_name = f"wallet_{_safe_secret_part(wallet_id)}_{_safe_secret_part(field)}"
            meta = vault.put(
                name=secret_name,
                value=text,
                kind="wallet_credential",
                scope=["wallet", provider, wallet_id],
                owner=operator,
            )
            out[ref_key] = f"vault://{meta.name}"
            stored.append(meta.as_public())
        else:
            out[field] = text
    return out, stored


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SENSITIVE_OUTPUT_KEYS = (
    "access_token",
    "accesstoken",
    "refresh_token",
    "refreshtoken",
    "private",
    "secret",
    "passphrase",
    "authorization",
    "cookie",
    "sessioncert",
    "sessionsignature",
    "agentsessionid",
)


def _redact_cli_value(value: Any, *, key: str = "") -> Any:
    """Remove secrets from third-party CLI output before returning it."""

    key_l = key.lower().replace("_", "")
    if isinstance(value, dict):
        return {k: _redact_cli_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_cli_value(v) for v in value]
    if any(marker in key_l for marker in _SENSITIVE_OUTPUT_KEYS):
        return "***"
    return value


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _extract_json_output(text: str) -> Any | None:
    """Return the first valid JSON document found in CLI output."""

    clean = _strip_ansi(text).strip()
    if not clean:
        return None
    for idx, ch in enumerate(clean):
        if ch not in "{[":
            continue
        try:
            return json.loads(clean[idx:])
        except json.JSONDecodeError:
            continue
    return None


def _npm_safe_name(package: str) -> str:
    return package.replace("@", "").replace("/", "__")


def _resolve_wallet_cli(client, cli: dict[str, Any]) -> tuple[list[str] | None, str]:
    """Resolve an installed wallet CLI command without invoking a shell."""

    kind = str(cli.get("kind") or "").strip()
    if kind == "npm":
        pkg = str(cli.get("package") or "").strip()
        bin_name = str(cli.get("bin") or "").strip() or pkg.split("/")[-1]
        if pkg:
            root = client.config.paths.root / "skills" / "_node" / _npm_safe_name(pkg)
            candidates = [
                root / "node_modules" / ".bin" / f"{bin_name}.cmd",
                root / "node_modules" / ".bin" / bin_name,
            ]
            for candidate in candidates:
                if candidate.exists():
                    return [str(candidate)], str(root)
            pkg_root = root / "node_modules" / pkg
            pkg_json = pkg_root / "package.json"
            if pkg_json.exists():
                try:
                    doc = json.loads(pkg_json.read_text(encoding="utf-8"))
                    bin_field = doc.get("bin")
                    if isinstance(bin_field, dict):
                        rel = bin_field.get(bin_name) or next(iter(bin_field.values()), "")
                    else:
                        rel = bin_field or ""
                    if rel:
                        entry = pkg_root / str(rel)
                        if entry.exists():
                            return ["node", str(entry)], str(root)
                except Exception:
                    pass
        resolved = shutil.which(bin_name)
        if resolved:
            return [resolved], ""
        return None, str(cli.get("install_command") or "")
    if kind == "binary":
        binary = str(cli.get("binary") or "").strip()
        if binary:
            suffix = ".exe" if os.name == "nt" else ""
            local = client.config.paths.root / "skills" / "_bin" / binary / f"{binary}{suffix}"
            if local.exists():
                return [str(local)], str(local.parent)
            resolved = shutil.which(binary)
            if resolved:
                return [resolved], ""
        return None, str(cli.get("install_command") or "")
    return None, str(cli.get("install_command") or "")


def _safe_install_name(value: str) -> str:
    return Path(value.rstrip("/")).name.removesuffix(".git") or "package"


def _install_command_state(client, command: str) -> dict[str, Any]:
    cmd = str(command or "").strip()
    if not cmd:
        return {"installed": True, "kind": "noop", "install_command": ""}
    root = client.config.paths.root
    state: dict[str, Any] = {
        "installed": False,
        "kind": "unknown",
        "install_command": cmd,
    }
    if cmd.startswith("npm:"):
        spec = cmd[len("npm:"):].split("#", 1)[0].strip()
        safe = _npm_safe_name(spec)
        install_dir = root / "skills" / "_node" / safe
        pkg_root = install_dir / "node_modules" / spec
        state.update({
            "kind": "npm",
            "target": spec,
            "install_path": str(pkg_root if pkg_root.exists() else install_dir),
            "installed": install_dir.exists() and (pkg_root.exists() or (install_dir / "package.json").exists()),
        })
        return state
    if cmd.startswith("git-repo:") or cmd.startswith("node-skill:"):
        kind, _, spec = cmd.partition(":")
        repo = spec.split("#", 1)[0].strip()
        install_dir = root / "skills" / "_node" / _safe_install_name(repo)
        state.update({
            "kind": kind,
            "target": repo,
            "install_path": str(install_dir),
            "installed": install_dir.exists(),
        })
        return state
    if cmd.startswith("github-release-bin:"):
        spec = cmd[len("github-release-bin:"):]
        repo = spec.split("#", 1)[0].strip()
        binary = _safe_install_name(repo)
        if "#" in spec:
            frag = spec.split("#", 1)[1]
            for chunk in frag.split("&"):
                if chunk.startswith("binary="):
                    binary = chunk.split("=", 1)[1].strip() or binary
        suffix = ".exe" if os.name == "nt" else ""
        install_path = root / "skills" / "_bin" / binary / f"{binary}{suffix}"
        state.update({
            "kind": "github-release-bin",
            "target": repo,
            "install_path": str(install_path),
            "installed": install_path.exists(),
        })
        return state
    return state


def _provider_install_state(client, provider: str) -> dict[str, Any]:
    entry = wallet_mod.PROVIDERS.get(provider) or {}
    cli = dict(entry.get("auth_cli") or {})
    cmd_prefix: list[str] | None = None
    cli_root = ""
    if cli:
        cmd_prefix, cli_root = _resolve_wallet_cli(client, cli)
    install_command = str(cli.get("install_command") or entry.get("install_command") or "").strip()
    state = _install_command_state(client, install_command)
    if cli:
        state.update({
            "kind": cli.get("kind") or state.get("kind"),
            "binary": cli.get("binary"),
            "package": cli.get("package"),
            "bin": cli.get("bin"),
            "installed": bool(cmd_prefix) or bool(state.get("installed")),
            "command": cmd_prefix or [],
        })
        if cli_root and cmd_prefix:
            state["install_path"] = cli_root
    return state


def _local_proxy_env_if_available() -> dict[str, str]:
    try:
        with socket.create_connection(("127.0.0.1", 7897), timeout=0.25):
            pass
    except OSError:
        return {}
    proxy = "http://127.0.0.1:7897"
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "ALL_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "all_proxy": proxy,
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }


def _wallet_cli_env(client) -> dict[str, str]:
    env = build_process_env(os.environ.copy(), client.config.paths)
    if not any(env.get(k) for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")):
        env.update(_local_proxy_env_if_available())
    proxy = (
        env.get("HTTPS_PROXY")
        or env.get("HTTP_PROXY")
        or env.get("ALL_PROXY")
        or env.get("https_proxy")
        or env.get("http_proxy")
        or env.get("all_proxy")
    )
    if proxy:
        env.setdefault("AWAL_ELECTRON_PROXY_SERVER", proxy)
        env.setdefault("GLOBAL_AGENT_HTTP_PROXY", proxy)
        env.setdefault("GLOBAL_AGENT_HTTPS_PROXY", proxy)
    no_proxy = env.get("NO_PROXY") or env.get("no_proxy")
    if no_proxy:
        env.setdefault("GLOBAL_AGENT_NO_PROXY", no_proxy)
    return env


def _patch_coinbase_awal_windows(client) -> None:
    if os.name != "nt":
        return
    root = client.config.paths.root / "skills" / "_node" / "awal" / "node_modules" / "awal"
    target = root / "dist" / "utils" / "serverManager.js"
    if target.exists():
        text = target.read_text(encoding="utf-8")
        patched = text
        if "const electronArgs = proxyServer" not in patched:
            patched = patched.replace(
                "const child = spawn(electronBin, [bundleElectron], {",
                "const proxyServer = process.env.AWAL_ELECTRON_PROXY_SERVER || "
                "process.env.HTTPS_PROXY || process.env.HTTP_PROXY || "
                "process.env.ALL_PROXY || process.env.https_proxy || "
                "process.env.http_proxy || process.env.all_proxy;\n"
                "    const electronArgs = proxyServer\n"
                "        ? [`--proxy-server=${proxyServer}`, bundleElectron]\n"
                "        : [bundleElectron];\n"
                "    const child = spawn(electronBin, electronArgs, {",
                1,
            )
        patched = patched.replace(
            ", '--proxy-bypass-list=<-loopback>'",
            "",
        )
        needle = "stdio: 'ignore',\n        env: {"
        if (
            needle in patched
            and "windowsHide: true" not in patched
        ):
            patched = patched.replace(
                needle,
                "stdio: 'ignore',\n"
                "        shell: process.platform === 'win32',\n"
                "        windowsHide: true,\n"
                "        env: {",
                1,
            )
        if patched != text:
            target.write_text(patched, encoding="utf-8")
    bundle_candidates = [root / "server-bundle" / "bundle-electron.js"]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        bundle_candidates.append(
            Path(local_appdata) / "awal-nodejs" / "Data" / "server" / "bundle-electron.js"
        )
    for bundle in bundle_candidates:
        if not bundle.exists():
            continue
        text = bundle.read_text(encoding="utf-8")
        patched = text.replace("new sm({show:!1,width:500", "new sm({show:!0,width:500", 1)
        if patched != text:
            bundle.write_text(patched, encoding="utf-8")


def _coinbase_awal_lock_path() -> Path:
    system_drive = os.environ.get("SystemDrive") or "C:"
    return Path(system_drive + "\\tmp\\payments-mcp-ui.lock")


def _coinbase_awal_session_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Electron"
    return Path.home() / "AppData" / "Roaming" / "Electron"


def _process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _coinbase_awal_window_state() -> dict[str, Any]:
    lock_file = _coinbase_awal_lock_path()
    state: dict[str, Any] = {
        "running": False,
        "pid": None,
        "lock_file": str(lock_file),
    }
    if not lock_file.exists():
        return state
    raw = lock_file.read_text(encoding="utf-8", errors="replace").strip()
    try:
        pid = int(raw)
    except ValueError:
        state["raw"] = raw[:80]
        return state
    state["pid"] = pid
    state["running"] = _process_running(pid)
    return state


def _wait_coinbase_awal_window(timeout_s: float = 25.0) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.5, timeout_s)
    state = _coinbase_awal_window_state()
    while not state.get("running") and time.monotonic() < deadline:
        time.sleep(0.5)
        state = _coinbase_awal_window_state()
    return state


def _coinbase_popup_started_result(result: dict[str, Any]) -> dict[str, Any] | None:
    if result.get("ok") or result.get("error") != "auth_cli_timeout":
        return None
    state = _coinbase_awal_window_state()
    if not state.get("running"):
        return None
    return {
        "ok": True,
        "provider": "coinbase",
        "return_code": None,
        "json": {
            "walletWindow": state,
            "loginMethod": "email_popup",
        },
        "note": "coinbase_wallet_popup_started",
    }


def _start_wallet_cli_background(
    client,
    provider: str,
    args: list[str],
    *,
    timeout_s: float = 25.0,
) -> dict[str, Any]:
    entry = wallet_mod.PROVIDERS.get(provider) or {}
    cli = dict(entry.get("auth_cli") or {})
    if not cli:
        return {"ok": False, "error": "auth_cli_unavailable", "provider": provider}
    cmd_prefix, cli_root = _resolve_wallet_cli(client, cli)
    if not cmd_prefix:
        return {
            "ok": False,
            "error": "dependency_missing",
            "provider": provider,
            "install_command": cli.get("install_command") or entry.get("install_command", ""),
        }
    if provider == "coinbase":
        _patch_coinbase_awal_windows(client)
    env = _wallet_cli_env(client)
    path_parts: list[str] = []
    if cli_root:
        root = Path(cli_root)
        path_parts.extend([
            str(root / "node_modules" / ".bin"),
            str(root),
        ])
    env["PATH"] = os.pathsep.join([p for p in path_parts if p] + [env.get("PATH", "")])
    cmd = [*cmd_prefix, *args]
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(client.config.paths.root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "error": "dependency_missing",
            "provider": provider,
            "detail": str(exc),
        }
    state = _wait_coinbase_awal_window(timeout_s=timeout_s) if provider == "coinbase" else {}
    if provider == "coinbase" and state.get("running"):
        return {
            "ok": True,
            "provider": provider,
            "return_code": None,
            "json": {
                "walletWindow": state,
                "loginMethod": "email_popup",
                "cliPid": proc.pid,
            },
            "note": "coinbase_wallet_popup_started",
        }
    return {
        "ok": False,
        "provider": provider,
        "error": "wallet_popup_not_running",
        "json": {"cliPid": proc.pid, "walletWindow": state},
    }


def _coinbase_status_result() -> dict[str, Any]:
    state = _coinbase_awal_window_state()
    session_path = _coinbase_awal_session_path()
    running = bool(state.get("running"))
    return {
        "ok": running,
        "provider": "coinbase",
        "return_code": None,
        "json": {
            "walletWindow": state,
            "agenticSessionPath": str(session_path),
            "agenticSessionExists": session_path.exists(),
            "loginMethod": "email_popup",
        },
        "error": "" if running else "wallet_popup_not_running",
    }


def _enrich_wallet_provider_rows(client, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        state = _provider_install_state(client, str(item.get("id") or ""))
        item["auth_install_state"] = state
        item["installed"] = bool(item.get("installed") or state.get("installed"))
        readiness = dict(item.get("readiness") or {})
        readiness["installed"] = bool(readiness.get("installed") or state.get("installed"))
        item["readiness"] = readiness
        enriched.append(item)
    return enriched


def _run_wallet_cli(
    client,
    provider: str,
    args: list[str],
    *,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    entry = wallet_mod.PROVIDERS.get(provider) or {}
    cli = dict(entry.get("auth_cli") or {})
    if not cli:
        return {
            "ok": False,
            "error": "auth_cli_unavailable",
            "provider": provider,
        }
    cmd_prefix, cli_root = _resolve_wallet_cli(client, cli)
    if not cmd_prefix:
        return {
            "ok": False,
            "error": "dependency_missing",
            "provider": provider,
            "install_command": cli.get("install_command") or entry.get("install_command", ""),
        }
    if provider == "coinbase":
        _patch_coinbase_awal_windows(client)
    env = _wallet_cli_env(client)
    path_parts: list[str] = []
    if cli_root:
        root = Path(cli_root)
        path_parts.extend([
            str(root / "node_modules" / ".bin"),
            str(root),
        ])
    env["PATH"] = os.pathsep.join([p for p in path_parts if p] + [env.get("PATH", "")])
    cmd = [*cmd_prefix, *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(client.config.paths.root),
            env=env,
            capture_output=True,
            timeout=timeout_s,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "auth_cli_timeout", "provider": provider}
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "error": "dependency_missing",
            "provider": provider,
            "detail": str(exc),
        }
    stdout = _strip_ansi(proc.stdout or "")
    stderr = _strip_ansi(proc.stderr or "")
    parsed = _extract_json_output(stdout)
    return {
        "ok": proc.returncode == 0,
        "provider": provider,
        "return_code": proc.returncode,
        "json": _redact_cli_value(parsed) if parsed is not None else None,
        "stdout_tail": stdout[-1500:] if proc.returncode != 0 and not parsed else "",
        "stderr_tail": stderr[-1500:] if proc.returncode != 0 else "",
    }


def _install_for_auth(client, provider: str, *, approve: bool) -> dict[str, Any] | None:
    entry = wallet_mod.PROVIDERS.get(provider) or {}
    cli = dict(entry.get("auth_cli") or {})
    cmd = str(cli.get("install_command") or entry.get("install_command") or "").strip()
    if not cmd:
        return None
    state = _provider_install_state(client, provider)
    if state.get("installed"):
        return {
            "ok": True,
            "kind": "already-installed",
            "target": state.get("target") or provider,
            "command": "",
            "duration_s": 0.0,
            "install_path": state.get("install_path") or "",
            "skipped": True,
            "skip_reason": "auth_dependency_already_installed",
            "extra": {"install_state": state},
        }
    result = run_install(client.config.paths, cmd, config_data=client.config.data, approve=approve)
    if provider == "coinbase" and result.ok:
        _patch_coinbase_awal_windows(client)
    return result.asdict()


def _auth_start_args(provider: str, payload: dict[str, Any]) -> tuple[list[str] | None, str, list[str]]:
    email = str(payload.get("email") or "").strip()
    locale = str(payload.get("locale") or "en-US").strip() or "en-US"
    if provider == "okx_os":
        if not email:
            return None, "email_required", ["email"]
        return ["wallet", "login", email, "--locale", locale], "otp", ["otp"]
    if provider == "coinbase":
        if not email:
            return None, "email_required", ["email"]
        return ["auth", "login", email, "--json"], "otp", ["otp"]
    if provider == "binance_agentic":
        return ["auth", "signin", "--json"], "qr_approval", ["qrCodeId"]
    if provider in {"bitget", "self_custody", "byreal"}:
        return None, "no_login_required", []
    return None, "auth_cli_unavailable", []


def _auth_verify_args(provider: str, payload: dict[str, Any]) -> tuple[list[str] | None, str]:
    otp = str(payload.get("otp") or payload.get("code") or "").strip()
    if provider == "okx_os":
        if not otp:
            return None, "otp_required"
        return ["wallet", "verify", otp], ""
    if provider == "coinbase":
        if not otp:
            return None, "otp_required"
        return ["auth", "verify", otp, "--json"], ""
    if provider == "binance_agentic":
        qid = str(payload.get("qrCodeId") or payload.get("qr_code_id") or "").strip()
        if not qid:
            return None, "qr_code_id_required"
        return ["auth", "verify", "--qrCodeId", qid, "--json"], ""
    return None, "auth_cli_unavailable"


def _auth_status_args(provider: str) -> list[str] | None:
    if provider == "okx_os":
        return ["wallet", "status"]
    if provider == "coinbase":
        return ["status", "--json"]
    if provider == "binance_agentic":
        return ["wallet", "status", "--json"]
    if provider == "byreal":
        return ["wallet", "address"]
    return None


def _cli_json_data(result: dict[str, Any] | None) -> dict[str, Any]:
    doc = (result or {}).get("json")
    if not isinstance(doc, dict):
        return {}
    data = doc.get("data")
    if isinstance(data, dict):
        return data
    return doc


def _first_auth_string(*records: dict[str, Any], keys: list[str]) -> str:
    for record in records:
        for key in keys:
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _ensure_okx_wallet_account(client, verify_result: dict[str, Any]) -> dict[str, Any]:
    verify_data = _cli_json_data(verify_result)
    account_id = _first_auth_string(
        verify_data,
        keys=["accountId", "account_id", "currentAccountId"],
    )
    account_name = _first_auth_string(
        verify_data,
        keys=["accountName", "account_name", "currentAccountName"],
    )
    status = _run_wallet_cli(client, "okx_os", ["wallet", "status"], timeout_s=120.0)
    status_data = _cli_json_data(status)
    if status.get("ok"):
        account_id = _first_auth_string(
            status_data,
            verify_data,
            keys=["currentAccountId", "accountId", "account_id"],
        ) or account_id
        account_name = _first_auth_string(
            status_data,
            verify_data,
            keys=["currentAccountName", "accountName", "account_name"],
        ) or account_name
        logged_in = bool(status_data.get("loggedIn")) or bool(account_id)
        try:
            account_count = int(status_data.get("accountCount") or 0)
        except (TypeError, ValueError):
            account_count = 0
        if logged_in and not account_id and account_count == 0:
            add = _run_wallet_cli(client, "okx_os", ["wallet", "add"], timeout_s=180.0)
            add_data = _cli_json_data(add)
            if not add.get("ok"):
                return {
                    "ok": False,
                    "provider": "okx_os",
                    "error": add.get("error") or "account_create_failed",
                    "status": status,
                    "add": add,
                }
            account_id = _first_auth_string(
                add_data,
                keys=["accountId", "account_id", "currentAccountId"],
            )
            account_name = _first_auth_string(
                add_data,
                keys=["accountName", "account_name", "currentAccountName"],
            )
            status = _run_wallet_cli(client, "okx_os", ["wallet", "status"], timeout_s=120.0)
            status_data = _cli_json_data(status)
            account_id = _first_auth_string(
                status_data,
                add_data,
                keys=["currentAccountId", "accountId", "account_id"],
            ) or account_id
            account_name = _first_auth_string(
                status_data,
                add_data,
                keys=["currentAccountName", "accountName", "account_name"],
            ) or account_name
            return {
                "ok": bool(account_id),
                "provider": "okx_os",
                "created": True,
                "account_id": account_id,
                "account_name": account_name,
                "status": status,
                "add": add,
                "error": "" if account_id else "account_id_missing_after_create",
            }
    return {
        "ok": bool(account_id),
        "provider": "okx_os",
        "created": False,
        "account_id": account_id,
        "account_name": account_name,
        "status": status,
        "error": "" if account_id else "account_id_missing",
    }


def _ensure_provider_account(
    client,
    provider: str,
    verify_result: dict[str, Any],
) -> dict[str, Any] | None:
    if provider == "okx_os":
        return _ensure_okx_wallet_account(client, verify_result)
    return None


def _config_from_auth_payload(
    provider: str,
    payload: dict[str, Any],
    *,
    account: dict[str, Any] | None = None,
    install: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = dict(payload.get("config") or {}) if isinstance(payload.get("config"), dict) else {}
    account_id = str((account or {}).get("account_id") or "").strip()
    if provider == "okx_os" and account_id and not cfg.get("account_id"):
        cfg["account_id"] = account_id
    if provider == "bitget" and install:
        install_path = str(install.get("install_path") or "").strip()
        extra = install.get("extra") if isinstance(install.get("extra"), dict) else {}
        state = extra.get("install_state") if isinstance(extra.get("install_state"), dict) else {}
        if install_path and not cfg.get("skill_path"):
            cfg["skill_path"] = install_path
        entry = extra.get("entry") or state.get("entry")
        if not entry and state:
            cmd = str(state.get("install_command") or "")
            if "entry=" in cmd:
                entry = cmd.split("entry=", 1)[1].split("&", 1)[0]
        if entry and not cfg.get("entry"):
            cfg["entry"] = str(entry)
    if provider == "binance_agentic" and install:
        install_path = str(install.get("install_path") or "").strip()
        extra = install.get("extra") if isinstance(install.get("extra"), dict) else {}
        if install_path and not cfg.get("skill_path"):
            cfg["skill_path"] = install_path
        entry = extra.get("entry")
        if entry and not cfg.get("entry"):
            cfg["entry"] = str(entry)
    if provider == "byreal":
        if install:
            install_path = str(install.get("install_path") or "").strip()
            if install_path and not cfg.get("install_path"):
                cfg["install_path"] = install_path
            extra = install.get("extra") if isinstance(install.get("extra"), dict) else {}
            cli_path = str(extra.get("cli_path") or "").strip()
            if cli_path and not cfg.get("cli_path"):
                cfg["cli_path"] = cli_path
            version = extra.get("version")
            if version and not cfg.get("cli_version"):
                cfg["cli_version"] = str(version)
    if provider == "coinbase":
        if not cfg.get("agentic_session_path"):
            cfg["agentic_session_path"] = str(_coinbase_awal_session_path())
        email = str(payload.get("email") or "").strip()
        if email and not cfg.get("email"):
            cfg["email"] = email
    return cfg


def _available_wallet_id(client, provider: str, requested: str) -> str:
    providers = dict((client.config.data.get("wallet") or {}).get("providers") or {})
    candidate = requested or f"{provider}_main"
    existing = providers.get(candidate)
    if not isinstance(existing, dict):
        return candidate
    existing_provider = str(existing.get("provider") or "").strip().lower()
    if not existing_provider or existing_provider == provider:
        return candidate
    base = f"{provider}_main"
    candidate = base
    idx = 2
    while True:
        existing = providers.get(candidate)
        if not isinstance(existing, dict):
            return candidate
        existing_provider = str(existing.get("provider") or "").strip().lower()
        if not existing_provider or existing_provider == provider:
            return candidate
        candidate = f"{base}_{idx}"
        idx += 1


def _maybe_save_auth_binding(
    client,
    provider: str,
    payload: dict[str, Any],
    *,
    account: dict[str, Any] | None = None,
    install: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    wallet_id = str(payload.get("wallet_id") or "").strip()
    create_binding = bool(payload.get("create_binding", False))
    if not wallet_id and create_binding:
        wallet_id = f"{provider}_main"
    if not wallet_id:
        return None
    requested_wallet_id = wallet_id
    wallet_id = _available_wallet_id(client, provider, wallet_id)
    cfg = _config_from_auth_payload(provider, payload, account=account, install=install)
    default_label = str((wallet_mod.PROVIDERS.get(provider) or {}).get("label") or wallet_id or provider)
    label = str(payload.get("label") or wallet_id or provider).strip()
    if wallet_id != requested_wallet_id:
        label = default_label
    balances = payload.get("balances")
    if not isinstance(balances, list):
        balances = None
    initial_balance = payload.get("initial_balance_usd")
    try:
        initial_balance_value = (
            float(initial_balance)
            if initial_balance not in (None, "")
            else None
        )
    except (TypeError, ValueError):
        initial_balance_value = None
    return _configure_wallet_binding(
        client,
        provider=provider,
        wallet_id=wallet_id,
        label=label,
        config=cfg,
        activate=bool(payload.get("activate", False)),
        operator=str(payload.get("operator") or "dashboard"),
        auto_create_account=bool(payload.get("auto_create_account", False)),
        account_mode=str(payload.get("account_mode") or "").strip().lower() or None,
        account_id_hint=str(payload.get("account_id") or "").strip().lower() or None,
        initial_balance_usd=initial_balance_value,
        balances=balances,
    )


def routes():
    def list_all(client, _payload):
        return {
            "providers": _enrich_wallet_provider_rows(
                client,
                wallet_mod.readiness_report(
                    client.config.data, workspace=_workspace(client),
                ),
            ),
            "active": ((client.config.data.get("wallet") or {}).get("provider") or "") or None,
        }

    def list_configured(client, _payload):
        """Every wallet binding declared in
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
        wallet_id = str(payload.get("wallet_id") or "").strip()
        label = str(payload.get("label") or wallet_id or name or "").strip()
        balances = payload.get("balances")
        if not isinstance(balances, list):
            balances = None
        initial_balance = payload.get("initial_balance_usd")
        try:
            initial_balance_value = (
                float(initial_balance)
                if initial_balance not in (None, "")
                else None
            )
        except (TypeError, ValueError):
            initial_balance_value = None
        return _configure_wallet_binding(
            client,
            provider=name,
            wallet_id=wallet_id,
            label=label,
            config=payload.get("config") if isinstance(payload.get("config"), dict) else {},
            activate=bool(payload.get("activate", False)),
            operator=str(payload.get("operator") or "dashboard"),
            replace_existing=bool(payload.get("replace_existing", False)),
            auto_create_account=bool(payload.get("auto_create_account", False)),
            account_mode=str(payload.get("account_mode") or "").strip().lower() or None,
            account_id_hint=str(payload.get("account_id") or "").strip().lower() or None,
            initial_balance_usd=initial_balance_value,
            balances=balances,
        )

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
        if not client.config.live_trading_enabled():
            return {"ok": False, "error": "live_trading_disabled",
                    "reason": "enable runtime.live_trading_enabled to run swaps"}
        if client.config.kill_switch():
            return {"ok": False, "error": "kill_switch_enabled"}
        try:
            request, quote_snapshot = prepare_swap(client.config, payload)
            actor_id = trusted_http_actor(payload) or "operator:http"
            return request_swap_approval(
                client.config,
                request=request,
                quote=quote_snapshot,
                actor_id=actor_id,
                session_id=str(payload.get("session_id") or "").strip(),
                turn_id=str(payload.get("turn_id") or "").strip(),
                tool_call_id=str(
                    payload.get("tool_call_id")
                    or payload.get("tool_use_id")
                    or ""
                ).strip(),
            )
        except ValueError as exc:
            return {"ok": False, "error": "invalid_request", "reason": str(exc)}
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

    def klines(client, payload):
        chain = str(payload.get("chain") or "")
        token = str(payload.get("token") or "")
        market = str(payload.get("market") or payload.get("symbol") or "").strip()
        interval = str(payload.get("interval") or "1h")
        limit = int(payload.get("limit") or 100)
        if not ((chain and token) or market):
            return {"ok": False, "error": "chain_token_or_market_required"}
        provider = str(payload.get("provider") or "").strip().lower()
        wallet_id = str(payload.get("wallet_id") or "").strip()
        if provider or wallet_id:
            selected = None
            for binding in wallet_mod.list_configured_providers(client.config.data):
                if wallet_id and binding.get("wallet_id") != wallet_id:
                    continue
                if provider and binding.get("provider") != provider:
                    continue
                selected = binding
                break
            if selected is not None:
                try:
                    p = wallet_mod.build_provider(
                        str(selected["provider"]),
                        dict(selected.get("config") or {}),
                        workspace=_workspace(client),
                    )
                    token_fetcher = getattr(p, "get_token_klines", None)
                    market_fetcher = getattr(p, "get_market_klines", None)
                    if chain and token and callable(token_fetcher):
                        candles = token_fetcher(
                            chain=chain, token=token, interval=interval, limit=limit,
                        )
                        source_market = f"{chain}:{token}"
                    elif market and callable(market_fetcher):
                        candles = market_fetcher(
                            market=market, interval=interval, limit=limit,
                        )
                        source_market = market
                    else:
                        candles = []
                        source_market = market or f"{chain}:{token}"
                    if candles:
                        return {
                            "ok": True,
                            "chain": chain,
                            "token": token,
                            "market": source_market,
                            "interval": interval,
                            "count": len(candles),
                            "candles": candles,
                            "source": str(selected["provider"]),
                            "wallet_id": selected.get("wallet_id"),
                        }
                except WalletDependencyError as exc:
                    return {
                        "ok": False,
                        "error": "dependency_missing",
                        "provider": exc.provider,
                        "missing": exc.missing,
                        "install_hint": exc.install_hint,
                    }
                except WalletPolicyDenied as exc:
                    return {"ok": False, "error": "policy_denied", "reason": str(exc)}
                except Exception as exc:
                    return {"ok": False, "error": "provider_error", "reason": str(exc)}
        if not chain or not token:
            return {"ok": False, "error": "chain_and_token_required_for_fallback"}
        candles = fetch_token_klines(chain, token, interval=interval, limit=limit)
        return {"ok": True, "chain": chain, "token": token,
                "interval": interval, "count": len(candles),
                "candles": candles, "source": "geckoterminal"}

    def credential_schema(client, payload):
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
            "auth_flows": list(entry.get("auth_flows") or []),
            "credential_fields": list(entry.get("credential_fields") or []),
            "advanced_credential_fields": list(entry.get("advanced_credential_fields") or []),
            "auth_install_state": _provider_install_state(client, name),
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
        if result.kind in ("node-skill", "npm", "git-repo") and result.install_path:
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

    def auth_start(client, payload):
        body = payload or {}
        name = str(body.get("provider") or "").strip().lower()
        approve = bool(body.get("approve", False))
        should_install = bool(body.get("install", True))
        if not name:
            return {"ok": False, "error": "provider_required"}
        entry = wallet_mod.PROVIDERS.get(name)
        if entry is None:
            return {"ok": False, "error": "unknown_provider"}
        install_result: dict[str, Any] | None = None
        if should_install:
            try:
                install_result = _install_for_auth(client, name, approve=approve)
            except DependencyInstallError as exc:
                return {
                    "ok": False,
                    "error": "install_refused",
                    "detail": str(exc),
                    "provider": name,
                }
            if install_result is not None and not install_result.get("ok"):
                return {
                    "ok": False,
                    "error": "install_failed",
                    "provider": name,
                    "install": install_result,
                }
        args, next_action, required_inputs = _auth_start_args(name, body)
        if args is None:
            if next_action == "no_login_required":
                binding = _maybe_save_auth_binding(
                    client,
                    name,
                    body,
                    install=install_result,
                )
                out = {
                    "ok": True,
                    "provider": name,
                    "next_action": next_action,
                    "required_inputs": required_inputs,
                    "install": install_result,
                }
                if binding is not None:
                    out["binding"] = binding
                    out["bindings"] = binding.get("bindings", [])
                return out
            return {
                "ok": False,
                "provider": name,
                "error": next_action,
                "required_inputs": required_inputs,
                "install": install_result,
            }
        if name == "coinbase":
            result = _start_wallet_cli_background(client, name, args, timeout_s=25.0)
            if result.get("ok"):
                next_action = "wallet_popup_login"
                required_inputs = ["email_link"]
        else:
            result = _run_wallet_cli(client, name, args, timeout_s=180.0)
        out = {
            "ok": bool(result.get("ok")),
            "provider": name,
            "next_action": next_action,
            "required_inputs": required_inputs,
            "auth": result,
            "install": install_result,
        }
        if (
            result.get("ok")
            and name == "binance_agentic"
            and (body.get("wallet_id") or body.get("create_binding"))
        ):
            binding = _maybe_save_auth_binding(
                client,
                name,
                body,
                install=install_result,
            )
            if binding is not None:
                out["binding"] = binding
                out["bindings"] = binding.get("bindings", [])
        if not result.get("ok"):
            out["error"] = result.get("error") or "auth_start_failed"
        return out

    def auth_verify(client, payload):
        body = payload or {}
        name = str(body.get("provider") or "").strip().lower()
        if not name:
            return {"ok": False, "error": "provider_required"}
        if name not in wallet_mod.PROVIDERS:
            return {"ok": False, "error": "unknown_provider"}
        args, error = _auth_verify_args(name, body)
        if args is None:
            return {"ok": False, "provider": name, "error": error}
        result = _run_wallet_cli(client, name, args, timeout_s=300.0)
        account = _ensure_provider_account(client, name, result) if result.get("ok") else None
        binding = None
        if result.get("ok") and (account is None or account.get("ok")):
            binding = _maybe_save_auth_binding(client, name, body, account=account)
        out = {
            "ok": bool(result.get("ok")) and (account is None or bool(account.get("ok"))),
            "provider": name,
            "auth": result,
        }
        if account is not None:
            out["account"] = account
        if binding is not None:
            out["binding"] = binding
            out["bindings"] = binding.get("bindings", [])
        if not result.get("ok"):
            out["error"] = result.get("error") or "auth_verify_failed"
        elif account is not None and not account.get("ok"):
            out["error"] = account.get("error") or "account_create_failed"
        return out

    def auth_status(client, payload):
        body = payload or {}
        name = str(body.get("provider") or "").strip().lower()
        if not name:
            return {"ok": False, "error": "provider_required"}
        if name not in wallet_mod.PROVIDERS:
            return {"ok": False, "error": "unknown_provider"}
        args = _auth_status_args(name)
        if args is None:
            return {"ok": False, "provider": name, "error": "auth_cli_unavailable"}
        if name == "coinbase":
            result = _coinbase_status_result()
        else:
            result = _run_wallet_cli(client, name, args, timeout_s=120.0)
        out = {
            "ok": bool(result.get("ok")),
            "provider": name,
            "auth": result,
        }
        if result.get("ok") and (body.get("wallet_id") or body.get("create_binding")):
            account = _ensure_provider_account(client, name, result)
            binding = _maybe_save_auth_binding(client, name, body, account=account)
            if account is not None:
                out["account"] = account
            if binding is not None:
                out["binding"] = binding
                out["bindings"] = binding.get("bindings", [])
        if not result.get("ok"):
            out["error"] = result.get("error") or "auth_status_failed"
        return out

    def installed_endpoint(client, _payload):
        rows = list_installed_node_skills(client.config.paths)
        providers = {
            provider: _provider_install_state(client, provider)
            for provider in sorted(wallet_mod.PROVIDERS.keys())
        }
        return {
            "ok": True,
            "skills": rows,
            "providers": providers,
            "count": len(rows),
        }

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

        Once a wallet provider is installed
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
        ("POST", "/wallet/auth/start", auth_start),
        ("POST", "/wallet/auth/verify", auth_verify),
        ("POST", "/wallet/auth/status", auth_status),
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
