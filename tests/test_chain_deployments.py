from types import SimpleNamespace
from copy import deepcopy

import pytest
from eth_utils import keccak

from nerya.connectors.chains import ChainRegistry
from nerya.core.errors import TradingError
from test_trading_kernel_safety import cfg

pytestmark = pytest.mark.smoke


def test_network_aliases_do_not_default_unknown_chain_to_ethereum():
    registry = ChainRegistry()
    assert [registry.get(name).chain_id for name in ("ETH", "BASE", "Robinhood", "BNB", "Arb")] == [1, 8453, 4663, 56, 42161]
    with pytest.raises(TradingError, match="unsupported_evm_chain"):
        registry.get("unknown")
    with pytest.raises(TradingError, match="identity_mismatch"):
        registry.get("base").verify(SimpleNamespace(_rpc=lambda *a: "0x1"))


def test_protocol_readiness_requires_review_and_actual_contract_code(cfg):
    address = "0x" + "1"*40
    code = "0x60016001"
    connector = SimpleNamespace(_rpc=lambda name, args: "0x2105" if name == "eth_chainId" else code)
    cfg.data["financial"]["defi"] = {"deployments": {"base": {"aave_v3": {
        "reviewed": True, "contracts": {"pool": address}, "code_hashes": {"pool": "0x" + keccak(bytes.fromhex(code[2:])).hex()}}}}}
    registry = ChainRegistry(cfg)
    assert registry.deployment("base", "aave_v3", connector)["contracts"]["pool"] == address
    cfg.data["financial"]["defi"]["deployments"]["base"]["aave_v3"]["code_hashes"]["pool"] = "0x" + "0"*64
    with pytest.raises(TradingError, match="code_changed"):
        registry.deployment("base", "aave_v3", connector)
    with pytest.raises(TradingError, match="not_reviewed"):
        registry.deployment("robinhood", "aave_v3", SimpleNamespace(_rpc=lambda *a: hex(4663)))
