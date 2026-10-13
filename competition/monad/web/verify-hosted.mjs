// Explicit unauthenticated HTTP acceptance; no browser, cookies or auth headers.
import {mkdir, writeFile, readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {snapshot} from '../core.mjs';
import {proxyTarget} from './transport.mjs';
import {compare} from './model.mjs';
const origin='https://nerya-monad-metropolis.vercel.app';
const dir=new URL('./validation/hosted/',import.meta.url);
await mkdir(dir,{recursive:true});
const report={testedAt:new Date().toISOString(),origin,authenticated:false,browserUsed:false,checks:[],snapshots:[],failures:[]};
const nativeFetch=globalThis.fetch.bind(globalThis);
async function check(name,path,options={},expected=200,inspect=()=>true) {
  try {
    const response=await nativeFetch(origin+path,{...options,credentials:'omit',redirect:'manual',signal:AbortSignal.timeout(25000)});
    const body=await response.text();
    const headers=Object.fromEntries(response.headers);
    await writeFile(new URL(name+'.body',dir),body);
    await writeFile(new URL(name+'.headers.json',dir),JSON.stringify(headers,null,2)+'\n');
    const pass=response.status === expected && Boolean(await inspect(body,headers));
    const result={name,path,method:options.method||'GET',status:response.status,expected,contentType:headers['content-type'],pass};
    report.checks.push(result);
    if (!pass) report.failures.push(name+': unexpected status or content');
    return {body,headers,status:response.status};
  } catch (error) {report.failures.push(name+': '+error.message); return null;}
}
// At most three independent requests at once.
async function batch(checks) {
  for(let i=0;i<checks.length;i+=3) await Promise.all(checks.slice(i,i+3).map(args=>check(...args)));
}
await check('root','/',{},200,(body,h)=>h['content-type']?.includes('text/html') && /^<!doctype html>/i.test(body.trim()) && body.includes('<title>Nerya · Monad risk lens</title>') && body.includes('id="notional"') && body.includes('value="300"') && body.includes('./app.mjs'));
const sha=body=>createHash('sha256').update(body).digest('hex');
const parentCore=await readFile(new URL('../core.mjs',import.meta.url),'utf8');
const assetChecks=[];
for(const name of ['index.html','styles.css','app.mjs','model.mjs','transport.mjs','config.mjs','core.mjs','build-manifest.json']) {
  const inspect=name === 'config.mjs' ? body=>body.includes("proxyBase: '/api'") : name === 'core.mjs' ? body=>sha(body) === sha(parentCore) : name.endsWith('.mjs') ? (body,h)=>h['content-type']?.includes('javascript') && !body.trim().startsWith('<') : name === 'styles.css' ? (body,h)=>h['content-type']?.includes('text/css') && body.includes('#8b5cf6') && body.includes('#22d3ee') : name === 'build-manifest.json' ? body=>JSON.parse(body).sharedCoreSha256 === sha(parentCore) : (body,h)=>h['content-type']?.includes('text/html') && body.includes('Nerya');
  assetChecks.push(['asset-'+name.replaceAll('.','-'),'/'+name,{},200,inspect]);
}
await batch(assetChecks);
const rpc={jsonrpc:'2.0',id:1,method:'eth_chainId',params:[]};
await batch([
  ['context','/api/context',{},200,body=>JSON.parse(body).chain.chain_id === 143],
  ['chain','/api/rpc',{method:'POST',headers:{'content-type':'application/json',Origin:origin},body:JSON.stringify(rpc)},200,body=>JSON.parse(body).result === '0x8f'],
  ['same-origin-preflight','/api/rpc',{method:'OPTIONS',headers:{Origin:origin,'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type'}},204,(_b,h)=>h['access-control-allow-origin'] === origin && h['access-control-allow-methods'] === 'POST, OPTIONS']
]);
const until=Math.floor(Date.now()/3600000)*3600000;
await check('candles-MON','/api/candles/10/candles/3600/'+(until-72*3600000)+'-'+(until-1),{},200,body=>{const d=JSON.parse(body).d; if (!Array.isArray(d)) return false; const closed=d.filter(row=>row.t < until); report.candleBoundary={returnedRows:d.length,closedRows:closed.length,excludedOpenRows:d.length-closed.length}; return closed.length > 0 && closed.every(row=>Number.isFinite(row.t) && Number.isFinite(row.c));});
await batch([
  ['forbidden-order','/api/order',{method:'POST'},404],
  ['forbidden-context-write','/api/context',{method:'POST'},405],
  ['forbidden-query','/api/context?url=https%3A%2F%2Fexample.com',{},400],
  ['forbidden-origin','/api/context',{headers:{Origin:'https://unapproved.example'}},403],
  ['forbidden-rpc-transaction','/api/rpc',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({...rpc,method:'eth_sendRawTransaction'})},400],
  ['forbidden-rpc-batch','/api/rpc',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify([rpc])},400],
  ['forbidden-rpc-params','/api/rpc',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({...rpc,params:['unexpected']})},400],
  ['forbidden-market','/api/candles/999/candles/3600/'+(until-72*3600000)+'-'+(until-1),{},404],
  ['forbidden-window','/api/candles/10/candles/3600/'+(until-73*3600000)+'-'+(until-1),{},400],
  ['forbidden-future','/api/candles/10/candles/3600/'+until+'-'+(until+72*3600000),{},400]
]);
// Real shared-core snapshots via the deployed allowlisted proxy.
try {
  globalThis.fetch=async(url,options)=>{
    const target=proxyTarget(String(url),origin+'/api');
    const response=await nativeFetch(target,{...options,credentials:'omit',redirect:'error'});
    const clone=response.clone();
    const name='snapshot-http-'+report.snapshots.length+'-'+(String(url).includes('context')?'context':String(url).includes('rpc.monad')?'rpc':'candles');
    await writeFile(new URL(name+'.json',dir),JSON.stringify({url:target,status:response.status,headers:Object.fromEntries(response.headers),body:await clone.text()},null,2));
    return response;
  };
  for (const symbol of ['MON','BTC','ETH']) {
    const data=await snapshot(symbol);
    const result=compare(data,{capital:1000,notional:300,adversePct:5,maxLossPct:2,slippageBps:10});
    await writeFile(new URL(symbol+'-snapshot.json',dir),JSON.stringify(data,null,2));
    report.snapshots.push({symbol,chainId:data.chainId,marketId:data.marketId,mark:data.mark,takerFeeBps:data.takerFeeBps,closedHours:data.closedHourlyCandles.length,observedAt:data.observedAt,sourceAgeSeconds:result.sourceAgeSeconds,baselineLoss:result.baseline.totalLoss,candidateLoss:result.candidate.totalLoss,baselineStatus:result.baseline.status,candidateStatus:result.candidate.status});
  }
} catch(error) { report.failures.push('shared snapshot: '+error.message); }
finally { globalThis.fetch=nativeFetch; }
report.finishedAt=new Date().toISOString();
report.passed=report.failures.length === 0;
await writeFile(new URL('report.json',dir),JSON.stringify(report,null,2)+'\n');
console.log(JSON.stringify(report,null,2));
if (!report.passed) process.exitCode=1;
