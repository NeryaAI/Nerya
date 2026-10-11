from copy import deepcopy
from types import SimpleNamespace
import base64
import time

import pytest
from eth_abi import encode

from nerya.core.config import Config,DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core.redaction import redact_public_record
from nerya.financial.contracts import FinancialError
from nerya.financial.adapters import EvmFunds,units,validate_evm_address
from nerya.financial.solana_funds import message,instruction,SYSTEM,pubkey
from nerya.financial.lifi import decode_bridge_transaction,BRIDGE_DATA
from nerya.connectors.solana_native import _first_required_signer

pytestmark=pytest.mark.smoke
SENDER="0x"+"1"*40
DEST="0x"+"2"*40
ASSET="0x"+"3"*40


def config(tmp_path):
    data=deepcopy(DEFAULT_CONFIG);data["financial"]={"wallet_permissions":{"wallet":{"wallet_transfer":True,"contract_approval":True}}}
    return Config(WorkspacePaths(tmp_path),data)


def test_public_addresses_hashes_and_private_keys_are_separate():
    public="0x"+"a"*64
    out=redact_public_record({"recipient":DEST,"transaction_hash":public,"private_key":public},
        identity_fields=frozenset({"recipient","transaction_hash"}))
    assert out["recipient"]==DEST and out["transaction_hash"]==public
    assert out["private_key"]!=public


def test_evm_units_reject_precision_and_unlimited():
    assert units("1.234567",6)==1234567
    with pytest.raises(FinancialError,match="precision"):units("1.0000001",6)
    with pytest.raises(FinancialError,match="unlimited"):units(str(2**256-1),0)
    with pytest.raises(FinancialError,match="address"):validate_evm_address("0x"+"1"*64)


def test_evm_frozen_transfer_and_allowance_cannot_change(tmp_path):
    class Rpc:
        def _verify_chain_id(self):pass
        def get_erc20_decimals(self,_):return 6
    provider=SimpleNamespace(id="self_custody",_resolve_signer_key=lambda:"unused")
    adapter=EvmFunds(config(tmp_path),{"chain":"base"},connector=Rpc(),provider=provider,
                     wallet_config={"address":SENDER,"native_symbol":"ETH","token_symbols":{ASSET:"USDC"}})
    request={"kind":"wallet_transfer","wallet_id":"wallet","chain":"base","asset":ASSET,"amount":"1","recipient":DEST}
    transfer="0xa9059cbb"+DEST[2:].rjust(64,"0")+hex(1000000)[2:].rjust(64,"0")
    quote={"expires_at":time.time()+60,"transaction":{"from":SENDER,"to":ASSET,"value":"0x0","data":transfer}}
    adapter.validate(request,quote)
    with pytest.raises(FinancialError,match="mismatch"):
        adapter.validate({**request,"amount":"2"},quote)
    approval={**request,"kind":"contract_approval","spender":DEST}
    approve_data="0x095ea7b3"+DEST[2:].rjust(64,"0")+hex(1000000)[2:].rjust(64,"0")
    adapter.validate(approval,{**quote,"transaction":{**quote["transaction"],"data":approve_data}})
    with pytest.raises(FinancialError,match="allowance"):
        adapter.validate({**approval,"spender":SENDER},{**quote,"transaction":{**quote["transaction"],"data":approve_data}})


def test_solana_message_pins_the_payer_and_instructions():
    import base58
    payer=base58.b58encode(bytes([1])*32).decode();receiver=base58.b58encode(bytes([2])*32).decode();blockhash=base58.b58encode(bytes([3])*32).decode()
    transfer=instruction(SYSTEM,[(payer,True),(receiver,True)],bytes([2,0,0,0])+int(1000).to_bytes(8,"little"))
    encoded=message(payer,blockhash,[transfer])
    assert _first_required_signer(encoded)==payer
    assert encoded.endswith(base64.b64decode(transfer["data"]))
    assert pubkey(receiver) in encoded


def test_bridge_decoder_rejects_receiver_and_unenforced_output():
    selector="0x12345678"
    bridge=(bytes(32),"reviewed","nerya","0x"+"0"*40,ASSET,DEST,1000000,8453,False,False)
    tx={"chainId":1,"data":selector+encode([BRIDGE_DATA,"uint256"],[bridge,990000]).hex()}
    request={"recipient":DEST,"asset":ASSET}
    allowed={selector:{"extra_types":["uint256"],"minimum_output_path":[1]}}
    out=decode_bridge_transaction(tx,request,1,8453,1000000,selectors=allowed,minimum_output=980000)
    assert out["receiver"].lower()==DEST.lower()
    with pytest.raises(FinancialError,match="receiver"):
        decode_bridge_transaction(tx,{**request,"recipient":SENDER},1,8453,1000000,selectors=allowed,minimum_output=980000)
    with pytest.raises(FinancialError,match="floor_mismatch"):
        decode_bridge_transaction(tx,request,1,8453,1000000,selectors=allowed,minimum_output=999999)
    with pytest.raises(FinancialError,match="not_reviewed"):
        decode_bridge_transaction(tx,request,1,8453,1000000,selectors={selector:{}},minimum_output=1)


def test_unknown_bridge_selector_never_reaches_signer():
    with pytest.raises(FinancialError,match="selector_not_reviewed"):
        decode_bridge_transaction({"data":"0xdeadbeef","chainId":1},{"recipient":DEST,"asset":ASSET},1,8453,1,selectors={})
