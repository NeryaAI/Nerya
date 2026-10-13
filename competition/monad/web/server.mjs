import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {CONTEXT_URL, RPC_URL} from './core.mjs';
const upstreamFetch = globalThis.fetch.bind(globalThis);
const types = {'.html':'text/html; charset=utf-8','.mjs':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.json':'application/json; charset=utf-8'};
const assets = new Set(['index.html','styles.css','app.mjs','model.mjs','transport.mjs','config.mjs','core.mjs','build-manifest.json']);
const publicRpc = {jsonrpc:'2.0',id:1,method:'eth_chainId',params:[]};
function send(res, code, value, type='application/json; charset=utf-8') {
  res.writeHead(code, {'content-type':type,'cache-control':'no-store','x-content-type-options':'nosniff'});
  res.end(typeof value === 'string' || Buffer.isBuffer(value) ? value : JSON.stringify(value));
}
async function body(req) {
  if (req.body !== undefined) {
    const raw = typeof req.body === 'string' ? req.body : JSON.stringify(req.body);
    if (Buffer.byteLength(raw) > 1024) throw Object.assign(new Error('Request body too large'), {status:413});
    try { return JSON.parse(raw); } catch { throw Object.assign(new Error('Invalid JSON'), {status:400}); }
  }
  let raw = '';
  for await (const chunk of req) {
    raw += chunk;
    if (Buffer.byteLength(raw) > 1024) throw Object.assign(new Error('Request body too large'),{status:413});
  }
  try { return JSON.parse(raw); } catch { throw Object.assign(new Error('Invalid JSON'),{status:400}); }
}
export function createPublicHandler({allowedOrigins=[]} = {}) {
  const allowed = new Set(allowedOrigins);
  return async (req,res)=>{
    try {
      const url = new URL(req.url,'http://localhost');
      const api = url.pathname.startsWith('/api/');
      if (api) {
        const origin = req.headers.origin;
        // No credentialed CORS. Permit explicitly configured public origins,
        // plus this server's own origin for local browser requests.
        const sameHost = origin && ['http:', 'https:'].includes(new URL(origin).protocol) && new URL(origin).host === req.headers.host;
        if (origin && !sameHost && !allowed.has(origin)) return send(res,403,{error:'Origin is not allowed'});
        if (origin) { res.setHeader('Access-Control-Allow-Origin',origin); res.setHeader('Vary','Origin'); }
        if (url.search) return send(res,400,{error:'Query parameters are not supported'});
        let upstream, method;
        if (url.pathname === '/api/context') { upstream=CONTEXT_URL; method='GET'; }
        else if (url.pathname === '/api/rpc') { upstream=RPC_URL; method='POST'; }
        else {
          const match = /^\/api\/candles\/(1|10|20)\/candles\/3600\/(\d{13})-(\d{13})$/.exec(url.pathname);
          if (!match) return send(res,404,{error:'Public endpoint is not allowlisted'});
          const start=Number(match[2]), end=Number(match[3]), now=Date.now();
          if (end < start || end-start > 72*3600000 || start < now-96*3600000 || end > now) return send(res,400,{error:'Only recent closed-hour windows up to 72 hours are allowed'});
          upstream='https://app.perpl.xyz/api/v1/market-data/'+match[1]+'/candles/3600/'+match[2]+'-'+match[3]; method='GET';
        }
        if (req.method === 'OPTIONS') {
          res.setHeader('Access-Control-Allow-Methods',method+', OPTIONS');
          res.setHeader('Access-Control-Allow-Headers','Content-Type');
          res.writeHead(204); return res.end();
        }
        if (req.method !== method) { res.setHeader('Allow',method+', OPTIONS'); return send(res,405,{error:'Method is not allowed'}); }
        let options={signal:AbortSignal.timeout(15000),redirect:'error'};
        if (method === 'POST') {
          const value=await body(req);
          if (JSON.stringify(Object.keys(value??{}).sort()) !== JSON.stringify(['id','jsonrpc','method','params']) || value.jsonrpc !== '2.0' || value.id !== 1 || value.method !== 'eth_chainId' || !Array.isArray(value.params) || value.params.length) return send(res,400,{error:'Only the shared-core eth_chainId identity read is allowed'});
          options={...options,method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(publicRpc)};
        }
        // Fixed upstreams only; no user-supplied URL or forwarded credentials.
        const upstreamResponse=await upstreamFetch(upstream,options);
        if (!upstreamResponse.ok) return send(res,502,{error:'Public upstream returned HTTP '+upstreamResponse.status});
        const raw=await upstreamResponse.text();
        try { JSON.parse(raw); } catch { return send(res,502,{error:'Public upstream did not return JSON'}); }
        return send(res,200,raw);
      }
      if (!['GET','HEAD'].includes(req.method)) return send(res,405,{error:'Static files are read-only'});
      const name=url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
      if (!assets.has(name)) return send(res,404,{error:'File not found'});
      let content=await readFile(new URL('./dist/'+name,import.meta.url));
      if (name === 'index.html') content=content.toString().replace('<head>','<head>\n  <meta name="nerya-proxy-base" content="/api">');
      const ext=name.slice(name.lastIndexOf('.'));
      if (req.method === 'HEAD') { res.writeHead(200,{'content-type':types[ext],'x-content-type-options':'nosniff'}); return res.end(); }
      return send(res,200,content,types[ext]);
    } catch (error) {
      const code=error.status || (error.code === 'ENOENT' ? 404 : 502);
      send(res,code,{error:code === 404 ? 'Build the static artifact first: npm run build' : error.status ? error.message : 'Public data request failed or timed out; no fallback data provided'});
    }
  };
}
export function createPublicServer(options) { return createServer(createPublicHandler(options)); }
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const port=Number(process.env.PORT || 8787);
  const origins=(process.env.PUBLIC_ORIGINS || '').split(',').map(s=>s.trim()).filter(Boolean);
  const server=createPublicServer({allowedOrigins:origins});
  server.listen(port,'127.0.0.1',()=>console.log('Public judge server: http://127.0.0.1:'+server.address().port+' (read-only proxy)'));
  for (const signal of ['SIGTERM','SIGINT']) process.on(signal,()=>server.close(()=>process.exit(0)));
}
