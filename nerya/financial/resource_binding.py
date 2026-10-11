"""Grant scope pins resource identities, even when a display id is reused."""
from .contracts import security_revision,FinancialError


def canonical_request(config,request):
    request=dict(request)
    if request.get("wallet_id"):
        from ..connectors.chains import ChainRegistry
        for field in ("chain","to_chain"):
            if request.get(field):
                value=str(request[field]).lower()
                request[field]="solana" if value in {"sol","solana"} else ChainRegistry(config).get(value).name
        if request.get("account_id"):
            from ..trading.accounts import get_account_profile
            if get_account_profile(config.paths,request["account_id"]).wallet_id!=request["wallet_id"]:
                raise FinancialError("financial_account_wallet_binding_mismatch",403)
    return request


def canonical_policy(config,policy):
    if not policy["resources"].get("wallets"):return policy
    from ..connectors.chains import ChainRegistry
    registry=ChainRegistry(config)
    policy["resources"]["chains"]=list(dict.fromkeys("solana" if name.lower() in {"sol","solana"} else registry.get(name).name for name in policy["resources"].get("chains",[])))
    return policy


def resource_revisions(config,policy):
    result={};resources=policy['resources']
    from ..trading.accounts import get_account_profile
    from ..wallet.bindings import binding_fingerprint
    from ..wallet.errors import WalletError
    for aid in resources.get('accounts',[]):
        profile=get_account_profile(config.paths,aid)
        from ..connectors.provider_spec import get_registry
        provider=get_registry(config.paths.root).find(profile.venue)
        result['account:'+aid]=security_revision({"account":profile.raw,"provider":provider.binding() if provider else None})
    for wid in resources.get('wallets',[]):
        try:result['wallet:'+wid]=binding_fingerprint(config,{'wallet_id':wid})
        except (WalletError,ValueError) as exc:raise FinancialError('grant_wallet_resource_unavailable',422) from exc
    if resources.get("components"):
        from ..trading.components import trading_components
        registry=trading_components(config)
        for cid in resources["components"]:
            result["component:"+cid]=registry.revision(cid)
    return result
