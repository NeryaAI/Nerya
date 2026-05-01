"""Real on-chain whale events.

Two acquisition modes, chosen automatically:

1. **Default — JSON-RPC only** (no keys, no paid services). Scans the last
   N blocks/slots of the chain's public RPC and filters by native value:

        EVM     : eth_blockNumber + eth_getBlockByNumber(block, true)
        Solana  : getSlot + getBlock(slot, {transactionDetails: "full"})

2. **Optional — Etherscan-style Scan API**. Activates only when an API key
   is passed explicitly or resolved from env (``NERYA_SCAN_KEY_<CHAIN>``).
   Lets you track specific wallets with richer tx history.

Any failure in either mode silently falls back to :func:`mock_whale_events`
so demos and tests keep running offline.

Default public RPCs ship with the module; operators can override via
``NERYA_RPC_<CHAIN>`` env vars or the chain's account config.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..connectors.http import HttpTransport, UrllibHttp
from ..core.truth import (
    degraded_envelope,
    live_envelope,
    mock_envelope,
    resolve_allow_mock,
    tag_list_envelope,
)

log = logging.getLogger(__name__)


# ============================================================ default RPCs
_EVM_RPCS: dict[str, str] = {
    "ethereum":  "https://eth.llamarpc.com",
    "eth":       "https://eth.llamarpc.com",
    "bsc":       "https://bsc-dataseed.binance.org",
    "polygon":   "https://polygon-rpc.com",
    "arbitrum":  "https://arb1.arbitrum.io/rpc",
    "base":      "https://mainnet.base.org",
    "optimism":  "https://mainnet.optimism.io",
}
_EVM_NATIVE_TOKEN: dict[str, str] = {
    "ethereum": "ETH", "eth": "ETH",
    "bsc": "BNB", "polygon": "MATIC",
    "arbitrum": "ETH", "base": "ETH", "optimism": "ETH",
}
_SOLANA_RPC = "https://api.mainnet-beta.solana.com"


def _resolve_rpc_url(chain_l: str) -> str:
    env = os.environ.get(f"NERYA_RPC_{chain_l.upper()}")
    if env:
        return env
    if chain_l == "solana":
        return _SOLANA_RPC
    return _EVM_RPCS.get(chain_l, "")


# ============================================================ optional Scan-API layer
_EVM_SCAN_APIS: dict[str, str] = {
    "ethereum":  "https://api.etherscan.io/api",
    "eth":       "https://api.etherscan.io/api",
    "bsc":       "https://api.bscscan.com/api",
    "polygon":   "https://api.polygonscan.com/api",
    "arbitrum":  "https://api.arbiscan.io/api",
    "base":      "https://api.basescan.org/api",
    "optimism":  "https://api-optimistic.etherscan.io/api",
}

# Well-known large-holder / hot-wallet seed — operator can replace via config.
_EVM_WATCH_ADDRS: dict[str, list[str]] = {
    "ethereum": [
        "0xBE0eB53F46cd790Cd13851d5EFf43D12404d33E8",  # binance 7
        "0xDFd5293D8e347dFe59E90eFd55b2956a1343963d",  # binance 8
        "0x28C6c06298d514Db089934071355E5743bf21d60",  # binance 14
    ],
}
_EVM_WATCH_ADDRS["eth"] = _EVM_WATCH_ADDRS["ethereum"]


def _resolve_scan_api_key(chain_l: str, explicit_key: str) -> str:
    if explicit_key:
        return explicit_key
    env = os.environ.get(f"NERYA_SCAN_KEY_{chain_l.upper()}")
    if env:
        return env
    return os.environ.get("NERYA_SCAN_KEY", "")


# ============================================================ RPC helper
def _rpc_call(
    http: HttpTransport,
    url: str,
    method: str,
    params: list[Any],
    *,
    timeout: float = 15.0,
) -> Any:
    status, doc = http.request(
        "POST", url,
        body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=timeout,
    )
    if status >= 400 or not isinstance(doc, dict):
        return None
    return doc.get("result")


def _int_from_hex(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16) if value.startswith("0x") else int(value)
        except ValueError:
            return 0
    return 0


# ============================================================ EVM RPC scan
def _scan_evm_rpc(
    *,
    chain_l: str,
    rpc_url: str,
    http: HttpTransport,
    min_native_amount: float,
    blocks_to_scan: int,
    limit: int,
) -> list[dict]:
    latest_hex = _rpc_call(http, rpc_url, "eth_blockNumber", [])
    latest = _int_from_hex(latest_hex)
    if latest <= 0:
        return []

    native = _EVM_NATIVE_TOKEN.get(chain_l, chain_l.upper())
    events: list[dict] = []
    for i in range(max(1, blocks_to_scan)):
        block_num = latest - i
        block = _rpc_call(
            http, rpc_url, "eth_getBlockByNumber",
            [hex(block_num), True], timeout=20.0,
        )
        if not isinstance(block, dict):
            continue
        ts = _int_from_hex(block.get("timestamp"))
        for tx in block.get("transactions") or []:
            if not isinstance(tx, dict):
                continue
            amount = _int_from_hex(tx.get("value")) / 1e18
            if amount < min_native_amount:
                continue
            events.append({
                "chain": chain_l,
                "block": block_num,
                "block_time": ts,
                "wallet": tx.get("from") or "",
                "to": tx.get("to") or "",
                "tx_hash": tx.get("hash") or "",
                "action": "transfer",
                "token": native,
                "amount": amount,
                "source": "rpc",
            })
            if len(events) >= limit:
                return events
    events.sort(key=lambda e: (e["block"], e["block_time"]), reverse=True)
    return events[:limit]


# ============================================================ EVM Scan-API (opt-in)
def _scan_evm_scanapi(
    *,
    chain_l: str,
    api_url: str,
    api_key: str,
    http: HttpTransport,
    min_native_amount: float,
    limit: int,
    watch_addresses: list[str] | None,
) -> list[dict]:
    addrs = watch_addresses or _EVM_WATCH_ADDRS.get(chain_l, [])
    native = _EVM_NATIVE_TOKEN.get(chain_l, chain_l.upper())
    events: list[dict] = []
    for addr in addrs:
        params = {
            "module": "account", "action": "txlist", "address": addr,
            "startblock": 0, "endblock": 99999999,
            "page": 1, "offset": limit, "sort": "desc",
            "apikey": api_key,
        }
        try:
            status, body = http.request("GET", api_url, params=params, timeout=15.0)
        except Exception as exc:
            log.debug("scan-api %s %s failed: %s", chain_l, addr, exc)
            continue
        if status >= 400 or not isinstance(body, dict):
            continue
        result = body.get("result")
        if not isinstance(result, list):
            continue
        for tx in result:
            try:
                amount = int(tx.get("value") or 0) / 1e18
            except (TypeError, ValueError):
                continue
            if amount < min_native_amount:
                continue
            events.append({
                "chain": chain_l,
                "block": int(tx.get("blockNumber") or 0),
                "block_time": int(tx.get("timeStamp") or 0),
                "wallet": tx.get("from") or "",
                "to": tx.get("to") or "",
                "watching": addr,
                "tx_hash": tx.get("hash") or "",
                "action": "transfer",
                "token": native,
                "amount": amount,
                "source": "scan_api",
            })
    events.sort(key=lambda e: (e["block"], e["block_time"]), reverse=True)
    return events[:limit]


# ============================================================ Solana RPC scan
def _scan_solana_rpc(
    *,
    rpc_url: str,
    http: HttpTransport,
    min_native_amount: float,
    slots_to_scan: int,
    limit: int,
) -> list[dict]:
    slot = _rpc_call(http, rpc_url, "getSlot", [])
    try:
        slot_i = int(slot)
    except (TypeError, ValueError):
        return []
    if slot_i <= 0:
        return []

    min_lamports = int(min_native_amount * 1e9)
    events: list[dict] = []
    for i in range(max(1, slots_to_scan)):
        slot_num = slot_i - i
        block = _rpc_call(
            http, rpc_url, "getBlock",
            [slot_num, {
                "transactionDetails": "full",
                "maxSupportedTransactionVersion": 0,
                "rewards": False,
            }], timeout=20.0,
        )
        if not isinstance(block, dict):
            continue
        block_time = int(block.get("blockTime") or 0)
        for tx in block.get("transactions") or []:
            if not isinstance(tx, dict):
                continue
            meta = tx.get("meta") or {}
            if meta.get("err"):
                continue
            pre = meta.get("preBalances") or []
            post = meta.get("postBalances") or []
            if not pre or not post or len(pre) != len(post):
                continue
            keys = (
                ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys")
                or []
            )
            sigs = (tx.get("transaction") or {}).get("signatures") or [""]
            for idx, (p, q) in enumerate(zip(pre, post)):
                try:
                    diff = abs(int(q) - int(p))
                except (TypeError, ValueError):
                    continue
                if diff < min_lamports:
                    continue
                wallet = keys[idx] if idx < len(keys) else ""
                if isinstance(wallet, dict):
                    wallet = wallet.get("pubkey") or ""
                events.append({
                    "chain": "solana",
                    "block": slot_num,
                    "block_time": block_time,
                    "wallet": wallet,
                    "to": "",
                    "tx_hash": sigs[0] if sigs else "",
                    "action": "balance_delta",
                    "token": "SOL",
                    "amount": diff / 1e9,
                    "source": "rpc",
                })
                if len(events) >= limit:
                    return events
    events.sort(key=lambda e: (e["block"], e["block_time"]), reverse=True)
    return events[:limit]


# ============================================================ public API
def fetch_whale_events(
    chain: str,
    *,
    limit: int = 5,
    min_native_amount: float = 100.0,
    blocks_to_scan: int = 3,
    transport: HttpTransport | None = None,
    rpc_url: str | None = None,
    api_key: str = "",
    watch_addresses: list[str] | None = None,
    allow_mock: bool | None = None,
    config_like=None,
) -> list[dict]:
    """Pull recent whale-sized native transfers.

    Uses JSON-RPC by default (no keys, no paid services). If ``api_key`` is
    provided (explicit arg, ``NERYA_SCAN_KEY_<CHAIN>`` env, or global
    ``NERYA_SCAN_KEY``), the richer Etherscan-family Scan API is used
    instead for EVM chains; Solana always uses RPC.
    """
    http = transport or UrllibHttp(rate_limit_per_sec=4.0)
    chain_l = (chain or "").lower()
    err = ""
    events: list[dict] = []

    try:
        if chain_l == "solana":
            url = rpc_url or _resolve_rpc_url(chain_l)
            events = _scan_solana_rpc(
                rpc_url=url, http=http,
                min_native_amount=min_native_amount,
                slots_to_scan=blocks_to_scan, limit=limit,
            ) if url else []
        elif chain_l in _EVM_RPCS:
            resolved_key = _resolve_scan_api_key(chain_l, api_key)
            if resolved_key and chain_l in _EVM_SCAN_APIS:
                events = _scan_evm_scanapi(
                    chain_l=chain_l,
                    api_url=_EVM_SCAN_APIS[chain_l],
                    api_key=resolved_key,
                    http=http,
                    min_native_amount=min_native_amount,
                    limit=limit,
                    watch_addresses=watch_addresses,
                )
            else:
                url = rpc_url or _resolve_rpc_url(chain_l)
                events = _scan_evm_rpc(
                    chain_l=chain_l, rpc_url=url, http=http,
                    min_native_amount=min_native_amount,
                    blocks_to_scan=blocks_to_scan, limit=limit,
                ) if url else []
        else:
            err = "unknown_chain"
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        log.debug("whale scan failed %s: %s", chain_l, exc)
        events = []

    if events:
        return tag_list_envelope(
            events, live_envelope(source="rpc", venue=chain_l)
        )
    if resolve_allow_mock(allow_mock, config_like):
        return tag_list_envelope(
            mock_whale_events(chain, count=limit),
            mock_envelope(source="mock", venue=chain_l),
        )
    return tag_list_envelope(
        [], degraded_envelope("onchain", error=err or "no_events", venue=chain_l),
    )


def mock_whale_events(chain: str, count: int = 3) -> list[dict]:
    return [
        {"chain": chain, "wallet": f"0xwhale_{i}", "action": "buy",
         "token": "ETH", "amount": 100 + i * 10, "source": "mock"}
        for i in range(count)
    ]


__all__ = ["fetch_whale_events", "mock_whale_events"]
