"""Independent local EVM acceptance; never reads a real private key or broadcasts remotely.

Anvil must run on 127.0.0.1:19643 with chain-id 10143. Only WMON is a local
test harness; Uniswap factory, pools, router, quoter, NFPM are genuine artifacts.
"""
import json
import os
import subprocess
import time
from urllib.request import Request, urlopen

from eth_account import Account
from eth_utils import keccak

from deploy import CHAIN_ID, ROOT, WMON, Deployment, RPC, call_data, load_bundle, sqrt_price


class LocalRPC:
    def __call__(self, method, params):
        request = Request('http://127.0.0.1:19643', json.dumps(dict(jsonrpc='2.0',id=1,method=method,params=params)).encode(),
                          {'Content-Type':'application/json'})
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        if 'error' in result:
            raise RuntimeError(method+':'+result['error']['message'])
        return result['result']


def rejects(action, reason):
    try:
        action()
    except RuntimeError as exc:
        assert reason in str(exc), str(exc)
    else:
        raise AssertionError('expected rejection: '+reason)


def main():
    bundle = load_bundle(ROOT/'build/artifacts.json')
    for url in ('https://rpc.monad.xyz','http://127.0.0.1:19643','https://testnet-rpc.monad.xyz.attacker.invalid','https://testnet-rpc.monad.xyz/?redirect=1'):
        rejects(lambda:RPC(url),'rpc_not_allowlisted_testnet')
    assert sqrt_price(1,1) == 2**96
    assert sqrt_price(10**18,10**6) == 79228162514264337593543
    rpc = LocalRPC()
    rpc('anvil_reset', [])
    assert int(rpc('eth_chainId',[]),16) == CHAIN_ID
    account = Account.create()  # memory-only throwaway local test account
    rpc('anvil_setBalance',[account.address,hex(5*10**18)])
    source = '''// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;
contract LocalTestWETH {
  string public constant name="LOCAL TEST WMON";
  string public constant symbol="WMON";
  uint8 public constant decimals=18;
  mapping(address=>uint) public balanceOf;
  mapping(address=>mapping(address=>uint)) public allowance;
  event Transfer(address indexed from,address indexed to,uint value);
  event Approval(address indexed owner,address indexed spender,uint value);
  function deposit() external payable { balanceOf[msg.sender]+=msg.value; emit Transfer(address(0),msg.sender,msg.value); }
  function withdraw(uint n) external { require(balanceOf[msg.sender]>=n); balanceOf[msg.sender]-=n; payable(msg.sender).transfer(n); }
  function approve(address s,uint n) external returns(bool){ allowance[msg.sender][s]=n; emit Approval(msg.sender,s,n); return true; }
  function transfer(address to,uint n) external returns(bool){ return transferFrom(msg.sender,to,n); }
  function transferFrom(address from,address to,uint n) public returns(bool){
    require(balanceOf[from]>=n);
    if(from!=msg.sender){require(allowance[from][msg.sender]>=n); allowance[from][msg.sender]-=n;}
    balanceOf[from]-=n; balanceOf[to]+=n; emit Transfer(from,to,n); return true;
  }
}'''
    modules = os.environ.get('MONAD_ARTIFACT_NODE_MODULES','/Users/rick/Documents/Project/Nerya/pitch-video/monad-metropolis-20261013/v3/fork/node_modules')
    code = 'const s=require(process.argv[1]+"/solc");let b="";process.stdin.on("data",c=>b+=c);process.stdin.on("end",()=>process.stdout.write(s.compile(b)));'
    request = dict(language='Solidity',sources={'local.sol':dict(content=source)},
                   settings=dict(evmVersion='paris',outputSelection={'*':{'*':['evm.deployedBytecode.object']}}))
    compiled = json.loads(subprocess.check_output(['node','-e',code,modules], input=json.dumps(request).encode()))
    wmon_code = '0x'+compiled['contracts']['local.sol']['LocalTestWETH']['evm']['deployedBytecode']['object']
    rpc('anvil_setCode',[WMON,wmon_code])
    out = ROOT/'validation'
    out.mkdir(exist_ok=True)
    journal = out/'local.journal.json'
    journal.unlink(missing_ok=True)
    run = Deployment(rpc,bundle,journal,account,sender=account.address)
    deployment = run.run(out/'local')
    count = int(rpc('eth_getTransactionCount',[account.address,'latest']),16)
    hashes = [r['hash'] for r in run.journal['transactions'].values()]
    # Restart from disk must skip every confirmed deployment, wrap, approve and mint.
    restarted = Deployment(rpc,bundle,journal,account,sender=account.address)
    assert restarted.run(out/'local') == deployment
    assert int(rpc('eth_getTransactionCount',[account.address,'latest']),16) == count
    assert [r['hash'] for r in restarted.journal['transactions'].values()] == hashes
    wrong_chain = lambda method,params: '0x8f' if method=='eth_chainId' else rpc(method,params)
    rejects(lambda:Deployment(wrong_chain,bundle,out/'wrong-chain.journal.json',sender=account.address),'chain_10143_required')
    low = Deployment(rpc,bundle,out/'budget.journal.json',account,'0.000000001',sender=account.address)
    rejects(lambda:low.transact('budget',WMON,call_data('deposit()'),1),'total_gas_budget_would_exceed')
    assert not low.journal['transactions']
    # Test genuine quoter and eight-field router against both actual canonical pools.
    swaps = []
    for name,pool in deployment['pools'].items():
        token_in = WMON
        token_out = pool['token1'] if pool['token0'].lower()==WMON.lower() else pool['token0']
        quantity = 10**15
        quote_type = '(address,address,uint256,uint24,uint160)'
        quote = run.read(deployment['contracts']['quoter'],'quoteExactInputSingle('+quote_type+')', [quote_type],
                         [(token_in,token_out,quantity,pool['fee'],0)], ['uint256','uint160','uint32','uint256'])
        assert quote[0] > 0 and quote[1] > 0 and quote[3] > 0
        router = deployment['contracts']['router']
        run.approve('selftest:approve:'+name,token_in,router,quantity)
        before = run.read(token_out,'balanceOf(address)',['address'],[account.address],['uint256'])[0]
        swap_type = '(address,address,uint24,address,uint256,uint256,uint256,uint160)'
        receipt = run.transact('selftest:swap:'+name,router,call_data('exactInputSingle('+swap_type+')',[swap_type],
                              [(token_in,token_out,pool['fee'],account.address,int(time.time())+120,quantity,quote[0]*99//100,0)]))
        after = run.read(token_out,'balanceOf(address)',['address'],[account.address],['uint256'])[0]
        assert after-before == quote[0]
        event = '0x'+keccak(text='Swap(address,address,int256,int256,uint160,uint128,int24)').hex()
        assert any(log['address'].lower()==pool['address'].lower() and log['topics'][0]==event for log in receipt['logs'])
        assert run.read(token_in,'allowance(address,address)',['address','address'],[account.address,router],['uint256'])[0] == 0
        swaps.append(dict(pool=name,amount_in=str(quantity),amount_out=str(quote[0]),hash=receipt['transactionHash']))
    # Metadata and real LP NFT ownership are available.
    manager = deployment['contracts']['position_manager']
    for pool in deployment['pools'].values():
        token_id = pool['seed_token_id']
        assert run.read(manager,'ownerOf(uint256)',['uint256'],[token_id],['address'])[0].lower()==account.address.lower()
        uri = run.read(manager,'tokenURI(uint256)',['uint256'],[token_id],['string'])[0]
        assert 'Test-only canonical Uniswap V3' in uri
    print(json.dumps(dict(ok=True,network='local EVM only',chain_id=CHAIN_ID,
                          bootstrap_transactions=count,idempotent_rerun=True,budget_rejection=True,wrong_chain_rejection=True,
                          pools_seeded=2,canonical_router_swaps=swaps,gas_used_MON=str(run.spent_and_reserved()/10**18))))


if __name__=='__main__':
    main()
