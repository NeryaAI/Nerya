from threading import Lock

import pytest

from nerya.connectors.evm_native import EVMNative
from nerya.core.errors import TradingError


class LogTransport:
    def __init__(self, failure=None):
        self.calls = []
        self.lock = Lock()
        self.failure = failure

    def request(self, method, url, *, body, timeout):
        query = body['params'][0]
        start, end = int(query['fromBlock'],16), int(query['toBlock'],16)
        with self.lock:
            self.calls.append(query)
        assert end-start < 100
        if self.failure == start:
            return 413, {'error':{'message':'range refused'}}
        return 200, {'result':[{'blockNumber':hex(block)} for block in range(start,end+1)]}


def test_monad_pages_cover_every_block_once_and_preserve_filters():
    transport = LogTransport()
    connector = EVMNative(chain='monad-testnet',chain_id=10143,rpc_url='https://example.invalid',transport=transport)
    query = {'address':'0x'+'12'*20,'topics':['topic',None,'owner'],'fromBlock':'0x64','toBlock':'0x190'}
    result = connector._rpc('eth_getLogs',[query])
    assert [int(row['blockNumber'],16) for row in result] == list(range(100,401))
    assert query['fromBlock']=='0x64' and query['toBlock']=='0x190'
    assert sorted((int(q['fromBlock'],16),int(q['toBlock'],16)) for q in transport.calls)==[(100,199),(200,299),(300,399),(400,400)]
    assert all(q['topics']==query['topics'] and q['address']==query['address'] for q in transport.calls)


def test_failed_page_never_returns_partial_inventory():
    connector = EVMNative(chain='monad-testnet',chain_id=10143,rpc_url='https://example.invalid',transport=LogTransport(failure=200))
    with pytest.raises(TradingError,match='http 413'):
        connector._rpc('eth_getLogs',[{'fromBlock':'0x64','toBlock':'0x190'}])


def test_other_chains_and_blockhash_queries_are_not_paginated():
    class Transport:
        def request(self, method, url, *, body, timeout):
            self.body = body
            return 200, {'result':[]}
    for chain_id, query in ((143, {'fromBlock':'0x1','toBlock':'0xffff'}),(10143,{'blockHash':'0xabc'})):
        transport=Transport()
        connector=EVMNative(chain_id=chain_id,rpc_url='https://example.invalid',transport=transport)
        assert connector._rpc('eth_getLogs',[query])==[]
        assert transport.body['params']==[query]
