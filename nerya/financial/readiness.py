"""Read-only execution inventory: implementation is not live readiness.

No connector construction, credential resolution, network calls or account
orders. This is a configuration/dependency preflight, not venue certification.
"""
from importlib import metadata
import time


def _version(distribution):
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def execution_readiness(config, *, version_provider=None):
    version_provider = version_provider or _version
    dependencies = {name:version_provider(name) for name in ('ccxt','eth-account','eth-abi','pynacl','base58','polymarket-client')}
    from ..wallet.registry import list_configured_providers
    from ..core import yaml_io
    wallets = list_configured_providers(config.data)
    accounts_doc = yaml_io.load(config.paths.accounts_file, default={}) or {}
    accounts = accounts_doc.get('accounts', [])
    if not isinstance(accounts,list):
        accounts = []
    accounts = [row for row in accounts if isinstance(row,dict)]
    live = config.live_trading_enabled() and not config.kill_switch()
    funds = bool(config.get('financial.enabled',False))

    def report(identifier, label, actions, missing, resources, limits, *, needs_funds=False, implementation='implemented'):
        blockers = list(dict.fromkeys(missing))
        if implementation != 'implemented':
            state = 'unsupported'
        elif blockers:
            state = 'configuration_required'
        elif not live or needs_funds and not funds:
            state = 'runtime_disabled'
        else:
            state = 'ready_for_preflight'
        if not live:
            blockers.append('runtime_live_disabled_or_kill_switch')
        if needs_funds and not funds:
            blockers.append('financial_gateway_disabled')
        return {'id':identifier,'label':label,'implementation':implementation,'state':state,
                'actions':actions,'resources':resources,'blockers':blockers,'limitations':limits,
                'live_verified':False}

    def missing_dependencies(names):
        return ['dependency:'+name for name in names if not dependencies[name]]

    def credentialed(account, fields):
        credentials = account.get('credentials') or {}
        provider = account.get('provider_config') or {}
        return all(str(credentials.get(field) or provider.get(field) or account.get(field+'_ref') or '').startswith('vault://') for field in fields)

    def active_account(account):
        return (account.get('mode') == 'live' and account.get('status') == 'active'
                and account.get('live_trading_enabled') is True and (account.get('permissions') or {}).get('place_order') is True)

    cex = [row for row in accounts if row.get('kind','cex') == 'cex' and row.get('venue',row.get('exchange')) not in {None,'mock','paper'}]
    usable_cex = [row for row in cex if active_account(row) and credentialed(row,('api_key','api_secret'))]
    result = [report('cex','CCXT spot and linear derivatives',['open_position','reduce_position','close_position','attach_protection'],
        missing_dependencies(('ccxt',)) + ([] if usable_cex else ['credentialed_cex_account_required']),
        [row['id'] for row in usable_cex if row.get('id')],
        ['Venue/account mode and position-side acceptance still require per-venue tests.',
         'Inverse contracts are not supported by the base-sized execution path.'])]

    predictions = [row for row in accounts if row.get('kind') == 'prediction_market' and str(row.get('venue',row.get('exchange',''))).lower() in {'polymarket','polymarket_v2','pm'}]
    pm = [row for row in predictions if active_account(row) and credentialed(row,('api_key','api_secret','api_passphrase','private_key'))]
    result.append(report('polymarket','Polymarket CLOB',['buy_outcome','sell_outcome','cancel_order','confirmed_fill_polling'],
        ([] if dependencies['polymarket-client']=='0.12.0' else ['dependency:polymarket-client==0.12.0'])+
        ([] if pm else ['polymarket_signer_and_clob_credentials_required']),
        [row['id'] for row in pm if row.get('id')],
        ['Data API v2; GTD at least 180 seconds ahead.', 'No native leverage, shorts or TP/SL.',
         'Exchange fees remain unverified unless independent fee evidence is present.']))

    solana = []
    for wallet in wallets:
        raw = wallet.get('config') or {}
        if wallet.get('provider') not in {'self_custody','metamask'}:
            continue
        if not str(raw.get('signer_ref','')).startswith('vault://') or not str(raw.get('jupiter_api_key_ref','')).startswith('vault://'):
            continue
        if not (raw.get('rpc_urls') or {}).get('solana') and not raw.get('solana_rpc_url'):
            continue
        solana.append(wallet['wallet_id'])
    result.append(report('jupiter_v2','Jupiter aggregated Solana swaps',['quote','swap','receipt_recovery'],
        missing_dependencies(('pynacl','base58'))+([] if solana else ['solana_rpc_signer_and_jupiter_key_required']), solana,
        ['Raydium/Orca/Meteora route availability is determined by a live Jupiter quote, not guaranteed by its brand name.',
         'No sponsored wallets or multi-signer RFQ execution.', 'Aggregate swaps do not implement native Solana LP positions.'],needs_funds=True))

    deployments = config.get('financial.defi.deployments',{})
    wallet_permissions = config.get('financial.wallet_permissions',{})
    for protocol,label,actions in (
        ('uniswap_v3','Uniswap v3 LP',['lp_add','lp_remove','lp_collect']),
        ('uniswap_v4','Uniswap v4 LP',['lp_add','lp_remove','lp_collect']),
        ('pancakeswap_v3','PancakeSwap v3 LP',['lp_add','lp_remove','lp_collect']),
        ('aave_v3','Aave v3',['lend_supply','lend_withdraw','borrow','repay']),
        ('polymarket_ctf','Polymarket standard CTF redemption',['redeem']),
    ):
        matched=[]
        for chain, protocols in deployments.items():
            deployment=protocols.get(protocol) or {}
            if not deployment.get('reviewed') or not deployment.get('contracts') or not deployment.get('code_hashes'):
                continue
            if protocol in {'uniswap_v3','uniswap_v4','pancakeswap_v3'} and (not deployment.get('pools') or not deployment.get('price_feeds')):
                continue
            for wallet in wallets:
                raw=wallet.get('config') or {}
                if not str(raw.get('signer_ref','')).startswith('vault://') or not (raw.get('rpc_urls') or {}).get(chain):
                    continue
                enabled=[action for action in actions if wallet_permissions.get(wallet['wallet_id'],{}).get(action) is True]
                if enabled:
                    matched.append({'wallet_id':wallet['wallet_id'],'chain':chain,'actions':enabled})
        limitations=['Pool/deployment code, price evidence, balances, finite grants and chain receipts are checked at execution.']
        if protocol=='uniswap_v4': limitations.append('Only reviewed static-fee pools with zero hooks and ERC20 currencies.')
        if protocol=='pancakeswap_v3': limitations.append('No Infinity hooks or MasterChef farming.')
        if protocol=='aave_v3': limitations.append('Variable debt only; no E-mode. Borrow/withdraw require a health-factor buffer.')
        if protocol=='polymarket_ctf': limitations.append('Only a linked EOA and fully strategy-owned standard binary outcomes; existing ERC1155 operator approval is required. No proxy/negative-risk/combo settlement.')
        result.append(report(protocol,label,actions,missing_dependencies(('eth-account','eth-abi'))+
            ([] if matched else ['reviewed_deployment_rpc_wallet_and_action_permissions_required']),matched,limitations,needs_funds=True))
    lp_resources=[resource for item in result if item['id'] in {'uniswap_v3','uniswap_v4','pancakeswap_v3'} for resource in item['resources']]
    result.append(report('lp_rebalance','Durable LP range adjustment',['prepare_rebalance','advance_rebalance','get_rebalance','stop_rebalance'],
        [] if lp_resources else ['configured_lp_child_actions_required'],lp_resources,['Uses Uniswap/Pancake child actions; each action retains its own approval and quote.',
               'Only confirmed exit proceeds may fund the next step. A failed sequence never silently starts over.'],needs_funds=True))
    return {'as_of':time.time(),'probe_kind':'local_configuration_only','network_probed':False,
            'dependencies':dependencies,'families':result,
            'unsupported':['native_raydium_lp','native_orca_lp','native_meteora_lp','byreal_lp',
                           'inverse_futures','polymarket_proxy_redemption','polymarket_combo_redemption'],
            'live_trading_enabled':live,'financial_enabled':funds}
