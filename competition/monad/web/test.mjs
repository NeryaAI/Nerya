import test from 'node:test';
import assert from 'node:assert/strict';
import {once} from 'node:events';
import {readFile} from 'node:fs/promises';
import {scenario} from '../core.mjs';
import {parseInputs,compare} from './model.mjs';
import {proxyTarget} from './transport.mjs';
import {createPublicServer} from './server.mjs';
const inputs={capital:1000,notional:300,adversePct:5,maxLossPct:2,slippageBps:10};
test('shared-core arithmetic: 3.45 bps gives exactly 15.807 on 300',()=>{
  // Mathematical inputs only, never served to the UI as market data.
  const source={sourceAgeSeconds:0,takerFeeBps:3.45};
  const result=scenario(source,inputs);
  assert.ok(Math.abs(result.totalLoss-15.807)<1e-10);
  assert.ok(Math.abs(scenario(source,{...inputs,notional:1000}).totalLoss-52.69)<1e-10);
  assert.equal(result.status,'Within scenario budget');
});
test('comparison uses shared core and ages both columns',()=>{
  const now=Date.now();
  const data={observedAt:new Date(now).toISOString(),takerFeeBps:3.45};
  const fresh=compare(data,inputs,now);
  assert.equal(fresh.baseline.status,'Review required');
  assert.equal(fresh.candidate.status,'Within scenario budget');
  assert.ok(Math.abs(fresh.reduction-36.883)<1e-10);
  const stale=compare(data,inputs,now+121000);
  assert.equal(stale.candidate.status,'Review required');
  assert.ok(stale.candidate.reasons.includes('Market observation is stale'));
  assert.equal(compare(data,inputs,now-11000).candidate.status,'Review required');
});
test('input validation rejects blanks, non-finite values and out-of-range assumptions',()=>{
  assert.deepEqual(parseInputs(inputs),inputs);
  for(const change of [{notional:''},{capital:' '},{capital:0},{notional:1001},{adversePct:101},{maxLossPct:0},{slippageBps:-1},{capital:Infinity}]) assert.throws(()=>parseInputs({...inputs,...change}));
  assert.equal(parseInputs({...inputs,notional:0}).notional,0);
});
test('transport maps only fixed public sources',()=>{
  assert.equal(proxyTarget('https://app.perpl.xyz/api/v1/pub/context','/api/'),'/api/context');
  assert.equal(proxyTarget('https://rpc.monad.xyz','/api'),'/api/rpc');
  assert.equal(proxyTarget('https://app.perpl.xyz/api/v1/market-data/10/candles/3600/1791000000000-1791003600000','/api'),'/api/candles/10/candles/3600/1791000000000-1791003600000');
  assert.throws(()=>proxyTarget('https://evil.example/','/api'));
  assert.throws(()=>proxyTarget('https://app.perpl.xyz/api/v1/pub/context?url=https://evil.example','/api'));
});
test('local HTTP proxy rejects execution, arbitrary paths and unauthorized origins without upstream calls',async()=>{
  const server=createPublicServer({allowedOrigins:['https://judge.example']});
  server.listen(0,'127.0.0.1'); await once(server,'listening');
  const base='http://127.0.0.1:'+server.address().port;
  try{
    const cases=[['/api/order',{method:'POST'},404],['/api/context',{method:'POST'},405],['/api/context?url=foo',{},400],['/api/context',{headers:{Origin:'https://evil.example'}},403],['/api/rpc',{method:'POST',body:JSON.stringify({jsonrpc:'2.0',id:1,method:'eth_sendRawTransaction',params:[]})},400],['/api/rpc',{method:'POST',body:'{}'},400],['/api/rpc',{method:'POST',body:'['},400],['/api/rpc',{method:'POST',body:'x'.repeat(1025)},413],['/api/candles/999/candles/3600/1791000000000-1791003600000',{},404],['/api/candles/10/candles/3600/1791000000000-1791003600000',{},400],['/server.mjs',{},404],['/../package.json',{},404]];
    for(const [path,options,code] of cases) assert.equal((await fetch(base+path,options)).status,code,path);
    const preflight=await fetch(base+'/api/rpc',{method:'OPTIONS',headers:{Origin:'https://judge.example','Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type'}});
    assert.equal(preflight.status,204);
    assert.equal(preflight.headers.get('access-control-allow-origin'),'https://judge.example');
    assert.equal(preflight.headers.get('access-control-allow-methods'),'POST, OPTIONS');
    assert.equal(preflight.headers.get('access-control-allow-credentials'),null);
  }finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});
test('static artifact is self-contained and packages shared core verbatim',async()=>{
  const original=await readFile(new URL('../core.mjs',import.meta.url));
  assert.deepEqual(await readFile(new URL('./dist/core.mjs',import.meta.url)),original);
  const html=await readFile(new URL('./dist/index.html',import.meta.url),'utf8');
  const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
  assert.equal(new Set(ids).size,ids.length);
  const app=await readFile(new URL('./dist/app.mjs',import.meta.url),'utf8');
  for(const match of app.matchAll(/\$\('([^']+)'\)/g)) assert.ok(ids.includes(match[1]),'DOM id exists: '+match[1]);
  for(const file of ['app.mjs','model.mjs','transport.mjs']) {
    const source=await readFile(new URL('./dist/'+file,import.meta.url),'utf8');
    assert.ok(!source.includes("from '../core.mjs'"));
    assert.ok(!source.includes('innerHTML'));
  }
});
test('same-host HTTPS origin works behind TLS termination',async()=>{
  const server=createPublicServer(); server.listen(0,'127.0.0.1'); await once(server,'listening');
  const host='127.0.0.1:'+server.address().port;
  try {
    const r=await fetch('http://'+host+'/api/rpc',{method:'OPTIONS',headers:{Origin:'https://'+host}});
    assert.equal(r.status,204); assert.equal(r.headers.get('access-control-allow-origin'),'https://'+host);
  } finally {server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});
test('Vercel adapter rejects extra queries and applies shared allowlist',async()=>{
  const {default:handler}=await import('./api/public.mjs');
  const {createServer}=await import('node:http');
  const server=createServer(handler); server.listen(0,'127.0.0.1'); await once(server,'listening');
  const base='http://127.0.0.1:'+server.address().port;
  try {
    assert.equal((await fetch(base+'/api/public?route=rpc',{method:'OPTIONS',headers:{Origin:base.replace('http:','https:')}})).status,204);
    assert.equal((await fetch(base+'/api/public?route=order',{method:'POST'})).status,404);
    assert.equal((await fetch(base+'/api/public?route=rpc&url=foo')).status,400);
    assert.equal((await fetch(base+'/api/public?route=rpc',{method:'POST',body:JSON.stringify({jsonrpc:'2.0',id:1,method:'eth_sendRawTransaction',params:[]})})).status,400);
  } finally {server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});
