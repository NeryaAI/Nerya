"""Byreal CLMM DEX (Solana) wallet provider.

Byreal (https://byreal.io) ships an AI-native CLI, ``@byreal-io/byreal-cli``
(bin ``byreal-cli``), for its concentrated-liquidity (CLMM) DEX on Solana.
Nerya treats it as an on-chain wallet provider: read-only commands
(``overview``, ``pools``, ``tokens``, ``pools klines``) need no wallet, while
``swap`` and CLMM ``positions`` require a local Solana keypair the operator
configures interactively via ``byreal-cli setup``.

Nerya never installs the CLI implicitly. When it is absent the provider raises
:class:`WalletDependencyError` carrying the exact install command — either a
global ``npm install -g @byreal-io/byreal-cli`` or, inside a Nerya workspace,
the ``npm:@byreal-io/byreal-cli`` install handled by
:mod:`nerya.install.dep_installer`.

Wire details:

* Every command is invoked as ``byreal-cli -o json <subcommand> ...`` and the
  CLI replies with ``{"success": bool, "meta": {...}, "data": <payload>}``;
  this provider returns / parses the ``data`` payload.
* Byreal K-lines are per-**pool**, not per-token. ``get_token_klines`` therefore
  treats the ``token`` argument as a Byreal **pool address** (market id
  ``solana:<poolAddress>``). An optional ``token_mint`` keyword selects which
  side of the pool to chart; otherwise the CLI auto-detects the base token.
* On-chain timestamps are normalised to **seconds** to match Nerya's other
  on-chain candle sources (OKX OnchainOS / GeckoTerminal fallback).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
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


_PACKAGE = "@byreal-io/byreal-cli"
_PACKAGE_SCOPE = "@byreal-io"
_PACKAGE_NAME = "byreal-cli"
_SAFE_PACKAGE = "byreal-io__byreal-cli"
_BIN = "byreal-cli"
_ENTRY = "dist/index.cjs"
_VERSION = "0.3.6"

_INSTALL_COMMAND = f"npm:{_PACKAGE}#version={_VERSION}&entry={_ENTRY}"

_SOLANA_ALIASES = {"solana", "sol", "mainnet-beta", "mainnet", ""}

# byreal-cli K-line intervals (src/core/types.ts KlineInterval).
_BYREAL_INTERVALS = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "1hr": "1h",
    "60m": "1h",
    "4h": "4h",
    "12h": "12h",
    "1d": "1d",
    "day": "1d",
    "1day": "1d",
}


_CAPABILITIES = WalletCapabilities(
    balance=WalletCapability(
        supported=True,
        status="real",
        note="byreal-cli `wallet balance` for the locally configured Solana keypair.",
    ),
    quote=WalletCapability(
        supported=True,
        status="real",
        note="byreal-cli `swap execute --dry-run` returns a signed-quote preview (price impact, est. out).",
    ),
    swap=WalletCapability(
        supported=True,
        status="partial",
        note=(
            "byreal-cli `swap execute --confirm` signs locally with the operator "
            "keypair. Nerya still requires runtime.live_trading_enabled before "
            "broadcasting."
        ),
    ),
    market_data=WalletCapability(
        supported=True,
        status="real",
        note=(
            "Solana CLMM pool OHLCV via byreal-cli `pools klines`; the token field "
            "is the Byreal pool address (market id solana:<poolAddress>)."
        ),
    ),
    execution_profile="partial",
    chains=("solana",),
    notes=(
        "Read-only pools/tokens/overview/klines need no wallet. Swaps and CLMM "
        "positions use a local keypair from `byreal-cli setup`; keys never leave "
        "the host."
    ),
)


@dataclass
class ByrealWallet(WalletProvider):
    id: str = "byreal"
    label: str = "Byreal CLMM DEX (Solana)"
    cli_path: str = ""
    workspace: str = ""
    rpc_url: str = ""
    keypair_path: str = ""
    config: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # CLI resolution
    # ------------------------------------------------------------------
    def _cli_command(self) -> list[str] | None:
        """Return the argv prefix that runs byreal-cli, or ``None``.

        Resolution order: explicit ``cli_path`` → workspace npm install
        (``skills/_node/<safe>/node_modules/.bin``) → the package entry run
        through ``node`` → ``byreal-cli`` on ``PATH``.
        """

        if self.cli_path:
            p = Path(self.cli_path)
            if p.exists():
                if p.suffix in (".cjs", ".js", ".mjs"):
                    return ["node", str(p)]
                return [str(p)]

        if self.workspace:
            root = Path(self.workspace) / "skills" / "_node" / _SAFE_PACKAGE
            bin_names = [f"{_BIN}.cmd", _BIN] if os.name == "nt" else [_BIN]
            for bin_name in bin_names:
                shim = root / "node_modules" / ".bin" / bin_name
                if shim.exists():
                    return [str(shim)]
            entry = (
                root / "node_modules" / _PACKAGE_SCOPE / _PACKAGE_NAME / "dist" / "index.cjs"
            )
            if entry.exists():
                return ["node", str(entry)]

        resolved = shutil.which(_BIN)
        if resolved:
            return [resolved]
        return None

    def _node_available(self) -> bool:
        return shutil.which("node") is not None

    def _missing(self) -> list[str]:
        missing: list[str] = []
        if not self._node_available():
            missing.append("bin:node")
        if not self._cli_command():
            missing.append(f"npm:{_PACKAGE}")
        return missing

    def _install_hint(self) -> str:
        return (
            f"Install the Byreal CLI: `npm install -g {_PACKAGE}` (exposes "
            f"`{_BIN}` on PATH), or let Nerya install it into the workspace via "
            f"`{_INSTALL_COMMAND}`. Read-only pools/tokens/overview/klines work "
            "immediately; run `byreal-cli setup` only for wallet-signed swaps/positions."
        )

    # ------------------------------------------------------------------
    # Subprocess
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_json(text: str) -> Any | None:
        cleaned = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
        decoder = json.JSONDecoder()
        for idx, ch in enumerate(cleaned):
            if ch in "{[":
                try:
                    obj, _ = decoder.raw_decode(cleaned[idx:])
                    return obj
                except json.JSONDecodeError:
                    continue
        return None

    def _run_cli(self, args: list[str], *, timeout_s: float = 45.0) -> Any:
        cmd = self._cli_command()
        if not cmd:
            raise WalletDependencyError(self.id, self._missing(), self._install_hint())
        env = os.environ.copy()
        if self.rpc_url:
            env.setdefault("BYREAL_RPC_URL", self.rpc_url)
            env.setdefault("SOLANA_RPC_URL", self.rpc_url)
        # Suppress the CLI's background auto-update / update-notice noise so JSON
        # output stays clean and deterministic for parsing.
        env.setdefault("BYREAL_DISABLE_AUTO_UPDATE", "1")
        env.setdefault("BYREAL_NO_UPDATE_NOTIFIER", "1")
        env.setdefault("NO_UPDATE_NOTIFIER", "1")
        full = [*cmd, "-o", "json", "--non-interactive", *args]
        try:
            proc = subprocess.run(
                full,
                cwd=self.workspace or None,
                env=env,
                capture_output=True,
                timeout=timeout_s,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise WalletDependencyError(
                self.id,
                ["bin:node"],
                "Install Node 18+: https://nodejs.org/",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise WalletPolicyDenied(
                f"byreal-cli {' '.join(args)} timed out after {timeout_s}s"
            ) from exc
        doc = self._extract_json(proc.stdout or "")
        if isinstance(doc, dict) and doc.get("success") is False:
            err = doc.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("code") or "unknown error"
            else:
                msg = str(err or "unknown error")
            raise WalletPolicyDenied(f"byreal-cli {' '.join(args)} failed: {msg}")
        if proc.returncode != 0 and doc is None:
            detail = (proc.stderr or proc.stdout or "").strip()[-512:]
            raise WalletPolicyDenied(
                f"byreal-cli {' '.join(args)} exited {proc.returncode}: {detail}"
            )
        if isinstance(doc, dict) and "data" in doc:
            return doc.get("data")
        return doc

    @staticmethod
    def _require_solana(chain: str) -> None:
        if str(chain or "").strip().lower() not in _SOLANA_ALIASES:
            raise WalletPolicyDenied(
                f"Byreal is a Solana-only DEX; unsupported chain {chain!r}"
            )

    # ------------------------------------------------------------------
    # Protocol surface
    # ------------------------------------------------------------------
    def readiness(self) -> WalletReadiness:
        missing = self._missing()
        ready = not missing
        return WalletReadiness(
            provider=self.id,
            ready=ready,
            missing=missing,
            install_hint="" if ready else self._install_hint(),
            reason="" if ready else "Byreal CLI (byreal-cli) is not installed.",
        )

    def capabilities(self) -> WalletCapabilities:
        return _CAPABILITIES

    # ------------------------------------------------------------------
    # On-chain data scraping helpers (read-only, no wallet required)
    # ------------------------------------------------------------------
    def overview(self) -> dict[str, Any]:
        """Global Byreal DEX statistics (TVL, volume, fees)."""

        data = self._run_cli(["overview"], timeout_s=30.0)
        return data if isinstance(data, dict) else {"raw": data}

    def list_pools(
        self,
        *,
        sort_field: str = "tvl",
        sort_type: str = "desc",
        limit: int = 20,
        category: str = "",
        **_kw: Any,
    ) -> list[dict[str, Any]]:
        """List CLMM pools (sortable by tvl/volumeUsd24h/feeUsd24h/apr24h)."""

        args = [
            "pools",
            "list",
            "--sort-field",
            str(sort_field or "tvl"),
            "--sort-type",
            str(sort_type or "desc"),
            "--page-size",
            str(max(1, min(int(limit or 20), 100))),
        ]
        if category:
            args += ["--category", str(category)]
        data = self._run_cli(args, timeout_s=30.0)
        pools = data.get("pools") if isinstance(data, dict) else data
        return [row for row in pools if isinstance(row, dict)] if isinstance(pools, list) else []

    def pool_info(self, pool: str) -> dict[str, Any]:
        """Detailed information about a single pool."""

        pool_s = str(pool or "").strip()
        if not pool_s:
            raise WalletPolicyDenied("Byreal pool info requires a pool address")
        data = self._run_cli(["pools", "info", pool_s], timeout_s=30.0)
        return data if isinstance(data, dict) else {"raw": data}

    def analyze_pool(self, pool: str, *, invest_usd: float | None = None) -> dict[str, Any]:
        """Comprehensive pool analysis (APR, risk, range recommendations)."""

        pool_s = str(pool or "").strip()
        if not pool_s:
            raise WalletPolicyDenied("Byreal pool analysis requires a pool address")
        args = ["pools", "analyze", pool_s]
        if invest_usd is not None:
            args += ["--amount", str(invest_usd)]
        data = self._run_cli(args, timeout_s=45.0)
        return data if isinstance(data, dict) else {"raw": data}

    def list_tokens(
        self,
        *,
        search: str = "",
        sort_field: str = "volumeUsd24h",
        limit: int = 20,
        **_kw: Any,
    ) -> list[dict[str, Any]]:
        """List tokens available on Byreal."""

        args = [
            "tokens",
            "list",
            "--sort-field",
            str(sort_field or "volumeUsd24h"),
            "--page-size",
            str(max(1, min(int(limit or 20), 100))),
        ]
        if search:
            args += ["--search", str(search)]
        data = self._run_cli(args, timeout_s=30.0)
        tokens = data.get("tokens") if isinstance(data, dict) else data
        return [row for row in tokens if isinstance(row, dict)] if isinstance(tokens, list) else []

    def get_token_klines(
        self,
        *,
        chain: str,
        token: str,
        interval: str = "1h",
        limit: int = 100,
        **kw: Any,
    ) -> list[dict[str, Any]]:
        """Fetch Byreal CLMM pool OHLCV.

        ``token`` is the Byreal **pool address** (market id ``solana:<pool>``).
        An optional ``token_mint`` keyword charts a specific side of the pool;
        otherwise byreal-cli auto-detects the pool's base token.
        """

        self._require_solana(chain)
        pool = str(token or "").strip()
        if not pool:
            raise WalletPolicyDenied(
                "Byreal market data requires a pool address (market id solana:<poolAddress>)"
            )
        bar = _BYREAL_INTERVALS.get(str(interval or "1h").lower(), "1h")
        args = ["pools", "klines", pool, "--interval", bar]
        token_mint = str(kw.get("token_mint") or kw.get("mint") or "").strip()
        if token_mint:
            args += ["--token", token_mint]
        data = self._run_cli(args, timeout_s=45.0)
        rows: Any = data
        if isinstance(data, dict):
            rows = data.get("klines") or data.get("candles") or data.get("list") or []
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            try:
                if isinstance(row, dict):
                    ts = row.get("timestamp") or row.get("ts") or row.get("time")
                    o = row.get("open")
                    h = row.get("high")
                    lo = row.get("low")
                    c = row.get("close")
                    v = row.get("volume") or row.get("vol")
                elif isinstance(row, (list, tuple)) and len(row) >= 6:
                    ts, o, h, lo, c, v = row[:6]
                else:
                    continue
                ts_i = int(float(ts))
                if ts_i > 1_000_000_000_000:  # ms → s (Byreal returns ms)
                    ts_i //= 1000
                out.append({
                    "ts": ts_i,
                    "open": float(o),
                    "high": float(h),
                    "low": float(lo),
                    "close": float(c),
                    "volume": float(v or 0.0),
                })
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda r_: r_["ts"])
        if limit and len(out) > int(limit):
            out = out[-int(limit):]
        return out

    # ------------------------------------------------------------------
    # Wallet-bound surface (requires `byreal-cli setup`)
    # ------------------------------------------------------------------
    def get_balance(
        self, *, chain: str, address: str, token: str, **kw: Any,
    ) -> WalletBalance:
        self._require_solana(chain)
        data = self._run_cli(["wallet", "balance"], timeout_s=30.0)
        balances = []
        if isinstance(data, dict):
            balances = data.get("balances") or data.get("tokens") or []
        token_s = str(token or "").strip().lower()
        total = 0.0
        symbol = str(token or "SOL")
        decimals = 9
        for row in balances if isinstance(balances, list) else []:
            if not isinstance(row, dict):
                continue
            mint = str(row.get("mint") or row.get("address") or "").lower()
            sym = str(row.get("symbol") or "")
            if token_s and token_s not in (mint, sym.lower()):
                continue
            try:
                total += float(row.get("uiAmount") or row.get("amount") or row.get("balance") or 0.0)
            except (TypeError, ValueError):
                continue
            if sym:
                symbol = sym
            try:
                decimals = int(row.get("decimals") or decimals)
            except (TypeError, ValueError):
                pass
            if token_s:
                break
        if not balances and isinstance(data, dict):
            try:
                total = float(data.get("totalValueUsd") or data.get("total") or 0.0)
                symbol = "USD"
                decimals = 2
            except (TypeError, ValueError):
                total = 0.0
        return WalletBalance(
            provider=self.id, chain="solana", address=address, token=token,
            balance=total, symbol=symbol, decimals=decimals,
        )

    def quote(
        self, *, chain: str, token_in: str, token_out: str,
        amount_in: float, slippage_bps: int = 50, **kw: Any,
    ) -> WalletQuote:
        self._require_solana(chain)
        data = self._run_cli(
            [
                "swap",
                "execute",
                "--input-mint",
                str(token_in),
                "--output-mint",
                str(token_out),
                "--amount",
                str(amount_in),
                "--slippage-bps",
                str(int(slippage_bps)),
                "--dry-run",
            ],
            timeout_s=45.0,
        )
        doc = data if isinstance(data, dict) else {}
        expected = float(
            doc.get("expectedOut")
            or doc.get("estimatedOut")
            or doc.get("outAmount")
            or doc.get("expected_out")
            or 0.0
        )
        min_out = float(
            doc.get("minOut")
            or doc.get("minimumOut")
            or doc.get("min_out")
            or expected * (1.0 - slippage_bps / 10_000)
        )
        impact = doc.get("priceImpactBps") or doc.get("price_impact_bps")
        if impact is None:
            pct = doc.get("priceImpactPct") or doc.get("priceImpact") or 0.0
            try:
                impact = int(round(float(pct) * 100))
            except (TypeError, ValueError):
                impact = 0
        return WalletQuote(
            provider=self.id, chain="solana",
            token_in=token_in, token_out=token_out,
            amount_in=float(amount_in),
            expected_out=expected,
            min_out=min_out,
            slippage_bps=slippage_bps,
            price_impact_bps=int(impact or 0),
            extra={"raw": doc},
        )

    def swap(
        self, *, chain: str, token_in: str, token_out: str,
        amount_in: float, slippage_bps: int = 50,
        receiver: str | None = None, live: bool = False, **kw: Any,
    ) -> WalletSwapResult:
        if not live:
            return WalletSwapResult(
                provider=self.id, chain="solana", ok=False,
                reason="live=False; Byreal swap requires runtime.live_trading_enabled",
                amount_in=float(amount_in),
            )
        self._require_solana(chain)
        data = self._run_cli(
            [
                "swap",
                "execute",
                "--input-mint",
                str(token_in),
                "--output-mint",
                str(token_out),
                "--amount",
                str(amount_in),
                "--slippage-bps",
                str(int(slippage_bps)),
                "--confirm",
            ],
            timeout_s=120.0,
        )
        doc = data if isinstance(data, dict) else {}
        return WalletSwapResult(
            provider=self.id, chain="solana",
            ok=bool(doc.get("txid") or doc.get("signature") or doc.get("ok")),
            tx_hash=str(doc.get("txid") or doc.get("signature") or doc.get("tx_hash") or ""),
            amount_in=float(amount_in),
            amount_out=float(
                doc.get("outAmount") or doc.get("amountOut") or doc.get("amount_out") or 0.0
            ),
            reason=str(doc.get("reason") or ""),
            extra={"raw": doc},
        )
