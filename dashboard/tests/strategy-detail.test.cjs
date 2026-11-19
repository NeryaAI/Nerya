// Pure UI contract fixtures, never persisted to the operator workspace.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, filename);
require.extensions['.css'] = module => { module.exports = new Proxy({}, { get: (_, key) => String(key) }); };
const { strategyDetailId, parseStrategyDetailId } = require('../lib/strategyDetail.ts');
const { strategyMarketResponseMatches } = require('../lib/strategyLanding.ts');
const { replayCandles, replayMarkers, replayTrades, replayNumber, replayTime } = require('../lib/backtestMarket.ts');
const T = 1700000000;

test('chart identity accepts canonical venue prefixes without accepting another asset or venue', () => {
  const outcome = {venue:'polymarket',market:'123456789012345678901'};
  assert.equal(strategyMarketResponseMatches(outcome,'POLYMARKET:123456789012345678901'),true);
  assert.equal(strategyMarketResponseMatches(outcome,outcome.market),true);
  assert.equal(strategyMarketResponseMatches(outcome,'POLYMARKET:999'),false);
  assert.equal(strategyMarketResponseMatches(outcome,'OTHER:123456789012345678901'),false);
  assert.equal(strategyMarketResponseMatches({venue:'bybit',market:'BTC/USDT:USDT'},'BYBIT:BTC/USDT:USDT'),true);
});
const panel = {id:'price:BTC',type:'candlestick',title:'BTC',market:'FIXTURE:BTC',series:[
  {kind:'candles',data:[{time:T,open:100,high:103,low:99,close:102}]},
  {kind:'markers',data:[{id:'trade:0',time:T,shape:'arrowUp'}, {id:'trade:1',time:T,shape:'arrowDown'}, {id:'gbs:0',time:T,kind:'gbs',text:'GBS'}]},
]};

test('tab identity includes strategy, candidate and exact run, with reversible escaping', () => {
  const base = {kind:'backtest',strategyId:'策略:a',proposalId:'candidate:b',ts:'20260927_120000'};
  assert.deepEqual(parseStrategyDetailId(strategyDetailId(base)),base);
  const variations = [base,{...base,strategyId:'other'}, {...base,proposalId:'other'}, {...base,ts:'20260927_120001'}, {...base,kind:'strategy'}];
  assert.equal(new Set(variations.map(strategyDetailId)).size,5);
  assert.equal(parseStrategyDetailId('backtest:20260927_120000'),null);
  assert.equal(parseStrategyDetailId('strategy:%broken'),null);
  assert.equal(parseStrategyDetailId('strategy:%5B%22%22%2C%22%22%5D'),null);
});

test('all simultaneous buy, sell and GBS markers survive', () => {
  const markers = replayMarkers(panel);
  assert.equal(markers.length,3);
  assert.deepEqual(markers.map(marker=>marker.id),['trade:0','trade:1','gbs:0']);
  assert.deepEqual(markers.map(marker=>marker.text),['B','S','GBS']);
  assert.deepEqual(markers.map(marker=>marker.kind),['trade','trade','gbs']);
});

test('bad candles and out-of-range markers cannot corrupt chart data', () => {
  const invalid = {...panel,series:[...panel.series,{kind:'candles',data:[{time:T+3600,open:1,high:0,low:3,close:2}]},{kind:'markers',data:[{time:T+3600,text:'GBS'}]}]};
  assert.equal(replayCandles(invalid).length,1);
  assert.equal(replayMarkers(invalid).length,3);
  assert.equal(replayNumber('NaN'),null); assert.equal(replayNumber(''),null); assert.equal(replayNumber(false),null);
  assert.equal(replayTime(T*1000),T); assert.equal(replayTime('2026-09-27T12:00:00'),null);
});

test('CSV trade ids stay joined to markers and ambiguous market is never guessed', () => {
  const table = {id:'trades',columns:['market','side','price','trade_id'],rows:[['FIXTURE:BTC','buy','101.000001','trade:0'],['FIXTURE:ETH','sell','20','trade:1']]};
  const rows = replayTrades([table],[panel,{...panel,market:'FIXTURE:ETH'}]);
  assert.equal(rows[0].id,'trade:0'); assert.equal(rows[0].row.price,'101.000001');
  assert.equal(rows[1].market,'FIXTURE:ETH');
  const missing = {id:'trades',columns:['side'],rows:[['buy']]};
  assert.equal(replayTrades([missing],[panel,{...panel,market:'FIXTURE:ETH'}])[0].market,'');
  assert.equal(replayTrades([missing],[panel])[0].market,'FIXTURE:BTC');
});

test('chat report controls target the right tab instead of a navigation link', () => {
  const React = require('react');
  const { renderToStaticMarkup } = require('react-dom/server');
  const { NextIntlClientProvider } = require('next-intl');
  const { StrategyDetailContext } = require('../components/chat/StrategyDetailContext.tsx');
  const { BacktestReplyCards } = require('../components/chat/BacktestReplyCards.tsx');
  const target = {kind:'backtest',strategyId:'fixture',proposalId:'prp_fixture',ts:'20260927_120000'};
  const message = {role:'assistant',id:'reply',ts:T,turn:{blocks:[{block:{kind:'tool_result',action:'strategy_backtest',result:{ok:true,strategy_id:target.strategyId,proposal_id:target.proposalId,backtest_ts:target.ts,metrics_display:{total_return_pct:'1.00%'}}}}]}};
  const html = renderToStaticMarkup(React.createElement(NextIntlClientProvider,{locale:'en',messages:{},timeZone:'UTC'},
    React.createElement(StrategyDetailContext.Provider,{value:{open:()=>{},active:strategyDetailId(target)}},React.createElement(BacktestReplyCards,{message}))));
  assert.match(html,/expand-backtest-details/);
  assert.ok(html.includes('aria-controls="task-dock-panel-'+strategyDetailId(target)+'"'));
  assert.match(html,/aria-expanded="true"/);
  assert.doesNotMatch(html,/href="\/strategies/);
});

test('market explorer labels the loaded full-year range and does not hide missing instruments', () => {
  const React = require('react');
  const { renderToStaticMarkup } = require('react-dom/server');
  const { NextIntlClientProvider } = require('next-intl');
  const { BacktestMarketExplorer } = require('../components/backtest/BacktestMarketExplorer.tsx');
  const start = 1735689600;
  const full = {...panel, series:[{kind:'candles',data:Array.from({length:2190}, (_,i)=>({
    time:start+i*14400,open:100,high:103,low:99,close:102,
  }))}]};
  const html=renderToStaticMarkup(React.createElement(NextIntlClientProvider,{locale:'en',messages:{},timeZone:'UTC'},
    React.createElement(BacktestMarketExplorer,{panels:[full],tables:[],meta:{markets:['FIXTURE:BTC','FIXTURE:ETH'],tf:'4h'}})));
  assert.match(html,/backtest-loaded-range/);
  assert.match(html,/2,190/);
  assert.match(html,/2025-01-01 00:00:00/);
  assert.match(html,/2025-12-31 20:00:00/);
  assert.match(html,/data-candle-count="2190"/);
  assert.match(html,/Fit range/);
});
