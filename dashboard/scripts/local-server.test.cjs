const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {loopback,markPeer,PROOF} = require('./local-server.cjs');
for (const address of ['127.0.0.1','::1','::ffff:127.0.0.1']) test(`real loopback ${address}`,()=>assert.equal(loopback(address),true));
for (const address of ['127.evil.example','192.168.1.1','8.8.8.8','']) test(`not loopback ${address}`,()=>assert.equal(loopback(address),false));
test('direct local request gains proof before framework forwarding headers',()=>{ const r={socket:{remoteAddress:'::1'},headers:{}};markPeer(r,'server-only-key');r.headers['x-forwarded-for']='::1';assert.equal(r.headers[PROOF],'server-only-key'); });
for (const header of ['x-forwarded-for','x-real-ip','forwarded']) test(`forwarded request cannot gain proof via ${header}`,()=>{ const r={socket:{remoteAddress:'127.0.0.1'},headers:{[header]:'127.0.0.1',[PROOF]:'forged'}};markPeer(r,'real');assert.equal(r.headers[PROOF],undefined); });
test('remote client cannot forge local proof',()=>{const r={socket:{remoteAddress:'10.0.0.2'},headers:{[PROOF]:'real'}};markPeer(r,'real');assert.equal(r.headers[PROOF],undefined);});

const serverSource = fs.readFileSync(require.resolve('./local-server.cjs'), 'utf8');
for (const code of ['EPERM', 'EACCES']) {
  test(`route scan ${code} fails before Next initialization or listen`, async () => {
    const path = require('node:path');
    const dir = path.resolve(__dirname, '..');
    const denied = path.join(dir, 'pages');
    const cause = Object.assign(new Error(`${code}: operation not permitted, scandir '${denied}'`), {code});
    const errors = [];
    const entry = {exports: {}};
    const processStub = {env: {NODE_ENV: 'development'}, cwd: () => dir, on() {}};
    let initialized = false, listening = false;
    const load = (id) => {
      if (id === 'node:fs/promises') return {async readdir(target) {
        if (target === denied) throw cause;
        return [];
      }};
      if (id === 'next') return () => {
        initialized = true;
        return {async prepare() {}, getRequestHandler: () => () => {}};
      };
      if (id === 'node:http') return {createServer: () => ({on() {}, listen() {listening = true;}})};
      return require(id);
    };
    load.main = entry;
    await vm.runInNewContext(serverSource, {require: load, module: entry, process: processStub,
      console: {error(error) {errors.push(error);}}});
    assert.equal(initialized, false, 'do not enter Next prepare with unreadable routes');
    assert.equal(listening, false);
    assert.equal(processStub.exitCode, 1);
    assert.equal(errors.length, 1);
    assert.equal(errors[0].cause, cause);
    assert.match(errors[0].message, /Files and Folders/);
    assert.ok(errors[0].message.includes(denied));
  });
}
for (const override of [undefined, '', '0', '1']) {
  test(`startup telemetry default preserves override ${JSON.stringify(override)}`, async () => {
    const env = {NODE_ENV: 'development', PORT: '18380'};
    if (override !== undefined) env.NEXT_TELEMETRY_DISABLED = override;
    const events = [];
    const scans = [];
    const record = (step) => events.push([step, env.NEXT_TELEMETRY_DISABLED]);
    const entry = {exports: {}};
    const load = (id) => {
      if (id === 'next') {
        record('require(next)');
        return () => {
          record('next()');
          return {
            async prepare() {
              await Promise.resolve();
              record('prepare()');
            },
            getRequestHandler: () => () => {},
            getUpgradeHandler: () => () => {},
          };
        };
      }
      if (id === 'node:http') {
        return {createServer: () => ({
          on() {},
          listen() { record('listen()'); },
        })};
      }
      if (id === 'node:fs/promises') return {async readdir(target) {
        scans.push(target);
        assert.equal(events.length, 0, 'scan routes before loading Next');
        if (target !== require('node:path').join(__dirname, 'app')) {
          throw Object.assign(new Error('missing optional route directory'), {code: 'ENOENT'});
        }
        return [];
      }};
      assert.ok(['node:crypto', 'node:net', 'node:path'].includes(id), `unexpected require: ${id}`);
      return require(id);
    };
    load.main = entry;
    await vm.runInNewContext(serverSource, {
      require: load,
      module: entry,
      process: {env, cwd: () => __dirname, on() {}},
      console: {error(error) { throw error; }},
    }, {filename: 'local-server.cjs'});
    const expected = override ?? '1';
    assert.deepEqual(scans, ['pages', 'app', 'src/pages', 'src/app'].map(p => require('node:path').join(__dirname, p)));
    assert.deepEqual(events, [
      ['require(next)', expected],
      ['next()', expected],
      ['prepare()', expected],
      ['listen()', expected],
    ]);
  });
}

test('Next alone dispatches upgrades; reconnect never creates a second server', async () => {
  const {EventEmitter} = require('node:events');
  const server = new EventEmitter();
  server.listen = () => {};
  const entry = {exports:{}};
  let httpHandler, frameworkDispatches = 0, prepared = 0;
  const load = (id) => {
    if (id === 'next') return () => ({
      async prepare() { prepared++; },
      getRequestHandler() {
        let installed = false;
        return () => {
          if (!installed) {
            installed = true;
            server.on('upgrade', (req) => {
              frameworkDispatches++;
              assert.notEqual(req.headers[PROOF], 'forged');
              if (req.headers.forwarded) assert.equal(req.headers[PROOF], undefined);
              else assert.ok(req.headers[PROOF]);
            });
          }
        };
      },
      getUpgradeHandler() { throw new Error('Do not instantiate a second Next server'); },
    });
    if (id === 'node:http') return {createServer(handler) { httpHandler = handler; return server; }};
    if (id === 'node:fs/promises') return {async readdir() {return [];}};
    assert.ok(['node:crypto','node:net','node:path'].includes(id));
    return require(id);
  };
  load.main = entry;
  await vm.runInNewContext(serverSource, {require:load, module:entry,
    process:{env:{NODE_ENV:'development'},cwd:()=>__dirname,on(){}},
    console:{error(error){throw error;}}});
  for (let i=0;i<4;i++) {
    httpHandler({socket:{remoteAddress:'127.0.0.1'},headers:{}},{});
    server.emit('upgrade',{socket:{remoteAddress:'127.0.0.1'},
      headers:{[PROOF]:'forged',...(i%2 ? {forwarded:'for=10.0.0.2'} : {})}}, {}, Buffer.alloc(0));
  }
  assert.equal(prepared,1);
  assert.equal(frameworkDispatches,4);
  assert.equal(server.listenerCount('upgrade'),2); // sanitizer + Next, never two dispatchers
});
