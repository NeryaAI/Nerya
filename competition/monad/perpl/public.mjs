// Read-only testnet extension. Mainnet reuses the existing core verbatim.
import {snapshot as mainnet} from '../core.mjs';
export const NETWORKS = {
  143: {api: 'https://app.perpl.xyz/api', rpc: 'https://rpc.monad.xyz'},
  10143: {api: 'https://testnet.perpl.xyz/api', rpc: 'https://testnet-rpc.monad.xyz'},
};
async function read(url, options={}) {
  const response = await fetch(url, {...options, signal: AbortSignal.timeout(15000)});
  if (!response.ok) throw new Error('Perpl public HTTP '+response.status);
  return response.json();
}
export async function snapshot(chainId=10143, symbol='MON') {
  const network = NETWORKS[chainId];
  if (!network || symbol !== 'MON') throw new Error('Supported MON network required');
  if (chainId === 143) return mainnet(symbol);
  const [context, rpc] = await Promise.all([
    read(network.api+'/v1/pub/context'),
    read(network.rpc, {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({jsonrpc:'2.0',id:1,method:'eth_chainId',params:[]})}),
  ]);
  if (context.chain?.chain_id !== chainId || Number.parseInt(rpc.result,16) !== chainId) throw new Error('Perpl API/RPC chain identity mismatch');
  const market = context.markets.find(m => m.name.replace(/ Perp$/, '')===symbol || m.size_units===symbol);
  if (!market || !market.config?.is_open || !market.state?.at?.t) throw new Error('MON market unavailable');
  const now=Date.now(), until=Math.floor(now/3600000)*3600000;
  const doc=await read(network.api+'/v1/market-data/'+market.id+'/candles/3600/'+(until-72*3600000)+'-'+(until-1));
  const price = value => value/10**market.config.price_decimals;
  const candles=(doc.d||[]).filter(c=>c.t<until).map(c=>({time:c.t,open:price(c.o),high:price(c.h),low:price(c.l),close:price(c.c),trades:c.n}));
  return {chainId,network:'Monad testnet',venue:'Perpl',symbol,marketId:market.id,instanceId:market.instance_id,
    fetchedAt:new Date(now).toISOString(),observedAt:new Date(market.state.at.t).toISOString(),blockNumber:market.state.at.b,
    sourceAgeSeconds:(now-market.state.at.t)/1000,mark:price(market.state.mrk),bid:price(market.state.bid),ask:price(market.state.ask),
    takerFeeBps:market.config.taker_fee/100,makerFeeBps:market.config.maker_fee/100,
    closedHourlyCandles:candles,latestClosedHour:candles.at(-1)?.time||null,
    provenance:{context:network.api+'/v1/pub/context',rpc:network.rpc,priceDecimals:market.config.price_decimals,feeScale:'Micros / 100 = bps'},
    scope:'Public testnet observation; no authenticated account or executed trade'};
}
