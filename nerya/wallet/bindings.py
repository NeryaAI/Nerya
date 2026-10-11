"""Resolve an exact wallet binding and pin its execution identity."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from .errors import WalletPolicyDenied


def resolve_binding(config, payload):
    from .registry import list_configured_providers, resolve_provider_name
    body = dict(payload or {})
    provider = str(body.get('provider') or '').strip().lower()
    if provider:
        provider = resolve_provider_name(provider) or provider
    wallet_id = str(body.get('wallet_id') or '').strip()
    account_id = str(body.get('account_id') or '').strip()
    if account_id:
        from ..trading.accounts import get_account_profile
        account = get_account_profile(config.paths, account_id)
        settlement = (account.kind == 'prediction_market' and body.get('kind') == 'redeem'
                      and body.get('protocol') == 'polymarket_ctf' and body.get('chain') == 'polygon')
        if (account.kind not in ('chain', 'dex') and not settlement) or not account.wallet_id:
            raise WalletPolicyDenied('account has no on-chain wallet binding')
        if wallet_id and wallet_id != account.wallet_id:
            raise WalletPolicyDenied('account_id/wallet_id mismatch')
        wallet_id = account.wallet_id
    bindings = list_configured_providers(config.data)
    if wallet_id:
        selected = next((b for b in bindings if b['wallet_id'] == wallet_id), None)
        if selected is None and wallet_id == provider and not any(b['provider']==provider for b in bindings):
            selected = {'wallet_id':wallet_id,'provider':provider,'config':dict(config.get('wallet.'+provider,{}) or {})}
        if selected is None:
            raise WalletPolicyDenied('unknown wallet_id')
        if provider and selected['provider'] != provider:
            raise WalletPolicyDenied('wallet_id/provider mismatch')
    else:
        provider = provider or str(config.get('wallet.provider', '') or '').lower()
        candidates = [b for b in bindings if not provider or b['provider'] == provider]
        if len(candidates) > 1:
            raise WalletPolicyDenied('multiple wallets match; specify wallet_id or account_id')
        selected = candidates[0] if candidates else {'wallet_id':provider, 'provider':provider,
            'config':dict(config.get('wallet.' + provider, {}) or {})}
    if not selected['provider']:
        raise WalletPolicyDenied('no wallet provider selected')
    cfg = dict(selected.get('config') or {})
    overrides = (cfg.pop('accounts', None) or {}).get(account_id)
    if isinstance(overrides, dict):
        cfg.update(overrides)
    strategy_id = str(body.get('strategy_id') or '')
    overrides = (cfg.pop('strategies', None) or {}).get(strategy_id)
    if isinstance(overrides, dict):
        cfg.update(overrides)
    return str(selected['wallet_id']), str(selected['provider']), cfg


def binding_fingerprint(config, payload):
    wid, provider, cfg = resolve_binding(config, payload)
    identity = {'wallet_id':wid, 'provider':provider, 'config':cfg}
    # Credential rotations and external keypair replacement invalidate approval.
    files = [config.paths.root / 'vault' / 'secrets.enc']
    if cfg.get('keypair_path'):
        files.append(Path(cfg['keypair_path']).expanduser())
    for argument in cfg.get('command') or []:
        path=Path(argument).expanduser()
        if path.is_absolute() and path.is_file():files.append(path)
    if cfg.get('skill_path') and cfg.get('entry'):
        files.append(Path(cfg['skill_path']).expanduser()/cfg['entry'])
    identity['files'] = {str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in files if f.is_file()}
    return hashlib.sha256(json.dumps(identity,sort_keys=True,default=str).encode()).hexdigest()
