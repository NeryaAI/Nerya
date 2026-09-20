const {test} = require('node:test');
const assert = require('node:assert/strict');
const {loopback,markPeer,PROOF} = require('./local-server.cjs');
for (const address of ['127.0.0.1','::1','::ffff:127.0.0.1']) test(`real loopback ${address}`,()=>assert.equal(loopback(address),true));
for (const address of ['127.evil.example','192.168.1.1','8.8.8.8','']) test(`not loopback ${address}`,()=>assert.equal(loopback(address),false));
test('direct local request gains proof before framework forwarding headers',()=>{ const r={socket:{remoteAddress:'::1'},headers:{}};markPeer(r,'server-only-key');r.headers['x-forwarded-for']='::1';assert.equal(r.headers[PROOF],'server-only-key'); });
for (const header of ['x-forwarded-for','x-real-ip','forwarded']) test(`forwarded request cannot gain proof via ${header}`,()=>{ const r={socket:{remoteAddress:'127.0.0.1'},headers:{[header]:'127.0.0.1',[PROOF]:'forged'}};markPeer(r,'real');assert.equal(r.headers[PROOF],undefined); });
test('remote client cannot forge local proof',()=>{const r={socket:{remoteAddress:'10.0.0.2'},headers:{[PROOF]:'real'}};markPeer(r,'real');assert.equal(r.headers[PROOF],undefined);});
