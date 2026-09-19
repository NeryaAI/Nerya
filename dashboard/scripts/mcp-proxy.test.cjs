// Compile only the proxy under test; never load or mutate a live dashboard config.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../lib/mcpProxy.ts'), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const calls = [];
const sandbox = { exports: {}, Headers, Request, Response, URL, Uint8Array, AbortController,
  setTimeout, clearTimeout, process: { env: { NERYA_API: 'http://127.0.0.1:18317', NERYA_API_TOKEN: 'must-not-inject', SERVER_TOKEN: 'must-not-inject' } },
  fetch: async (url, options) => { calls.push({ url, options }); return new Response(null, {
    status: 303, headers: { location: 'https://client.example/callback?code=test', 'set-cookie': 'nerya_mcp_consent=test; HttpOnly; Secure', 'www-authenticate': 'Bearer resource_metadata="test"' },
  }); },
};
vm.runInNewContext(code, sandbox);
(async () => {
  const proxy = sandbox.exports.proxyMcp;
  const result = await proxy(new Request('https://public.example/mcp/oauth/login?ticket=test', {
    method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded', authorization: 'Bearer user-token', host: 'public.example', cookie: 'nerya_mcp_consent=test' },
    body: 'password=temporary-test-value',
  }));
  assert.equal(result.status, 303);
  assert.equal(result.headers.get('location'), 'https://client.example/callback?code=test');
  assert.match(result.headers.get('set-cookie'), /HttpOnly/);
  assert.equal(calls[0].url, 'http://127.0.0.1:18317/mcp/oauth/login?ticket=test');
  assert.equal(calls[0].options.headers.get('authorization'), 'Bearer user-token');
  assert.equal(calls[0].options.headers.get('host'), 'public.example');
  assert.equal(calls[0].options.redirect, 'manual');
  await proxy(new Request('https://public.example/mcp'));
  assert.equal(calls[1].options.headers.get('authorization'), null);
  assert.equal(await (await proxy(new Request('https://public.example/mcp/internal-admin'))).text(), '{"error":"not_found"}');
  assert.equal((await proxy(new Request('https://public.example/mcp', { method: 'POST', body: 'x'.repeat(1048577) }))).status, 413);
  console.log('MCP public proxy: identity isolation, redirect/cookie preservation, path allowlist and body limit passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
