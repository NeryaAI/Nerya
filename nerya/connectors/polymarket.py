"""Polymarket v2 CLOB + Gamma connector.

Surfaces prediction-market orderbooks and candles as ordinary Nerya
:class:`Connector` reads so the rest of the stack (market_data skill,
agent loop) doesn't need to know Polymarket is special.

Endpoints used (public):

* ``https://clob.polymarket.com``          — CLOB v2 (orderbooks, orders)
* ``https://gamma-api.polymarket.com``     — Gamma (market metadata)

Writes use the pinned official polymarket-client signer and authenticated
requests. Signed order hashes are persisted before posting; neither private
keys nor signed payloads are written. Public metadata needs no credentials.

Markets use decimal CLOB outcome token ids or explicit Gamma slug#outcome.
``_resolve_token`` handles both — slugs get one metadata lookup and are
cached.
"""

from __future__ import annotations

import time
import json
import math
import hashlib
from pathlib import Path
from decimal import Decimal,ROUND_FLOOR,ROUND_CEILING
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import TradingError
from .base import Balance, CEXConnectorBase, OrderAck, Ticker
from .cex_base import CEXCredentials
from .http import HttpTransport, UrllibHttp


CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"
DATA_URL = 'https://data-api.polymarket.com'


def _sdk_module():
    try:
        from . import polymarket_client
        return polymarket_client
    except ImportError as exc:
        raise TradingError('polymarket signing requires polymarket-client==0.12.0') from exc


@dataclass
class PolymarketConnector(CEXConnectorBase):
    """Polymarket CLOB v2 connector.

    Private reads require credentials. Writes additionally require live=True;
    the execution engine checks runtime/account permission before dispatch.
    """

    venue: str = "POLYMARKET"
    kind: str = 'prediction_market'
    credentials: CEXCredentials = field(default_factory=CEXCredentials)
    live: bool = False
    transport: HttpTransport = field(default_factory=UrllibHttp)
    clob_url: str = CLOB_URL
    gamma_url: str = GAMMA_URL
    data_url: str = DATA_URL
    workspace: Path | None = None
    account_id: str = ''
    signature_type: int = 0
    funder: str = ''
    chain_id: int = 137
    max_slippage_bps: int = 50
    settlement_rpc_url: str = ''
    settlement_exchange_code_hashes: dict[str,str] = field(default_factory=dict)
    settlement_confirmations: int = 3
    _client: Any = field(default=None,repr=False)
    _submitted: dict = field(default_factory=dict,repr=False)
    _market_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    # --------------------------------------------------------- helpers
    def _get(
        self, base: str, path: str, *,
        params: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any] | list[Any]:
        url = base.rstrip("/") + "/" + path.lstrip("/")
        status, doc = self.transport.request(
            "GET", url, params=params or {}, timeout=timeout,
        )
        if status >= 400:
            raise TradingError(f"polymarket GET {path} {status}: {doc}")
        return doc

    def _resolve_token(self, market: str) -> tuple[str, dict[str, Any]]:
        """Return ``(token_id, market_meta)``.

        Accepts either a CLOB asset id (long decimal string), a
        explicit Gamma slug#outcome. Condition ids are not tradable assets.
        """
        m = market.split(":", 1)[-1].strip()
        if m.lower().startswith('0x'):
            raise TradingError('condition ID is not an outcome token ID; select a decimal CLOB token or slug#outcome')
        if _looks_like_token_id(m):
            return m, self._market_cache.get(m, {"token_id": m})
        if m in self._market_cache:
            meta = self._market_cache[m]
            return meta.get("token_id") or m, meta
        slug,sep,outcome=m.partition('#')
        if not sep or not outcome:raise TradingError('prediction slug requires explicit #outcome, e.g. #Yes or #No')
        meta = self._lookup_slug(slug,outcome)
        self._market_cache[m] = meta
        return meta["token_id"], meta

    def _lookup_slug(self, slug: str, outcome: str) -> dict[str, Any]:
        doc = self._get(self.gamma_url, "markets", params={"slug": slug})
        row = doc[0] if isinstance(doc, list) and doc else (doc if isinstance(doc, dict) else None)
        if not row:
            raise TradingError(f"polymarket: no market for slug {slug!r}")
        # Gamma returns `clobTokenIds` as a JSON string or list of 2 outcomes.
        raw_ids = row.get("clobTokenIds") or row.get("tokenIds")
        if isinstance(raw_ids, str):
            import json as _json
            try:
                raw_ids = _json.loads(raw_ids)
            except Exception:
                raw_ids = [raw_ids]
        if not raw_ids:
            raise TradingError(f"polymarket: slug {slug!r} has no clob token ids")
        outcomes=row.get('outcomes') or []
        if isinstance(outcomes,str):outcomes=json.loads(outcomes)
        indices=[i for i,name in enumerate(outcomes) if str(name).casefold()==outcome.casefold()]
        if len(indices)!=1 or indices[0]>=len(raw_ids):raise TradingError('unknown or ambiguous prediction outcome')
        token_id = str(raw_ids[indices[0]])
        if not token_id.isdigit():raise TradingError('invalid outcome token id')
        return {
            "token_id": token_id,
            "slug": slug,
            "question": row.get("question"),
            "end_date": row.get("endDate"),
            "outcomes": row.get("outcomes"),
            "clob_token_ids": raw_ids,
            'outcome':outcomes[indices[0]],
            'closed':bool(row.get('closed')),
            'accepting_orders':row.get('acceptingOrders',True),
        }

    # --------------------------------------------------------- public reads
    def get_ticker(self, market: str) -> Ticker:
        token_id, meta = self._resolve_token(market)
        book_raw = self._get(self.clob_url, "book", params={"token_id": token_id})
        bids = (book_raw or {}).get("bids") or []  # type: ignore[union-attr]
        asks = (book_raw or {}).get("asks") or []  # type: ignore[union-attr]
        bid_levels = _sorted_levels(bids, reverse=True)
        ask_levels = _sorted_levels(asks)
        bid = bid_levels[0][0] if bid_levels else 0.0
        ask = ask_levels[0][0] if ask_levels else 0.0
        mid = round((bid + ask) / 2, 12) if (bid and ask) else (bid or ask)
        last = _float_or_zero((book_raw or {}).get("last_trade_price")) or mid  # type: ignore[union-attr]
        if not 0<=last<=1:raise TradingError('invalid prediction price')
        spread_bps = ((ask - bid) / mid) * 10_000 if mid and bid and ask else 0.0
        return Ticker(
            market=market, bid=bid, ask=ask, mid=mid, last=last,
            spread_bps=spread_bps, ts_ms=_book_timestamp(book_raw),
            venue=self.venue,
        )

    def get_order_book(self, market: str) -> dict[str, Any]:
        token_id, _meta = self._resolve_token(market)
        book = self._get(self.clob_url, "book", params={"token_id": token_id})
        if not isinstance(book, dict):
            raise TradingError(f"polymarket book bad response for {market}")
        bids = _sorted_levels(book.get("bids") or [], reverse=True)
        asks = _sorted_levels(book.get("asks") or [])
        return {
            "market": market, "token_id": token_id,
            "bid": bids[0][0] if bids else 0.0,
            "ask": asks[0][0] if asks else 0.0,
            "bids": bids[:20], "asks": asks[:20],
            "venue": self.venue, "ts_ms": _book_timestamp(book),
        }

    def list_markets(self,*,limit=20,offset=0):
        rows=self._get(self.gamma_url,'markets',params={'active':'true','closed':'false','limit':min(100,max(1,int(limit))),'offset':int(offset)})
        if not isinstance(rows,list):raise TradingError('invalid prediction market discovery response')
        out=[]
        for row in rows:
            ids=row.get('clobTokenIds') or [];outcomes=row.get('outcomes') or []
            if isinstance(ids,str):ids=json.loads(ids)
            if isinstance(outcomes,str):outcomes=json.loads(outcomes)
            for token,outcome in zip(ids,outcomes):
                out.append({'market':'POLYMARKET:'+str(token),'token_id':str(token),'slug':row.get('slug'),
                            'outcome':outcome,'question':row.get('question'),'accepting_orders':row.get('acceptingOrders',False)})
        return out

    def get_klines(
        self, market: str, *, interval: str = "1h", limit: int = 100,
        since: int | None = None,end: int | None = None,
    ) -> list[list[Any]]:
        """Polymarket CLOB price history -> ccxt-style OHLCV rows.

        The API provides price samples, not traded OHLCV. Each row has
        O=H=L=C and zero unavailable volume; the data facade labels this.
        """
        token_id, _meta = self._resolve_token(market)
        seconds=_interval_seconds(interval)
        end_s=int(end)//1000 if end is not None else int(time.time())
        start_s=int(since)//1000 if since is not None else end_s-seconds*min(max(1,int(limit)),10000)
        params = {"market": token_id, "startTs":start_s,"endTs":end_s,"fidelity":max(1,seconds//60)}
        doc = self._get_price_history(params)
        rows = doc.get("history") if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            return []
        samples={}
        for row in rows:
            ts_ms = int(row.get("t") or row.get("timestamp") or 0)
            if ts_ms and ts_ms < 10**12:
                ts_ms *= 1000
            price = float(row.get("p") or row.get("price") or 0.0)
            if start_s*1000<=ts_ms<=end_s*1000 and math.isfinite(price) and 0<=price<=1:
                samples[ts_ms]=[ts_ms,price,price,price,price,0.0]
        return [samples[k] for k in sorted(samples)][-limit:]

    def _get_price_history(self, params: dict[str, Any]) -> dict[str, Any] | list[Any]:
        try:
            return self._get(self.clob_url, "prices-history", params=params)
        except TradingError as primary_error:
            if self.data_url.rstrip("/") == self.clob_url.rstrip("/"):
                raise
            try:
                return self._get(self.data_url, "prices-history", params=params)
            except TradingError:
                raise primary_error

    def _sdk(self):
        if self._client is None:
            sdk=_sdk_module()
            creds=self.credentials
            if not all((creds.api_key,creds.api_secret,creds.api_passphrase,creds.extras.get('privateKey'))):
                raise TradingError('polymarket needs CLOB API key/secret/passphrase and a separate private_key signer')
            if self.chain_id!=137:raise TradingError('polymarket production chain_id must be Polygon 137')
            if self.signature_type not in (0,1,2,3):raise TradingError('invalid polymarket signature_type')
            if self.signature_type!=0 and not self.funder:raise TradingError('proxy/Safe signing needs funder address')
            if self.signature_type==0 and self.funder:
                from eth_account import Account
                if Account.from_key(creds.extras['privateKey']).address.lower()!=self.funder.lower():
                    raise TradingError('EOA funder differs from signing wallet; configure correct signature type')
            self._client=sdk.ClobClient(host=self.clob_url,chain_id=self.chain_id,key=creds.extras['privateKey'],
                creds=sdk.ApiCreds(api_key=creds.api_key,api_secret=creds.api_secret,api_passphrase=creds.api_passphrase),
                signature_type=self.signature_type,funder=self.funder or None,retry_on_error=False)
        return self._client

    def _require_live(self):
        if not self.live:raise TradingError('polymarket live trading disabled')

    def _state_path(self,client_id):
        if not self.workspace:return None
        key=hashlib.sha256((self.account_id+':'+self.funder+':'+self.credentials.api_key+':'+client_id).encode()).hexdigest()
        return Path(self.workspace)/'state'/'prediction_orders'/(key+'.json')

    def _load_send(self,client_id):
        path=self._state_path(client_id)
        return json.loads(path.read_text()) if path and path.exists() else self._submitted.get(client_id)

    def _save_send(self,client_id,record):
        self._submitted[client_id]=record
        path=self._state_path(client_id)
        if path:
            from ..core.atomic_write import atomic_write_text
            atomic_write_text(path,json.dumps(record))

    def _records(self):
        records=dict(self._submitted)
        root=Path(self.workspace)/'state'/'prediction_orders' if self.workspace else None
        if root and root.exists():
            for path in root.glob('*.json'):
                row=json.loads(path.read_text())
                cid=row.get('client_order_id','')
                if cid and self._state_path(cid)==path:records[cid]=row
        return records

    def _signed_order_hash(self,signed,neg_risk):
        return self._sdk().signed_order_hash(signed)

    def get_balances(self):
        sdk=_sdk_module()
        raw=self._sdk().get_balance_allowance(sdk.BalanceAllowanceParams(asset_type=sdk.AssetType.COLLATERAL))
        total=_positive_number(raw.get('balance'),allow_zero=True)/1_000_000
        # CLOB balance is collateral holdings; resting bids reserve collateral.
        locked=sum(max(0,float(o.size or 0)-float((o.raw or {}).get('matched_size') or 0))*float(o.price or 0) for o in self.fetch_open_orders() if o.side=='buy')
        return [Balance(asset='PUSD',free=max(0,total-locked),locked=min(total,locked),total=total)]

    def get_positions_value(self):
        from .prediction_data import portfolio_value
        owner=self.funder or self._sdk().signer_address
        return portfolio_value(self._get, self.data_url, owner)

    def get_positions(self, *, status='OPEN', max_pages=100):
        from .prediction_data import positions
        owner = self.funder or self._sdk().signer_address
        return positions(self._get, self.data_url, owner, status=status, max_pages=max_pages)

    def place_order(self,*,market,side,order_type,size,price=None,client_order_id=None,time_in_force='GTC',
                    reduce_only=False,leverage=None,margin_mode=None,position_side=None,position_idx=None,
                    stop_loss=None,take_profit=None,trigger_price=None,extra_params=None,reference_price=None):
        self._require_live()
        if stop_loss is not None or take_profit is not None or trigger_price is not None:
            raise TradingError('Polymarket native TP/SL and trigger orders are unsupported')
        if leverage not in (None,1,1.0) or margin_mode or position_side or position_idx is not None:
            raise TradingError('Polymarket outcome shares do not support leverage or margin selectors')
        if order_type not in ('market','limit'):raise TradingError('unsupported prediction order type')
        if side.lower() not in ('buy','sell'):raise TradingError('invalid prediction side')
        side=side.lower();size=_positive_number(size)
        if Decimal(str(size)).quantize(Decimal('.01'),rounding=ROUND_FLOOR)!=Decimal(str(size)):
            raise TradingError('Polymarket share size requires at most 2 decimal places')
        cid=str(client_order_id or '')
        if not cid or self.workspace is None:raise TradingError('prediction execution requires workspace and client_order_id for durable recovery')
        from ..trading.locks import trading_lock
        from ..core.paths import WorkspacePaths
        with trading_lock(WorkspacePaths(Path(self.workspace)),'prediction:'+self.account_id+':'+cid) as acquired:
            if not acquired:raise TradingError('prediction submission in progress',ambiguous=True)
            return self._place_locked(market,side,order_type,size,price,cid,time_in_force,reduce_only,extra_params,reference_price)

    def _place_locked(self,market,side,order_type,size,price,cid,time_in_force,reduce_only,extra_params,reference_price):
        token,meta=self._resolve_token(market)
        spec=dict(market=market,token=token,side=side,size=size,price=price,order_type=order_type,time_in_force=time_in_force,
                  reduce_only=reduce_only,extra_params=extra_params or {},reference_price=reference_price)
        prior=self._load_send(cid)
        if prior:
            if prior.get('spec')!=spec:raise TradingError('client_order_id reused for different prediction order')
            if prior.get('status')=='rejected':
                return OrderAck(order_id=prior['order_id'],client_order_id=cid,status='rejected',market=market,side=side,size=size,filled=0)
            return self.get_order(market=market,order_id=prior['order_id'])
        if meta.get('closed') or meta.get('accepting_orders') is False:raise TradingError('prediction market is closed')
        if reduce_only and side!='sell':raise TradingError('reduce-only prediction order must sell owned outcome shares')
        sdk=_sdk_module();client=self._sdk()
        params=dict(extra_params or {})
        if set(params)-{'expiration','slippage_bps'}:raise TradingError('unsupported prediction connector parameters')
        tif=str(time_in_force).upper();post_only=tif in ('POST_ONLY','PO')
        if tif not in ('GTC','GTD','IOC','FOK','POST_ONLY','PO'):raise TradingError('unsupported prediction time_in_force')
        book=self._get(self.clob_url,'book',params={'token_id':token})
        if not isinstance(book,dict):raise TradingError('invalid prediction book')
        if 'tick_size' not in book or 'min_order_size' not in book or 'neg_risk' not in book:
            raise TradingError('prediction market tick/minimum/negative-risk metadata unavailable')
        if time.time()*1000-_book_timestamp(book)>120000:raise TradingError('prediction order book is stale')
        tick=Decimal(str(book['tick_size']))
        if not tick.is_finite() or tick<=0 or tick>=1:raise TradingError('invalid prediction tick size')
        minimum=_positive_number(book['min_order_size'],allow_zero=True)
        if size<minimum:raise TradingError('prediction size below market minimum')
        neg_risk=bool(book.get('neg_risk',False))
        if order_type=='market':
            if post_only or tif=='GTD':raise TradingError('market order cannot be post-only or GTD')
            levels=_sorted_levels(book.get('asks' if side=='buy' else 'bids') or [],reverse=side=='sell')
            remaining=size;worst=0
            for px,qty in levels:
                remaining-=qty;worst=px
                if remaining<=1e-12:break
            if remaining>1e-12:raise TradingError('insufficient prediction book liquidity for requested shares')
            slip=int(params.get('slippage_bps',self.max_slippage_bps))
            if not 0<=slip<=self.max_slippage_bps:raise TradingError('prediction slippage exceeds configured cap')
            ref=_positive_number(reference_price if reference_price is not None else levels[0][0])
            bound=Decimal(str(ref))*Decimal(str(1+slip/10000 if side=='buy' else 1-slip/10000))
            bound=(bound/tick).to_integral_value(rounding=ROUND_FLOOR if side=='buy' else ROUND_CEILING)*tick
            price=float(min(Decimal(1)-tick,max(tick,bound)))
            if (side=='buy' and worst>price) or (side=='sell' and worst<price):raise TradingError('prediction market price exceeds slippage limit')
            venue_tif='FOK'  # marketable bounded order in shares; no USD/share confusion
        else:
            price=_positive_number(price)
            venue_tif='FAK' if tif=='IOC' else 'GTC' if post_only else tif
        px=Decimal(str(price))
        if not tick<=px<=Decimal(1)-tick or px%tick:raise TradingError('prediction price outside probability/tick range')
        expiration=int(params.get('expiration') or 0)
        if venue_tif=='GTD' and expiration<time.time()+180:
            raise TradingError('GTD expiration must be at least 180 seconds in the future (including the 60 second safety window)')
        if expiration and venue_tif!='GTD':raise TradingError('expiration only applies to GTD')
        if side=='sell':
            balance=client.get_balance_allowance(sdk.BalanceAllowanceParams(asset_type=sdk.AssetType.CONDITIONAL,token_id=token))
            available=_positive_number(balance.get('balance'),allow_zero=True)/1_000_000
            resting=sum(max(0,float(o.size or 0)-float((o.raw or {}).get('matched_size') or 0)) for o in self.fetch_open_orders(symbols=[market]) if o.side=='sell')
            if size>max(0,available-resting)+1e-9:raise TradingError('sell exceeds unreserved owned outcome shares')
        signed=client.create_order(sdk.OrderArgs(token_id=token,price=price,size=size,side=side.upper(),expiration=expiration),
                                   sdk.PartialCreateOrderOptions(tick_size=str(tick),neg_risk=neg_risk))
        oid=self._signed_order_hash(signed,neg_risk)
        record=dict(client_order_id=cid,order_id=oid,spec=spec,status='submitting',ts=time.time())
        self._save_send(cid,record)  # no signature/private bytes persisted
        try:doc=client.post_order(signed,order_type=venue_tif,post_only=post_only)
        except Exception as exc:raise TradingError('Polymarket post outcome unknown; query persisted order hash',ambiguous=True) from exc
        if not isinstance(doc,dict):raise TradingError('invalid Polymarket post response',ambiguous=True)
        if doc.get('success') is False or doc.get('errorMsg'):
            self._save_send(cid,{**record,'status':'rejected'})
            raise TradingError('Polymarket rejected order: '+str(doc.get('errorMsg') or 'unknown'))
        returned=str(doc.get('orderID') or doc.get('id') or '')
        if not returned or returned!=oid:raise TradingError('Polymarket returned missing/mismatched order hash',ambiguous=True)
        self._save_send(cid,{**record,'status':'submitted'})
        # Matching is not settled execution. The regular poller obtains trades.
        return OrderAck(order_id=oid,client_order_id=cid,status='new',market=market,side=side,price=price,size=size,filled=0,raw=doc)

    def get_order(self,*,market,order_id,query_params=None):
        token,_=self._resolve_token(market)
        prior=self._load_send(str((query_params or {}).get('clientOrderId') or order_id))
        oid=prior['order_id'] if prior else order_id
        client=self._sdk()
        try:doc=client.get_order(oid)
        except Exception as exc:
            code=getattr(exc,'status_code',None)
            if code==404:raise TradingError('prediction order not found',not_found=True) from exc
            raise
        if not isinstance(doc,dict):raise TradingError('invalid prediction order response')
        if doc.get('asset_id') and str(doc['asset_id'])!=token:raise TradingError('prediction order outcome mismatch')
        records=self._records();record=next((r for r in records.values() if r['order_id']==oid),{})
        cid=record.get('client_order_id','');spec=record.get('spec') or {}
        side=str(doc.get('side') or spec.get('side') or '').lower()
        total=_positive_number(doc.get('original_size',spec.get('size',0)),allow_zero=True)
        matched=_positive_number(doc.get('size_matched',0),allow_zero=True)
        filled=cost=0.;seen=set();pending=False;settlements=[]
        sdk=_sdk_module()
        for trade_id in doc.get('associate_trades') or []:
            trades=client.get_trades(sdk.TradeParams(id=trade_id)) or []
            for trade in trades:
                tid=str(trade.get('id') or '')
                if not tid or tid in seen:continue
                seen.add(tid)
                settled=str(trade.get('status')).upper()=='CONFIRMED' and bool(trade.get('transaction_hash'))
                pieces=[]
                if str(trade.get('taker_order_id'))==oid and str(trade.get('asset_id'))==token:pieces=[trade]
                else:pieces=[x for x in trade.get('maker_orders') or [] if str(x.get('order_id'))==oid and str(x.get('asset_id',token))==token]
                if pieces and not settled:
                    if str(trade.get('status')).upper()!='FAILED':pending=True
                    continue
                for fill in pieces:
                    qty=_positive_number(fill.get('matched_amount',fill.get('size',0)),allow_zero=True)
                    px=_positive_number(fill.get('price'))
                    filled+=qty;cost+=qty*px
                    settlements.append(str(trade['transaction_hash']))
        fee_evidence={'status':'unverified'}
        fee_value=None
        if filled and self.settlement_rpc_url:
            from .prediction_fees import confirmed_order_fees
            from .evm_native import EVMNative
            chain=EVMNative(chain='polygon',chain_id=137,rpc_url=self.settlement_rpc_url,transport=self.transport)
            fee_evidence=confirmed_order_fees(chain._rpc,transactions=settlements,order_hash=oid,
                owner=self.funder or client.signer_address,token_id=token,side=side,filled_shares=filled,
                exchange_code_hashes=self.settlement_exchange_code_hashes,confirmations=self.settlement_confirmations)
            fee_value=float(fee_evidence['fee_collateral'])
            cost=float(fee_evidence['cumulative_notional'])
        status=_map_pm_status(doc.get('status'))
        if filled>total+1e-8:raise TradingError('prediction confirmed fills exceed order size')
        if pending or matched>filled+1e-8:status='partial' if filled else 'new'
        elif filled>=total>0:status='filled'
        elif status not in ('canceled','rejected') and filled:status='partial'
        elif status=='filled':status='new'  # no settled trade evidence yet
        return OrderAck(order_id=oid,client_order_id=cid,status=status,market=market,side=side,
            price=_float_or_zero(doc.get('price')) or None,size=total,filled=filled,avg_price=cost/filled if filled else None,
            fee_usd=fee_value,
            raw={'venue_status':doc.get('status'),'matched_unsettled':max(0,matched-filled),'fee_status':fee_evidence['status'],
                 'fee_evidence':fee_evidence,
                 'cumulative_notional':cost,'prediction_market':True})

    def fetch_open_orders(self,*,symbols=None):
        rows=self._sdk().get_open_orders() or []
        allowed={self._resolve_token(x)[0] for x in symbols} if symbols else None
        records={r['order_id']:r for r in self._records().values()}
        out=[]
        for row in rows:
            token=str(row.get('asset_id') or '')
            if allowed is not None and token not in allowed:continue
            r=records.get(str(row.get('id')),{});spec=r.get('spec') or {}
            out.append(OrderAck(order_id=str(row['id']),client_order_id=r.get('client_order_id',''),status='new',
                market=spec.get('market') or 'POLYMARKET:'+token,side=str(row.get('side') or '').lower(),
                price=float(row.get('price') or 0),size=float(row.get('original_size') or 0),filled=0,
                raw={'matched_size':float(row.get('size_matched') or 0)}))
        return out

    def get_order_by_client_id(self,*,market,client_order_id):
        row=self._load_send(client_order_id)
        if not row:raise TradingError('prediction client id not recorded',ambiguous=True)
        if row.get('status')=='rejected':
            return OrderAck(order_id=row['order_id'],client_order_id=client_order_id,status='rejected',market=market,
                side=(row.get('spec') or {}).get('side',''),filled=0)
        return self.get_order(market=market,order_id=row['order_id'])

    def cancel_order(self,*,market,order_id):
        self._require_live()
        token,_=self._resolve_token(market)
        current=self.get_order(market=market,order_id=order_id)
        if current.status in ('filled','canceled','rejected'):return current
        doc=self._sdk().cancel_order(_sdk_module().OrderPayload(orderID=order_id))
        if not isinstance(doc,dict) or order_id not in (doc.get('canceled') or []):
            raise TradingError('Polymarket cancel was not confirmed; continue order polling')
        # A final fill may race cancellation; only a fresh order/trade query
        # can safely retire the tracked order and release its reservation.
        return self.get_order(market=market,order_id=order_id)

def _interval_seconds(interval):
    units={'m':60,'h':3600,'d':86400,'w':604800}
    try:
        value=int(interval[:-1])*units[interval[-1]]
    except (ValueError,KeyError,IndexError):raise TradingError('unsupported prediction interval')
    if value<=0:raise TradingError('unsupported prediction interval')
    return value


def _positive_number(value,allow_zero=False):
    try:n=float(value)
    except (TypeError,ValueError) as exc:raise TradingError('invalid prediction numeric value') from exc
    if not math.isfinite(n) or n<0 or (n==0 and not allow_zero):raise TradingError('invalid prediction amount/price')
    return n


def _book_timestamp(book):
    raw=book.get('timestamp')
    if raw is None:return int(time.time()*1000)
    ts=int(raw)
    return ts*1000 if ts<10**12 else ts


def _looks_like_token_id(value: str) -> bool:
    v = value.strip()
    return v.isdigit() and len(v) >= 20


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sorted_levels(rows: list[Any], *, reverse: bool = False) -> list[list[float]]:
    levels: list[list[float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        price = _float_or_zero(row.get("price"))
        size = _float_or_zero(row.get("size"))
        if not math.isfinite(price) or not math.isfinite(size) or price <= 0 or price>1 or size <= 0:
            continue
        levels.append([price, size])
    return sorted(levels, key=lambda item: item[0], reverse=reverse)


def _map_pm_status(raw: Any) -> str:
    s = str(raw or "").lower()
    if s in ("live", "open", "resting"):
        return "new"
    if s in ("filled", "matched"):
        return "filled"
    if s in ("partial", "partiallyfilled", "partially_filled"):
        return "partial"
    if s in ("cancelled", "canceled"):
        return "canceled"
    if s in ('rejected','invalid'):
        return "rejected"
    if s in ('expired','unmatched'):return 'canceled'
    return s or "new"


__all__ = ["PolymarketConnector"]
