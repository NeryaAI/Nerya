"""Durable wallet transaction state; never retry a possibly broadcast swap."""
from __future__ import annotations
import hashlib
import json
import time
from ..core.atomic_write import atomic_write_text


def _path(config, execution_id):
    return config.paths.state / 'wallet_swaps' / (hashlib.sha256(str(execution_id).encode()).hexdigest()+'.json')


def read(config, execution_id):
    path = _path(config, execution_id)
    return json.loads(path.read_text()) if path.exists() else None


def write(config, execution_id, **fields):
    state = {**(read(config,execution_id) or {}), **fields, 'execution_id':execution_id, 'updated_at':time.time()}
    atomic_write_text(_path(config,execution_id), json.dumps(state,default=str))
    return state


def broadcast_callback(config, execution_id):
    def record(tx):
        # The connector computes the hash before sending, so a transport timeout
        # still leaves an identity that can be looked up without re-broadcast.
        safe = {k:v for k,v in tx.items() if k not in ('raw_transaction','signed_tx','private_key')}
        aid=config.get('runtime.financial_action_id')
        if aid:
            from ..financial.store import FinancialStore
            FinancialStore(config).mark(aid,state='submitted',submission={
                'transaction_hash':safe.get('tx_hash'),'chain':safe.get('chain'),'execution_ref':safe.get('execution_ref')})
        return write(config,execution_id,status='submitted',transaction=safe)
    return record
