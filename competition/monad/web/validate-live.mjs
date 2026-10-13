// Explicit network validation; never invoked by the offline test command.
import {mkdir,writeFile} from 'node:fs/promises';
import {once} from 'node:events';
import {snapshot,CONTEXT_URL,RPC_URL} from '../core.mjs';
import {createPublicServer} from './server.mjs';
import {proxyTarget} from './transport.mjs';
import {compare} from './model.mjs';
const origin='https://nerya-judge.github.io';
const report={testedAt:new Date().toISOString(),origin,browserUsed:false,cors:[],snapshots:[],failures:[]};
const nativeFetch=globalThis.fetch.bind(globalThis);
const dir=new URL('./validation/',import.meta.url);
await mkdir(dir,{recursive:true});
async function cors(name,url,options={}) {
  try {
    const r=await nativeFetch(url,{...options,headers:{Origin:origin,...options.headers},signal:AbortSignal.timeout(20000)});
    const raw=await r.text();
    const entry={name,url,method:options.method || 'GET',status:r.status,allowOrigin:r.headers.get('access-control-allow-origin'),allowMethods:r.headers.get('access-control-allow-methods'),allowHeaders:r.headers.get('access-control-allow-headers')};
    entry.originAllowed=entry.allowOrigin === origin || entry.allowOrigin === '*';
    report.cors.push(entry);
    await writeFile(new URL(name+'-http.json',dir),JSON.stringify({...entry,body:raw},null,2));
  } catch (error) { report.failures.push(name+': '+error.message); }
}
await cors('perpl-context',CONTEXT_URL);
const until=Math.floor(Date.now()/3600000)*3600000;
await cors('perpl-candles','https://app.perpl.xyz/api/v1/market-data/10/candles/3600/'+(until-72*3600000)+'-'+(until-1));
await cors('monad-preflight',RPC_URL,{method:'OPTIONS',headers:{'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type'}});
await cors('monad-chain',RPC_URL,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({jsonrpc:'2.0',id:1,method:'eth_chainId',params:[]})});
const server=createPublicServer({allowedOrigins:[origin]}); server.listen(0,'127.0.0.1'); await once(server,'listening');
const base='http://127.0.0.1:'+server.address().port;
try {
  const html=await nativeFetch(base+'/');
  const content=await html.text();
  report.localStatic={status:html.status,proxyConfigured:content.includes('name="nerya-proxy-base" content="/api"')};
  if (html.status!==200 || !report.localStatic.proxyConfigured) throw new Error('Local static configuration failed');
  await cors('local-proxy-context',base+'/api/context');
  await cors('local-proxy-preflight',base+'/api/rpc',{method:'OPTIONS',headers:{'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type'}});
  // Exercise unchanged shared snapshot() over the actual local HTTP proxy.
  globalThis.fetch=(url,options)=>nativeFetch(proxyTarget(String(url),base+'/api'),options);
  for (const symbol of ['MON','BTC','ETH']) {
    const data=await snapshot(symbol);
    const result=compare(data,{capital:1000,notional:300,adversePct:5,maxLossPct:2,slippageBps:10});
    await writeFile(new URL(symbol+'-snapshot.json',dir),JSON.stringify(data,null,2));
    report.snapshots.push({symbol,chainId:data.chainId,mark:data.mark,takerFeeBps:data.takerFeeBps,closedHours:data.closedHourlyCandles.length,observedAt:data.observedAt,sourceAgeSeconds:result.sourceAgeSeconds,baselineLoss:result.baseline.totalLoss,candidateLoss:result.candidate.totalLoss,baselineStatus:result.baseline.status,candidateStatus:result.candidate.status});
  }
} catch (error) { report.failures.push(error.message); }
finally { globalThis.fetch=nativeFetch; server.closeAllConnections(); await new Promise(resolve=>server.close(resolve)); }
await writeFile(new URL('report.json',dir),JSON.stringify(report,null,2)+'\n');
console.log(JSON.stringify(report,null,2));
if (report.failures.length) process.exitCode=1;
