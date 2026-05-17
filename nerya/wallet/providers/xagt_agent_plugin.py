"""XAgent plugin wallet provider.

``@xagt/agent-plugin`` is an installer/auth package for XAgent and OKX
OnchainOS skills. Nerya treats it as a wallet provider for the account
surface, while routing token OHLCV through the existing OKX OnchainOS
adapter first and the public on-chain K-line fallback second.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
from typing import Any

from ..errors import WalletDependencyError, WalletPolicyDenied
from ..protocol import (
    WalletBalance,
    WalletCapabilities,
    WalletCapability,
    WalletProvider,
    WalletQuote,
    WalletReadiness,
    WalletSwapResult,
)
from .okx_os import OkxOsWallet


_PACKAGE = "@xagt/agent-plugin"
_SAFE_PACKAGE = "xagt__agent-plugin"


_CAPABILITIES = WalletCapabilities(
    balance=WalletCapability(
        supported=True,
        status="partial",
        note="Delegated to OKX OnchainOS when its local wallet/session is available.",
    ),
    quote=WalletCapability(
        supported=True,
        status="partial",
        note="Delegated to OKX OnchainOS signed quote support when configured.",
    ),
    swap=WalletCapability(
        supported=True,
        status="partial",
        note="Delegated to OKX OnchainOS unsigned swap support; Nerya still requires live-trading approval.",
    ),
    market_data=WalletCapability(
        supported=True,
        status="real",
        note="Token OHLCV via OKX OnchainOS, with Nerya's public on-chain K-line fallback if the OKX CLI is unavailable.",
    ),
    execution_profile="partial",
    chains=("ethereum", "bsc", "polygon", "arbitrum", "base", "solana"),
    notes=(
        "@xagt/agent-plugin provides XAgent login plus OKX skill setup. "
        "Nerya stores XAgent tokens in the vault and reuses the OKX market-data adapter."
    ),
)


@dataclass
class XagtAgentPluginWallet(WalletProvider):
    id: str = "xagt_agent_plugin"
    label: str = "XAgent x OKX Agent Plugin"
    plugin_path: str = ""
    workspace: str = ""
    user_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    access_expire: str = ""
    scope: str = ""
    api_base_url: str = "https://api.xerpaai.com"
    frontend_base_url: str = "https://www.xerpaai.com"
    config: dict[str, Any] = field(default_factory=dict)

    def _package_root(self) -> Path | None:
        candidates: list[Path] = []
        if self.plugin_path:
            candidates.append(Path(self.plugin_path))
        if self.workspace:
            candidates.append(Path(self.workspace) / "skills" / "_node" / _SAFE_PACKAGE)
        for candidate in candidates:
            if (candidate / "package.json").exists() and candidate.name == "agent-plugin":
                return candidate
            pkg_root = candidate / "node_modules" / "@xagt" / "agent-plugin"
            if (pkg_root / "package.json").exists():
                return pkg_root
        return None

    def _plugin_installed(self) -> bool:
        return bool(self._package_root() or shutil.which("xagt-plugin"))

    def _credentials_path(self) -> Path:
        configured = str(self.config.get("credentials_path") or "").strip()
        if configured:
            return Path(configured)
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "xagt" / "credentials.json"
        return Path.home() / ".xagt" / "credentials.json"

    def _credential_summary(self) -> dict[str, Any]:
        path = self._credentials_path()
        if not path.exists():
            return {}
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(doc, dict):
            return {}
        return {
            "user_id": doc.get("userId") or doc.get("user_id") or "",
            "access_expire": doc.get("accessExpire") or doc.get("access_expire") or "",
            "scope": doc.get("scope") or "",
            "credentials_path": str(path),
        }

    def _session_ready(self) -> bool:
        return bool(
            self.user_id
            or self.access_token
            or self.config.get("access_token_ref")
            or self.config.get("refresh_token_ref")
            or self._credential_summary().get("user_id")
        )

    def _okx(self) -> OkxOsWallet:
        okx_config = dict(self.config.get("okx") or {})
        for key in ("onchainos_bin", "binary_path", "base_url"):
            if self.config.get(key) and key not in okx_config:
                okx_config[key] = self.config[key]
        return OkxOsWallet(
            account_id=str(okx_config.get("account_id") or ""),
            api_key=str(okx_config.get("api_key") or ""),
            api_secret=str(okx_config.get("api_secret") or ""),
            api_passphrase=str(okx_config.get("api_passphrase") or ""),
            api_project_id=str(okx_config.get("api_project_id") or ""),
            base_url=str(okx_config.get("base_url") or OkxOsWallet.base_url),
            workspace=self.workspace,
            config=okx_config,
        )

    def readiness(self) -> WalletReadiness:
        missing: list[str] = []
        if not self._plugin_installed():
            missing.append("npm:@xagt/agent-plugin")
        if not self._session_ready():
            missing.append("login:xagt device approval")
        ready = not missing
        return WalletReadiness(
            provider=self.id,
            ready=ready,
            missing=missing,
            install_hint=(
                "Install @xagt/agent-plugin, open the XAgent login link, "
                "approve the user code, then verify so Nerya can vault the XAgent session."
            )
            if not ready
            else "",
            reason="" if ready else "XAgent plugin package or login session is missing.",
        )

    def capabilities(self) -> WalletCapabilities:
        return _CAPABILITIES

    def get_token_klines(
        self,
        *,
        chain: str,
        token: str,
        interval: str = "1h",
        limit: int = 100,
        **kw: Any,
    ) -> list[dict[str, Any]]:
        token_s = str(token or "").strip()
        if not token_s:
            raise WalletPolicyDenied("XAGT on-chain market data requires token")
        try:
            rows = self._okx().get_token_klines(
                chain=chain,
                token=token_s,
                interval=interval,
                limit=limit,
                **kw,
            )
            if rows:
                return rows
        except (WalletDependencyError, WalletPolicyDenied):
            pass
        from ...data.onchain_klines import fetch_token_klines

        return fetch_token_klines(chain, token_s, interval=interval, limit=limit)

    def get_balance(
        self, *, chain: str, address: str, token: str, **kw: Any,
    ) -> WalletBalance:
        return self._okx().get_balance(
            chain=chain,
            address=address,
            token=token,
            **kw,
        )

    def quote(
        self,
        *,
        chain: str,
        token_in: str,
        token_out: str,
        amount_in: float,
        slippage_bps: int = 50,
        **kw: Any,
    ) -> WalletQuote:
        return self._okx().quote(
            chain=chain,
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            slippage_bps=slippage_bps,
            **kw,
        )

    def swap(
        self,
        *,
        chain: str,
        token_in: str,
        token_out: str,
        amount_in: float,
        slippage_bps: int = 50,
        receiver: str | None = None,
        live: bool = False,
        **kw: Any,
    ) -> WalletSwapResult:
        return self._okx().swap(
            chain=chain,
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            slippage_bps=slippage_bps,
            receiver=receiver,
            live=live,
            **kw,
        )
