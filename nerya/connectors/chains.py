"""Reviewed EVM identities and protocol deployments, independent of wallets."""
from dataclasses import dataclass
from typing import Any

from ..core.errors import TradingError


@dataclass(frozen=True)
class ChainSpec:
    name: str
    chain_id: int
    native_symbol: str = "ETH"
    gas_style: str = "eip1559"
    confirmations: int = 2
    testnet: bool = False

    def __post_init__(self):
        if isinstance(self.chain_id, bool) or self.chain_id <= 0 or self.confirmations < 1:
            raise TradingError("invalid_chain_identity")
        if self.gas_style not in {"legacy", "eip1559"}:
            raise TradingError("unsupported_chain_gas_model")

    def verify(self, connector):
        actual = connector._rpc("eth_chainId", [])
        if int(actual, 16) != self.chain_id:
            raise TradingError("rpc_chain_identity_mismatch")


_SPECS = (
    ChainSpec("ethereum", 1), ChainSpec("base", 8453), ChainSpec("bsc", 56, "BNB", "legacy"),
    ChainSpec("arbitrum", 42161), ChainSpec("robinhood", 4663), ChainSpec("robinhood-testnet", 46630, testnet=True),
    ChainSpec("polygon", 137, "POL"), ChainSpec("optimism", 10), ChainSpec("avalanche", 43114, "AVAX"),
    ChainSpec("linea", 59144), ChainSpec("zksync", 324), ChainSpec("blast", 81457), ChainSpec("scroll", 534352),
    ChainSpec("mantle", 5000, "MNT"), ChainSpec("fantom", 250, "FTM"), ChainSpec("celo", 42220, "CELO"),
    ChainSpec("gnosis", 100, "XDAI"), ChainSpec("sepolia", 11155111, testnet=True),
    ChainSpec("base-sepolia", 84532, testnet=True),
)
CHAIN_IDS = {item.name: item.chain_id for item in _SPECS}
ALIASES = {"eth": "ethereum", "bnb": "bsc", "bnb-chain": "bsc", "arb": "arbitrum", "robinhood-chain": "robinhood"}


class ChainRegistry:
    def __init__(self, config=None):
        self.specs = {item.name: item for item in _SPECS}
        self.config = config
        for name, raw in (config.get("trading.chains", {}) if config else {}).items():
            spec = ChainSpec(name, raw["chain_id"], raw.get("native_symbol", "ETH"), raw.get("gas_style", "eip1559"),
                             raw.get("confirmations", 2), raw.get("testnet", False))
            if name in CHAIN_IDS and CHAIN_IDS[name] != spec.chain_id:
                raise TradingError("builtin_chain_identity_cannot_change")
            self.specs[name] = spec

    def get(self, name):
        canonical = ALIASES.get(name.lower(), name.lower())
        if canonical not in self.specs:
            raise TradingError("unsupported_evm_chain")
        return self.specs[canonical]

    def deployment(self, chain, protocol, connector):
        spec = self.get(chain)
        spec.verify(connector)
        raw = (self.config.get("financial.defi.deployments", {}) if self.config else {}).get(spec.name, {}).get(protocol)
        if not raw or raw.get("reviewed") is not True:
            raise TradingError("protocol_deployment_not_reviewed")
        from eth_utils import keccak
        for name, address in (raw.get("contracts") or {}).items():
            expected = (raw.get("code_hashes") or {}).get(name)
            if not expected or not isinstance(address, str) or len(address) != 42:
                raise TradingError("protocol_contract_evidence_missing")
            code = connector._rpc("eth_getCode", [address, "latest"])
            if not code or code == "0x" or "0x" + keccak(bytes.fromhex(code[2:])).hex() != expected.lower():
                raise TradingError("protocol_contract_code_changed")
            implementation=(raw.get("implementations") or {}).get(name)
            if implementation:
                slot=implementation.get("slot") or "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
                stored=connector._rpc("eth_getStorageAt",[address,slot,"latest"])
                target=implementation.get("address")
                if not target or stored[-40:].lower()!=target[2:].lower():raise TradingError("protocol_proxy_implementation_changed")
                deployed=connector._rpc("eth_getCode",[target,"latest"])
                if not deployed or deployed=="0x" or "0x"+keccak(bytes.fromhex(deployed[2:])).hex()!=str(implementation.get("code_hash")).lower():
                    raise TradingError("protocol_proxy_implementation_code_changed")
        if not raw.get("contracts"):
            raise TradingError("protocol_contract_evidence_missing")
        return raw
