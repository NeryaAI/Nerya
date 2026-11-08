const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');

// Mount the actual page/renderer tree, run effects, and rerender state changes.
// No DOM/browser is required; unlike static markup this catches mount fetches.
function fixture(response, widgetResponses = {}) {
  const instances = new Map(), effects = [], calls = [];
  let current, dirty = false, tree, visited;
  const hooks = {
    useState(initial) {
      const owner = current, i = owner.cursor++;
      if (!(i in owner.slots)) owner.slots[i] = typeof initial === 'function' ? initial() : initial;
      return [owner.slots[i], next => { const value = typeof next === 'function' ? next(owner.slots[i]) : next;
        if (!Object.is(value, owner.slots[i])) { owner.slots[i] = value; dirty = true; } }];
    },
    useEffect(fn, deps) {
      const owner = current, i = owner.cursor++, prior = owner.slots[i];
      if (!prior || !deps || deps.some((v, j) => !Object.is(v, prior.deps[j]))) {
        owner.slots[i] = { deps }; effects.push(() => { prior?.cleanup?.(); owner.slots[i].cleanup = fn(); });
      }
    },
    useMemo: fn => fn(),
  };
  const api = new Proxy({}, { get: (_, key) => async (...args) => {
    calls.push([key, ...args]);
    if (key !== 'workspaceUiPreview' && !Object.hasOwn(widgetResponses, key)) throw Error('Forbidden live widget call: ' + key);
    const result = key === 'workspaceUiPreview' ? response : widgetResponses[key];
    if (result instanceof Error) throw result;
    return typeof result === 'function' ? result(...args) : result;
  } });
  const element = (type, props, key) => ({ type, props: props || {}, key });
  const shell = name => props => element(name, props);
  const modules = {};
  function load(relative) {
    const filename = path.resolve(__dirname, relative);
    if (modules[filename]) return modules[filename];
    const m = new Module(filename, module); m.filename = filename; m.paths = module.paths;
    m.require = name => {
      if (name === 'react') return hooks;
      if (name === 'react/jsx-runtime') return { jsx: element, jsxs: element, Fragment: 'fragment' };
      if (name === 'next-intl') return { useTranslations: () => (key, values) => key + (values ? JSON.stringify(values) : '') };
      if (name === 'next/link') return { __esModule: true, default: shell('a') };
      if (name.endsWith('/clientApi')) return { clientApi: api };
      if (name.endsWith('/useWorkspaceUi')) return { useWorkspaceUi: () => { calls.push(['liveManifest']); throw Error('Live hook mounted'); } };
      if (name.endsWith('/WorkspaceCustomizeButton')) return { WorkspaceCustomizeButton: () => { throw Error('Customization mounted'); } };
      if (name === './WorkspaceUiRenderer') return load('../components/workspace/WorkspaceUiRenderer.tsx');
      if (name === '../ModePill') return load('../components/ModePill.tsx');
      if (name === '../CandleChart') return { CandleChart: props => {
        if (!Object.hasOwn(widgetResponses, 'marketCandles')) throw Error('Live chart mounted');
        return element('CandleChart', props);
      } };
      if (name === '../Page' || name === './Page') return Object.fromEntries(['Card', 'Kpi', 'Pill', 'StatusDot', 'PageHeader', 'PageBody'].map(key => [key, shell(key)]));
      throw Error('Unmocked import: ' + name);
    };
    m._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
    } }).outputText, filename);
    modules[filename] = m.exports; return m.exports;
  }
  function mount(node, at = 'root') {
    if (Array.isArray(node)) return node.map((n, i) => mount(n, at + '/' + i));
    if (!node || typeof node !== 'object') return node;
    at += ':' + String(node.key ?? '');
    if (typeof node.type === 'function') {
      const key = at + '/' + node.type.name;
      const instance = instances.get(key) || { slots: [], cursor: 0 };
      visited.add(key);
      instances.set(key, instance); current = instance; instance.cursor = 0;
      return mount(node.type(node.props), key);
    }
    return { ...node, props: { ...node.props,
      actions: mount(node.props.actions, at + '/actions'),
      children: mount(node.props.children, at + '/' + String(node.type)),
    } };
  }
  return { calls, load, async render(Component, props) {
    for (let i = 0; i < 10; i++) {
      dirty = false; visited = new Set(); tree = mount(element(Component, props));
      for (const [key, instance] of instances) {
        if (visited.has(key)) continue;
        instance.slots.forEach(slot => slot?.cleanup?.());
        instances.delete(key);
      }
      effects.splice(0).forEach(fn => fn());
      await new Promise(resolve => setImmediate(resolve));
      if (!dirty) return tree;
    }
    throw Error('component did not settle');
  } };
}

function findButton(node, label) {
  if (Array.isArray(node)) return node.map(child => findButton(child, label)).find(Boolean);
  if (!node || typeof node !== 'object') return undefined;
  if (node.type === 'button' && node.props.children === label) return node;
  return findButton(node.props?.actions, label) || findButton(node.props?.children, label);
}

function click(tree, label) {
  const button = findButton(tree, label);
  assert.ok(button, `Missing button: ${label}`);
  assert.equal(button.props.type, 'button');
  button.props.onClick();
}

const widgets = [
  { id: 'btc', kind: 'market_ticker', title: 'BTC / USDT', config: { market: 'BTC/USDT' } },
  { id: 'eth', kind: 'market_ticker', title: 'ETH / USDT', config: { market: 'ETH/USDT' } },
  { id: 'account', kind: 'portfolio', title: 'Account overview' },
];
const candidate = { ok: true, proposal_id: 'prp_preview', state: 'pending_review', applied: false, data_validation: 'not_checked',
  manifest: { version: 1, home: { widgets: [] }, pages: [{ id: 'market-overview', title: 'Market overview', widgets }] } };

test('mounted preview renderer never mounts live hooks or requests widget data', async () => {
  const f = fixture(candidate);
  const { WorkspaceUiRenderer } = f.load('../components/workspace/WorkspaceUiRenderer.tsx');
  const all = [...widgets, ...['chart', 'strategies', 'attention', 'skill_panel', 'agent_panel', 'link'].map(kind => ({ id: kind, kind }))];
  const tree = await f.render(WorkspaceUiRenderer, { widgets: all, preview: true });
  assert.deepEqual(f.calls, []);
  const serialized = JSON.stringify(tree);
  assert.ok(serialized.includes('BTC / USDT')); assert.match(serialized, /Account overview/);
  assert.match(serialized, /previewDataNotLoaded/);
  assert.doesNotMatch(serialized, /"type":"(?:a|button)"/);
});

test('default market charts use a readable full row while explicit spans are preserved', async () => {
  const f = fixture(candidate);
  const { WorkspaceUiRenderer } = f.load('../components/workspace/WorkspaceUiRenderer.tsx');
  const tree = await f.render(WorkspaceUiRenderer, { preview: true, widgets: [
    { id: 'default-chart', kind: 'chart' }, { id: 'explicit-chart', kind: 'chart', span: 'wide' },
  ] });
  assert.match(tree.props.children[0].props.className, /xl:col-span-3/);
  assert.match(tree.props.children[1].props.className, /xl:col-span-2/);
});

test('market chart exposes the last recorded bar time and stale source without inventing freshness', async () => {
  const f = fixture(candidate, { marketCandles: {
    candles: [{ ts: 1700000000, open: 100, high: 101, low: 99, close: 100, volume: 1 }],
    _envelope: { source: 'fixture_rest', mode: 'live', degraded: false, fallback_used: false },
  } });
  const { WorkspaceUiRenderer } = f.load('../components/workspace/WorkspaceUiRenderer.tsx');
  const tree = await f.render(WorkspaceUiRenderer, { widgets: [{ id: 'chart', kind: 'chart' }] });
  const text = JSON.stringify(tree);
  assert.match(text, /marketLastBarAt/);
  assert.match(text, /marketHistoryStale/);
  assert.match(text, /fixture_rest/);
});

test('invalid candle timestamps do not crash the widget or fabricate a recorded time', async () => {
  for (const ts of [null, 0, -1, 1e308]) {
    const f = fixture(candidate, { marketCandles: {
      candles: [{ ts, open: 100, high: 101, low: 99, close: 100, volume: 1 }],
    } });
    const { WorkspaceUiRenderer } = f.load('../components/workspace/WorkspaceUiRenderer.tsx');
    const tree = await f.render(WorkspaceUiRenderer, { widgets: [{ id: 'chart', kind: 'chart' }] });
    assert.doesNotMatch(JSON.stringify(tree), /marketLastBarAt|Invalid Date/);
  }
});

test('native ticker preserves provider 24h changes, unknowns and diagnostic details', async () => {
  for (const [change, percentage] of [[125, 1.25], [-125, -1.25], [0, 0], ['25', '0.5'],
    [null, undefined], ['', ' '], [false, NaN], [Infinity, 'unknown']]) {
    const f = fixture(candidate, { marketTicker: { last: 10000, change, percentage } });
    const { WorkspaceUiRenderer } = f.load('../components/workspace/WorkspaceUiRenderer.tsx');
    const tree = await f.render(WorkspaceUiRenderer, { widgets: [widgets[0]] });
    const serialized = JSON.stringify(tree);
    assert.match(serialized, /marketChange24h/);
    assert.match(serialized, /marketPercentage24h/);
    if (change === null || change === '' || change === false || change === Infinity) {
      assert.equal((serialized.match(/marketValueUnknown/g) || []).length, 2);
    } else {
      assert.doesNotMatch(serialized, /marketValueUnknown/);
      assert.ok(serialized.includes(Number(change).toLocaleString(undefined, { maximumFractionDigits: 8, signDisplay: 'exceptZero' })));
      assert.ok(serialized.includes(Number(percentage).toLocaleString(undefined, { maximumFractionDigits: 2, signDisplay: 'exceptZero' }) + '%'));
      assert.ok(serialized.includes(change > 0 ? 'text-ok' : change < 0 ? 'text-danger' : 'text-[color:var(--text)]'));
    }
    const hidden = await f.render(WorkspaceUiRenderer, { widgets: [{ ...widgets[0], config: { show_change: false } }] });
    assert.doesNotMatch(JSON.stringify(hidden), /marketChange24h|marketPercentage24h/);
  }
  const diagnostic = 'HTTPError: HTTP Error 451: restricted location\nTraceback: provider request failed';
  function nodes(node) {
    if (Array.isArray(node)) return node.flatMap(nodes);
    if (!node || typeof node !== 'object') return [];
    return [node, ...nodes(node.props?.children)];
  }
  for (const [kind, error] of [['market_ticker', diagnostic], ['chart', 'HTTP Error 451']]) {
    const f = fixture(candidate, { marketTicker: { error }, marketCandles: { candles: [], error } });
    const { WorkspaceUiRenderer } = f.load('../components/workspace/WorkspaceUiRenderer.tsx');
    const tree = await f.render(WorkspaceUiRenderer, { widgets: [{ id: kind, kind }] });
    const all = nodes(tree), alert = all.find(node => node.props?.role === 'alert');
    assert.ok(alert);
    assert.match(JSON.stringify(alert.props.children[0]), /marketAccessDenied/);
    assert.doesNotMatch(JSON.stringify(alert.props.children[0]), /HTTPError|Traceback/);
    assert.equal(all.find(node => node.type === 'pre').props.children, error);
    assert.equal(all.find(node => node.type === 'details').props.open, undefined);
  }
  for (const locale of ['en', 'zh']) {
    const { workspaceUi } = JSON.parse(fs.readFileSync(path.join(__dirname, '../messages', locale, 'settings.json'), 'utf8'));
    for (const key of ['marketChange24h', 'marketPercentage24h', 'marketValueUnknown', 'marketAccessDenied', 'widgetReadFailed', 'widgetDiagnosticDetails']) {
      assert.ok(workspaceUi[key]?.trim(), locale + '.' + key);
    }
  }
});

test('mounted candidate page fetches only the preview and shows unapplied structure', async () => {
  const f = fixture(candidate);
  const { WorkspacePageView } = f.load('../components/workspace/WorkspacePageView.tsx');
  const tree = await f.render(WorkspacePageView, { pageId: 'preview', proposalId: 'prp_preview' });
  assert.deepEqual(f.calls, [['workspaceUiPreview', 'prp_preview']]);
  assert.match(JSON.stringify(tree), /previewUnapplied/);
  assert.ok(JSON.stringify(tree).includes('BTC / USDT'));
});

test('candidate errors do not fall back to active manifest or live widgets', async () => {
  const f = fixture(new Error('candidate_not_found'));
  const { WorkspacePageView } = f.load('../components/workspace/WorkspacePageView.tsx');
  const tree = await f.render(WorkspacePageView, { pageId: 'preview', proposalId: 'missing' });
  assert.deepEqual(f.calls, [['workspaceUiPreview', 'missing']]);
  assert.match(JSON.stringify(tree), /candidate_not_found/);
  assert.ok(!JSON.stringify(tree).includes('BTC / USDT'));
});

test('explicit preview action loads existing read-only widgets without applying or certifying the candidate', async () => {
  const snapshot = JSON.stringify(candidate);
  const f = fixture(candidate, {
    marketTicker: ({ market }) => ({ last: market === 'BTC/USDT' ? 67001 : 3501 }),
    portfolioSummary: { totals: { equity_usd: 1234, cash_usd: 1000 }, accounts: [] },
  });
  const { WorkspacePageView } = f.load('../components/workspace/WorkspacePageView.tsx');
  const props = { pageId: 'preview', proposalId: 'prp_preview' };
  let tree = await f.render(WorkspacePageView, props);
  assert.deepEqual(f.calls, [['workspaceUiPreview', 'prp_preview']]);
  click(tree, 'previewLoadData');
  tree = await f.render(WorkspacePageView, props);
  assert.deepEqual(f.calls, [
    ['workspaceUiPreview', 'prp_preview'],
    ['marketTicker', { venue: 'bybit', market: 'BTC/USDT' }],
    ['marketTicker', { venue: 'bybit', market: 'ETH/USDT' }],
    ['portfolioSummary'],
  ]);
  const serialized = JSON.stringify(tree);
  assert.ok(serialized.includes((67001).toLocaleString()));
  assert.ok(serialized.includes((3501).toLocaleString()));
  assert.match(serialized, /previewReadOnlyData/);
  assert.match(serialized, /previewUnapplied/);
  assert.match(serialized, /pending_review/);
  assert.match(serialized, /previewDataDisclaimer/);
  assert.doesNotMatch(serialized, /previewApplied|previewDataNotLoaded|previewStructureOnly/);
  assert.equal(JSON.stringify(candidate), snapshot);
  await f.render(WorkspacePageView, props);
  assert.equal(f.calls.length, 4, 'ordinary rerenders must not refresh data');
  click(tree, 'previewShowStructure');
  tree = await f.render(WorkspacePageView, props);
  assert.match(JSON.stringify(tree), /previewDataNotLoaded/);
  assert.equal(f.calls.length, 4);
});

test('preview retains widget loading, errors and empty states, and explicit reload retries reads', async () => {
  let finishTicker, tickerAttempts = 0;
  const pendingTicker = new Promise(resolve => { finishTicker = resolve; });
  const f = fixture(candidate, {
    marketTicker: ({ market }) => {
      if (market === 'ETH/USDT') return { error: 'market_unavailable' };
      return ++tickerAttempts === 1 ? pendingTicker : { last: 68000 };
    },
    portfolioSummary: new Error('account_unavailable'),
  });
  const { WorkspacePageView } = f.load('../components/workspace/WorkspacePageView.tsx');
  const props = { pageId: 'preview', proposalId: 'prp_preview' };
  let tree = await f.render(WorkspacePageView, props);
  click(tree, 'previewLoadData');
  tree = await f.render(WorkspacePageView, props);
  assert.match(JSON.stringify(tree), /loadingWidget/);
  assert.match(JSON.stringify(tree), /market_unavailable/);
  assert.match(JSON.stringify(tree), /account_unavailable/);
  assert.match(JSON.stringify(tree), /previewUnapplied/);
  finishTicker({});
  tree = await f.render(WorkspacePageView, props);
  assert.doesNotMatch(JSON.stringify(tree), /loadingWidget/);
  assert.ok(JSON.stringify(tree).includes(JSON.stringify('—')));
  click(tree, 'previewReloadData');
  tree = await f.render(WorkspacePageView, props);
  assert.equal(tickerAttempts, 2);
  assert.equal(f.calls.length, 7);
  assert.ok(JSON.stringify(tree).includes((68000).toLocaleString()));
  assert.match(JSON.stringify(tree), /previewUnapplied/);
});

test('loaded preview keeps the widget allowlist, chart errors, empty results and paper/live status', async () => {
  const dataWidgets = ['chart', 'strategies', 'attention', 'skill_panel', 'agent_panel', 'execute_trade'].map(kind => ({
    id: kind, kind, config: { url: '/workspace/ui/apply', method: 'POST', proposal_id: 'prp_preview' },
  }));
  const response = { ...candidate, manifest: { ...candidate.manifest, pages: [{ id: 'data', title: 'Data', widgets: dataWidgets }] } };
  const f = fixture(response, {
    marketCandles: { candles: [], error: 'candles_unavailable' },
    strategyList: { strategies: [
      { id: 'paper', title: 'Paper strategy', status: 'paper', total_pnl_usd: 4 },
      { id: 'live', title: 'Live strategy', status: 'live', total_pnl_usd: -2 },
    ] },
    operatorOverview: { data: { attention: [] } },
    skills: { skills: [] },
    agentsList: { roles: [] },
  });
  const { WorkspacePageView } = f.load('../components/workspace/WorkspacePageView.tsx');
  const props = { pageId: 'data', proposalId: 'prp_preview' };
  let tree = await f.render(WorkspacePageView, props);
  assert.deepEqual(f.calls, [['workspaceUiPreview', 'prp_preview']]);
  click(tree, 'previewLoadData');
  tree = await f.render(WorkspacePageView, props);
  assert.deepEqual(f.calls.map(call => call[0]), [
    'workspaceUiPreview', 'marketCandles', 'strategyList', 'operatorOverview', 'skills', 'agentsList',
  ]);
  const serialized = JSON.stringify(tree);
  assert.match(serialized, /candles_unavailable/);
  assert.ok(serialized.includes(JSON.stringify('paper')));
  assert.ok(serialized.includes(JSON.stringify('live')));
  assert.match(serialized, /nothingNeedsAttention/);
  assert.match(serialized, /noData/);
  assert.match(serialized, /unsupportedWidget/);
  assert.match(serialized, /execute_trade/);
  assert.match(serialized, /previewUnapplied/);
});

test('switching proposal or page resets data opt-in and ignores late widget results', async () => {
  for (const change of [{ proposalId: 'prp_other' }, { pageId: 'market-overview' }]) {
    let finishTicker;
    const pendingTicker = new Promise(resolve => { finishTicker = resolve; });
    const f = fixture(candidate, {
      marketTicker: () => pendingTicker,
      portfolioSummary: { totals: {}, accounts: [] },
    });
    const { WorkspacePageView } = f.load('../components/workspace/WorkspacePageView.tsx');
    const props = { pageId: 'preview', proposalId: 'prp_preview' };
    let tree = await f.render(WorkspacePageView, props);
    click(tree, 'previewLoadData');
    tree = await f.render(WorkspacePageView, props);
    assert.match(JSON.stringify(tree), /loadingWidget/);
    const nextProps = { ...props, ...change };
    tree = await f.render(WorkspacePageView, nextProps);
    assert.match(JSON.stringify(tree), /previewDataNotLoaded/);
    assert.ok(findButton(tree, 'previewLoadData'));
    assert.equal(f.calls.length, 5);
    finishTicker({ last: 98765 });
    tree = await f.render(WorkspacePageView, nextProps);
    assert.match(JSON.stringify(tree), /previewDataNotLoaded/);
    assert.ok(!JSON.stringify(tree).includes((98765).toLocaleString()));
    assert.equal(f.calls.length, 5);
  }
});

test('empty or missing candidate pages do not offer a data-load action', async () => {
  for (const pageId of ['home', 'missing-page']) {
    const f = fixture(candidate);
    const { WorkspacePageView } = f.load('../components/workspace/WorkspacePageView.tsx');
    const tree = await f.render(WorkspacePageView, { pageId, proposalId: 'prp_preview' });
    assert.equal(findButton(tree, 'previewLoadData'), undefined);
    assert.deepEqual(f.calls, [['workspaceUiPreview', 'prp_preview']]);
  }
});

test('preview controls and read-only boundaries are localized in English and Chinese', () => {
  for (const locale of ['en', 'zh']) {
    const { workspaceUi } = JSON.parse(fs.readFileSync(path.join(__dirname, '../messages', locale, 'settings.json'), 'utf8'));
    for (const key of ['previewLoadData', 'previewReloadData', 'previewShowStructure', 'previewReadOnlyData', 'previewReadOnlyDescription', 'previewDataDisclaimer']) {
      assert.ok(workspaceUi[key]?.trim(), locale + '.' + key);
    }
    assert.match(workspaceUi.previewDataDisclaimer, locale === 'en' ? /validation receipt/ : /验证回执/);
    assert.match(workspaceUi.previewDataDisclaimer, locale === 'en' ? /Paper\/live/ : /模拟盘与实盘/);
  }
});

function nodesOfType(tree, type) {
  if (Array.isArray(tree)) return tree.flatMap(node => nodesOfType(node, type));
  if (!tree || typeof tree !== 'object') return [];
  return [
    ...(tree.type === type ? [tree] : []),
    ...nodesOfType(tree.props?.actions, type),
    ...nodesOfType(tree.props?.children, type),
  ];
}

test('portfolio labels modes from every actual account, never from configuration or trade enablement', async () => {
  for (const [modes, label, tone] of [
    [['paper', 'paper'], 'PAPER', 'brand'],
    [['live', 'live'], 'LIVE', 'danger'],
    [['paper', 'live'], 'portfolioModeMixed', 'warn'],
    [[], 'portfolioModeUnknown', 'neutral'],
    [[undefined], 'portfolioModeUnknown', 'neutral'],
    [[null], 'portfolioModeUnknown', 'neutral'],
    [['simulation'], 'portfolioModeUnknown', 'neutral'],
    [['paper', undefined], 'portfolioModeUnknown', 'neutral'],
    [['paper', 'live', 'unknown'], 'portfolioModeUnknown', 'neutral'],
  ]) {
    const f = fixture(candidate, {
      portfolioSummary: {
        accounts: modes.map((mode, id) => ({ id, mode, live_trading_enabled: mode !== 'live', positions: {} })),
        totals: { equity_usd: 610000, cash_usd: 610000 },
      },
    });
    const { WorkspaceUiRenderer } = f.load('../components/workspace/WorkspaceUiRenderer.tsx');
    const tree = await f.render(WorkspaceUiRenderer, {
      widgets: [{ id: 'portfolio', kind: 'portfolio', config: { mode: 'live', live_trading_enabled: true } }],
    });
    const badges = nodesOfType(tree, 'Pill');
    assert.equal(badges.length, 1);
    assert.equal(badges[0].props.children, label, JSON.stringify(modes));
    assert.equal(badges[0].props.tone, tone);
    assert.deepEqual(f.calls, [['portfolioSummary']]);
  }
});

test('portfolio keeps absent amounts distinct from zero and uses a card-width grid with unbroken amounts', async () => {
  for (const [equity, cash, hasValue] of [
    [610000, 610000, true],
    [0, 0, true],
    [null, undefined, false],
    [NaN, Infinity, false],
    ['', 'invalid', false],
  ]) {
    const f = fixture(candidate, {
      portfolioSummary: { accounts: [{ mode: 'paper', positions: {} }], totals: { equity_usd: equity, cash_usd: cash } },
    });
    const { WorkspaceUiRenderer } = f.load('../components/workspace/WorkspaceUiRenderer.tsx');
    const tree = await f.render(WorkspaceUiRenderer, { widgets: [{ id: 'portfolio', kind: 'portfolio' }] });
    const metrics = nodesOfType(tree, 'Kpi');
    for (const metric of metrics.slice(0, 2)) {
      assert.equal(metric.props.value.type, 'span');
      assert.match(metric.props.value.props.className, /whitespace-nowrap/);
      assert.match(metric.props.value.props.className, /overflow-x-auto/);
      if (!hasValue) assert.equal(metric.props.value.props.children, '—');
      else {
        const expected = new Intl.NumberFormat(undefined, {
          style: 'currency', currency: 'USD', maximumFractionDigits: equity >= 1000 ? 0 : 2,
        }).format(equity);
        assert.equal(metric.props.value.props.children, expected);
      }
    }
    assert.equal(metrics[2].props.value, 0);
    const layout = nodesOfType(tree, 'div').find(node => node.props.className?.includes('auto-fit'));
    assert.ok(layout);
    assert.match(layout.props.className, /minmax\(min\(100%,12rem\),1fr\)/);
    assert.doesNotMatch(JSON.stringify(tree), /sm:grid-cols-3/);
  }
  const f = fixture(candidate, {
    portfolioSummary: { accounts: [{ mode: 'paper' }], totals: {} },
  });
  const { WorkspaceUiRenderer } = f.load('../components/workspace/WorkspaceUiRenderer.tsx');
  const tree = await f.render(WorkspaceUiRenderer, { widgets: [{ id: 'portfolio', kind: 'portfolio' }] });
  assert.equal(nodesOfType(tree, 'Kpi')[2].props.value, '—');
});

test('market chart preserves a proposed timeframe', async () => {
  const f = fixture(candidate, { marketCandles: { candles: [] } });
  const { WorkspaceUiRenderer } = f.load('../components/workspace/WorkspaceUiRenderer.tsx');
  await f.render(WorkspaceUiRenderer, { widgets: [{ id: 'daily-chart', kind: 'chart', config: { venue: 'binance', market: 'ETH/USDT', timeframe: '1d' } }] });
  assert.deepEqual(f.calls, [['marketCandles', { venue: 'binance', market: 'ETH/USDT', interval: '1d', count: 120 }]]);
});

test('portfolio mixed and unknown modes have explicit labels in both locales', () => {
  for (const locale of ['en', 'zh']) {
    const { workspaceUi } = JSON.parse(fs.readFileSync(path.join(__dirname, '../messages', locale, 'settings.json'), 'utf8'));
    assert.equal(workspaceUi.portfolioModeUnknown, locale === 'en' ? 'Unknown' : '未知');
    assert.equal(workspaceUi.portfolioModeMixed, locale === 'en' ? 'Mixed (paper + live)' : '混合（模拟盘 + 实盘）');
  }
});
