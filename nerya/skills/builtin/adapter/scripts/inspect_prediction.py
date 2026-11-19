"""Read prediction outcomes/book/history without private credentials."""
from nerya.connectors.registry import build_connector
from nerya.connectors.provider_spec import get_registry
from nerya.skills.manifest import cli_main


def run(_ctx=None,**payload):
    market=str(payload.get('market') or '')
    provider=str(payload.get('provider') or (market.split(':',1)[0] if ':' in market else ''))
    workspace=_ctx.config.paths.root if _ctx is not None else None
    registry=get_registry(workspace)
    spec=registry.find(provider)
    if spec is None or spec.kind!='prediction_market':
        return {'ok':False,'error':'select a registered prediction-market provider'}
    if ':' in market and registry.find(market.split(':',1)[0]) is not spec:
        return {'ok':False,'error':'market and provider do not match'}
    connector=build_connector({'venue':spec.id,'live':False,'provider_config':payload.get('provider_config') or {}},workspace=workspace)
    if not all(callable(getattr(connector,name,None)) for name in ('list_markets','get_market_metadata','get_order_book')):
        return {'ok':False,'error':'prediction inspection capability unavailable'}
    if not market:
        return {'ok':True,'read_only':True,'markets':connector.list_markets(limit=min(100,max(1,int(payload.get('limit') or 20))),offset=max(0,int(payload.get('offset') or 0)))}
    meta=connector.get_market_metadata(market)
    market=meta['market']
    book=connector.get_order_book(market)
    depth=min(20,max(0,int(payload.get('depth',3))))
    book={**book,'bids':book.get('bids',[])[:depth],'asks':book.get('asks',[])[:depth]}
    metadata={key:meta.get(key) for key in ('asset_id','protocol_version','question','outcome','accepting_orders','settlement_status')}
    result={'ok':True,'read_only':True,'market':market,'token_id':meta['asset_id'],'metadata':metadata,'book':book,
            'notes':{**metadata, 'market':market, **{key:book.get(key) for key in (
                'bid','ask','min_order_size','size_step','quantity_unit','tick_size','ts_ms','neg_risk')}},
            'summary':f"{meta.get('question') or market} / {meta['outcome']}; bid={book.get('bid')}, ask={book.get('ask')}, min_shares={book.get('min_order_size')}, share_step={book.get('size_step')}, ts_ms={book.get('ts_ms')}"}
    if payload.get('history'):
        history=getattr(connector,'get_price_history',None)
        if not callable(history):
            return {'ok':False,'error':'prediction price-history capability unavailable'}
        result['history']=history(market,interval=str(payload.get('interval') or '1h'),limit=min(1000,max(1,int(payload.get('limit') or 24))),
            **{k:int(payload[k]) for k in ('start','end') if payload.get(k) is not None})
    return result


if __name__=='__main__':cli_main(run)
