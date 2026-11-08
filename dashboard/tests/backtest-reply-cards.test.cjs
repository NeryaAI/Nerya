// Unit fixtures only. No fabricated performance is written to a user session.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, filename);
const React = require('react');
require.extensions['.css'] = module => { module.exports = new Proxy({}, { get: (_, key) => String(key) }); };
const { renderToStaticMarkup } = require('react-dom/server');
const { NextIntlClientProvider } = require('next-intl');
const { collectBacktestResults, extractBacktestResults } = require('../lib/backtestResults.ts');
const { BacktestReplyCards } = require('../components/chat/BacktestReplyCards.tsx');
const result = { ok: true, strategy_id: 'sdk_fixture', proposal_id: 'prp_fixture', backtest_ts: '20260926_120000', verdict: 'WARN',
  metrics_display: { total_return_pct: '0.0274%', max_drawdown_pct: '0.00%', sharpe_ratio: '0.10', total_trades: '2' } };
const reply = (id, value) => ({ role: 'assistant', id, ts: 1700000000000, loading: true,
  turn: { blocks: [{ block: { kind: 'tool_result', action: 'strategy_backtest', result: value } }] } });
function html(message, locale = 'en') {
  return renderToStaticMarkup(React.createElement(NextIntlClientProvider, { locale, messages: {}, timeZone: 'UTC' },
    React.createElement(BacktestReplyCards, { message })));
}

test('pending interaction and policy-only reads never become backtest result cards', () => {
  const pending = {ok:false, reason:'retrospective_replay_authorization_pending',
    interaction:{kind:'question',state:'pending'}, message:'Waiting for the user to answer.'};
  const receipts = [
    {kind:'tool_result',action:'strategy_backtest',call_id:'pending-check',ok:true,
      semantic_success:false,result_protocol:'user_interaction_pending',result:JSON.stringify(pending)},
    // Persisted traces can omit result_protocol, but retain the structured result.
    {action:'strategy_backtest',ok:true,result:JSON.stringify(pending)},
    {kind:'tool_result',action:'strategy_backtest',call_id:'policy-check',ok:true,
      payload:{policy_check_only:true},result:{...result,result_type:'retrospective_authorization_policy',replay_started:false}},
    {action:'strategy_backtest',ok:true,result:{...result,result_type:'retrospective_authorization_policy',replay_started:false}},
    {kind:'tool_result',action:'strategy_backtest',call_id:'repeated-check',ok:false,
      payload:{policy_check_only:true},result:null,error_kind:'deduped',error:'Repeated tool call suppressed'},
    // Transcript result blocks omit request arguments on a suppressed call.
    {kind:'tool_result',action:'strategy_backtest',call_id:'suppressed-check',ok:false,
      result:null,error_kind:'deduped',elapsed_ms:0,error:'Repeated tool call suppressed'},
  ];
  for (const receipt of receipts) {
    const original = JSON.stringify(receipt);
    assert.deepEqual(extractBacktestResults(receipt), []);
    const message = reply('pending-policy', receipt);
    message.turn.tool_trace = [receipt];
    message.turn.actions = [{action:'strategy_backtest',payload:{policy_check_only:true}}];
    assert.deepEqual(collectBacktestResults({messages:[message]}), []);
    for (const locale of ['en','zh']) assert.equal(html(message,locale), '');
    assert.equal(JSON.stringify(receipt),original);
  }
  const failure = {kind:'tool_result',action:'strategy_backtest',call_id:'actual-replay',ok:false,
    result:{ok:false,result_type:'backtest_result',strategy_id:'sdk_fixture',proposal_id:'prp_fixture',
      backtest_status:'failed',reason:'decision_response_truncated',message:'Replay did not complete.'}};
  const message = reply('mixed-results',receipts[0]);
  message.turn.blocks.push({block:failure});
  message.turn.tool_trace = [...receipts,failure];
  const rows = collectBacktestResults({messages:[message]});
  assert.equal(rows.length,1);
  assert.equal(rows[0].id,'mixed-results:error:call:actual-replay:1');
  assert.equal(rows[0].status,'failed');
  assert.equal(rows[0].strategyId,'sdk_fixture');
  assert.equal(rows[0].proposalId,'prp_fixture');
  assert.equal(rows[0].failureReason,'decision_response_truncated');
  assert.equal((html(message).match(/data-testid="backtest-result-card"/g)||[]).length,1);
});

test('failed replay card uses the same human summary as its tool and retains raw details', () => {
  const message = reply('scope-failure', {result_type:'backtest_result', ok:false,
    backtest_status:'blocked', proposal_id:'prp_fixture',
    message:'request_scope_unverified: invalid_binding'});
  for (const locale of ['en','zh']) {
    const rendered = html(message, locale);
    const status = rendered.match(/<p[^>]*role="status"[^>]*>([\s\S]*?)<\/p>/)?.[1];
    assert.match(status, locale==='zh'?/请求范围尚未验证/:/requested scope has not been verified/);
    assert.doesNotMatch(status, /request_scope_unverified|invalid_binding/);
    assert.match(rendered, /<details[\s\S]*request_scope_unverified: invalid_binding/);
    assert.doesNotMatch(rendered, /data-testid="open-backtest-report"/);
  }
});

test('truncated decision response uses its structured producer reason without raw JSON in the card summary', () => {
  const {toolFailureSummary} = require('../lib/toolFailureSummary.ts');
  const failure = {kind:'tool_result',call_id:'failed-replay',action:'strategy_backtest',ok:false,
    error:'Producer diagnostic\n{"reason":"decision_response_truncated","backtest_status":"failed"}',
    recovery:{reason:'decision_response_truncated',action:'inspect_retrospective_response',
      artifacts:{run_receipt:'evolution/proposals/prp_fixture/after/strategies/btc/backtests/20261102_165239/run.json'}},
    verification:{protocol:'nerya.verification.v1',artifact_id:'strategy:btc:proposal:prp_fixture'}};
  const message = reply('truncated-replay',failure);
  assert.equal(collectBacktestResults({messages:[message]})[0].failureReason,'decision_response_truncated');
  for (const locale of ['zh','en']) {
    const rendered = html(message,locale);
    const summary = rendered.match(/<p[^>]*role="status"[^>]*>([\s\S]*?)<\/p>/)?.[1];
    assert.match(summary,locale==='zh'?/模型响应达到输出上限，回放未完成/:/model response reached its output limit/i);
    assert.equal(toolFailureSummary(failure,locale==='zh').replaceAll("'",'&#x27;'),summary);
    assert.doesNotMatch(toolFailureSummary({arguments:{reason:'decision_response_truncated'}},locale==='zh'),locale==='zh'?/模型响应达到输出上限/:/model response reached its output limit/i);
    assert.match(summary,locale==='zh'?/需要操作员决定后再试/:/operator decision is required before retrying/i);
    assert.doesNotMatch(summary,/decision_response_truncated|Producer diagnostic|[{}]/);
    assert.match(rendered,/<details[\s\S]*decision_response_truncated/);
    assert.match(rendered,/data-status="failed"/);
    assert.doesNotMatch(rendered,/data-testid="open-backtest-report"/);
  }
  assert.match(html(reply('different-failure',{...failure,recovery:{reason:'other_failure'}}),'zh'),/这次操作失败了/);
});

test('scope mismatch uses localized recovery copy, never raw producer instructions', () => {
  const {toolFailureSummary} = require('../lib/toolFailureSummary.ts');
  const message='request_scope_mismatch: unverified_scope: [{"field":"base","expected":"ETH","actual":"WETH"}]. Preserve the candidate and admitted resource/window.';
  for (const locale of ['zh','en']) {
    const expected=locale==='zh' ? /候选配置与原请求不一致/ : /candidate configuration differs from the request/;
    assert.match(toolFailureSummary({ok:false,error:message},locale==='zh'),expected);
    assert.match(toolFailureSummary({ok:false,recovery:{reason:'request_scope_mismatch'}},locale==='zh'),expected);
    assert.doesNotMatch(toolFailureSummary({arguments:{reason:'request_scope_mismatch'}},locale==='zh'),expected);
    assert.doesNotMatch(toolFailureSummary({message:'A quoted request_scope_mismatch is discussed here.'},locale==='zh'),expected);
  }
});

test('pool discovery gaps are localized without treating arguments as a failure', () => {
  const {toolFailureSummary}=require('../lib/toolFailureSummary.ts');
  const failure={ok:false,error:'Pool identity discovery did not complete. Diagnostic: {"stage":"deadline"}',
    recovery:{action:'inspect_pool_discovery',safe_to_retry:false}};
  for(const zh of [true,false]) {
    assert.match(toolFailureSummary(failure,zh),zh?/池身份核验尚未完成/:/Pool identity verification did not complete/);
    assert.doesNotMatch(toolFailureSummary({arguments:failure},zh),zh?/池身份核验/:/Pool identity verification/);
  }
});

test('protocol preflight blocker has a localized actionable summary without raw exception text', () => {
  const { toolFailureSummary } = require('../lib/toolFailureSummary.ts');
  const failure = {result_type:'backtest_result', ok:false, backtest_status:'blocked',
    proposal_id:'prp_fixture', reason:'backtest_preflight_failed',
    message:'backtest failed: BacktestPreflightError: protocol data unavailable',
    diagnostics:{blockers:[{code:'protocol_replay_model_required',message:'raw diagnostic'}]}};
  for (const locale of ['en','zh']) {
    const expected = locale === 'zh' ? /需要协议级历史数据与回放模型/ : /requires protocol history and a matching replay model/;
    assert.match(toolFailureSummary(failure, locale === 'zh'), expected);
    const rendered = html(reply('protocol-gap', failure), locale);
    const status = rendered.match(/<p[^>]*role="status"[^>]*>([\s\S]*?)<\/p>/)?.[1];
    assert.match(status, expected);
    assert.doesNotMatch(status, /BacktestPreflightError|protocol_replay_model_required|raw diagnostic/);
    assert.doesNotMatch(rendered, /data-testid="open-backtest-report"/);
  }
  assert.doesNotMatch(toolFailureSummary({arguments:failure}, true), /需要协议级历史数据/);
  const projected = {action:'strategy_backtest',ok:false,error:failure.message,
    recovery:{type:'provide_protocol_replay',reason:'protocol_replay_model_required'}};
  assert.match(toolFailureSummary(projected, true), /需要协议级历史数据/);
  assert.equal(extractBacktestResults(projected,0,'strategy_backtest')[0].status,'blocked');
  assert.match(html(reply('projected-protocol-gap',projected),'zh'), /需要协议级历史数据/);
});

test('unsupported history capability stays localized through the persisted recovery projection', () => {
  const { toolFailureSummary } = require('../lib/toolFailureSummary.ts');
  const projected = {action:'strategy_backtest',ok:false,error:'raw provider diagnostic',
    recovery:{action:'provide_historical_capability',reason:'historical_provider_unsupported',safe_to_retry:false}};
  for (const locale of ['zh','en']) {
    const expected = locale === 'zh' ? /不支持所请求市场的历史回测/ : /does not support historical replay/;
    assert.match(toolFailureSummary(projected,locale==='zh'),expected);
    const rendered=html(reply('unsupported-history',projected),locale);
    const status=rendered.match(/<p[^>]*role="status"[^>]*>([\s\S]*?)<\/p>/)?.[1];
    assert.match(status,expected);
    assert.doesNotMatch(status,/raw provider diagnostic|historical_provider_unsupported/);
    assert.doesNotMatch(rendered,/data-testid="open-backtest-report"/);
  }
  assert.doesNotMatch(toolFailureSummary({arguments:projected},true),/不支持所请求市场/);
});

test('archive authorization gap is localized without leaking provider bodies or suggesting unchanged retry', () => {
  const {toolFailureSummary}=require('../lib/toolFailureSummary.ts');
  const failure={error:{kind:'configuration_required',message:'raw diagnostic',
    detail:{reason:'historical_rpc_authorization_required',safe_to_retry:false}}};
  for(const zh of [true,false]) {
    const text=toolFailureSummary(failure,zh);
    assert.match(text,zh?/历史.*授权/:/historical.*authorization/i);
    assert.doesNotMatch(text,/raw diagnostic|historical_rpc_authorization_required/);
  }
  assert.doesNotMatch(toolFailureSummary({arguments:failure},true),/历史.*授权/);
});

const researchFixture = {
  bias_checks: { static_temporal_scan: 'passed', historical_prefix_only: true,
    closed_bar_context: true, multi_timeframe_close_aligned: true,
    strategy_order_execution: 'next_bar_open', end_of_data_signal: 'rejected_no_next_bar',
    static_warnings: [{ code: 'lookahead_dynamic_shift', message: 'Direction requires review', file: 'main.py', line: 6 }] },
  research_checks: { version: 1, scope: 'this_run_only', checks: [
    'dynamic_lookahead','warmup_stability','out_of_sample','walk_forward','cost_stress','parameter_sensitivity','ablation',
  ].map(id => ({ id, status: 'not_run' })) },
  provenance: { source_revision: 'frozen-v1', assumptions: { warmup_bars: 100,
    execution_model_limits: { order_types:'market_only', funding:'not_modeled',liquidation:'not_modeled' } } },
};

test('research checks survive compacted receipts and later sparse streaming duplicates', () => {
  const message = reply('review', 'summary\n[compacted_kept]\n' + JSON.stringify({ ...result, ...researchFixture }));
  message.turn.tool_trace = [{ action:'strategy_backtest', result }];
  const [row] = collectBacktestResults({ messages:[message] });
  assert.deepEqual(row.biasChecks, researchFixture.bias_checks);
  assert.deepEqual(row.researchChecks, researchFixture.research_checks);
  const rendered = html(message);
  assert.match(rendered, /1 need review/);
  assert.match(rendered, /7 not run here/);
  assert.match(rendered, /Direction requires review/);
  assert.match(rendered, /Dynamic lookahead/);
  assert.match(rendered, /Funding, Margin liquidation/);
  assert.doesNotMatch(rendered, /No blocking patterns/);
  assert.match(rendered, /backtest-research-review/);
});

test('economic PASS and missing research metadata never become research validation', () => {
  const rendered = html(reply('legacy', { ...result, verdict:'PASS' }));
  assert.match(rendered, /Economic checks passed/);
  assert.match(rendered, /Not recorded/);
  assert.doesNotMatch(rendered, /No blocking patterns|7 not run here|Closed historical prefix only/);
});

test('malformed evidence does not crash or fabricate a passed audit', () => {
  const rendered = html(reply('malformed', { ...result,
    bias_checks: { static_warnings:[null,42,{message:{unexpected:true}}] },
    research_checks: { checks:[null,{id:'dynamic_lookahead',status:'passed'}] },
  }));
  assert.match(rendered, /Review separate evidence/);
  assert.doesNotMatch(rendered, /No blocking patterns|7 not run here|\[object Object\]/);
});

test('nested metrics retain research fields while preserving displayed percentage values', () => {
  const [row] = extractBacktestResults({ ...result, metrics:researchFixture });
  assert.deepEqual(row.researchChecks, researchFixture.research_checks);
  assert.equal(row.metrics.total_return_pct, '0.0274%');
});

test('review draft pins the historical source and does not request automatic tuning', () => {
  const { backtestReviewDraft } = require('../lib/backtestReview.ts');
  const draft = backtestReviewDraft({strategyId:'source',ts:'20260928_000000',proposalId:'prp_old',sourceRevision:'frozen'},true);
  assert.match(draft, /"proposal_id":"prp_old"/);
  assert.match(draft, /"backtest_ts":"20260928_000000"/);
  assert.match(draft, /"source_revision":"frozen"/);
  assert.match(draft, /不自动批量回测/);
  const source = fs.readFileSync(path.join(__dirname,'../components/backtest/BacktestReviewAction.tsx'),'utf8');
  assert.match(source, /autoSend: false/);
  assert.doesNotMatch(source, /run_turn|callApi/);
});

test('compacted result keeps exact proposal/run identity and percentage units', () => {
  const message = reply('first', 'backtest: metrics=[...]\n[compacted_kept]\n' + JSON.stringify(result));
  const rows = collectBacktestResults({ messages: [message] });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].id, 'prp_fixture:sdk_fixture:20260926_120000');
  const rendered = html(message);
  assert.match(rendered, /backtest-result-card/);
  assert.match(rendered, /0\.0274%/); assert.doesNotMatch(rendered, /2\.74%/);
  assert.match(rendered, /View equity curve and trades/);
  assert.match(rendered, /aria-expanded="false"/);
});

test('nested stdout and MCP JSON parts survive truncated summaries', () => {
  const stdout = 'partial {broken ...\n' + JSON.stringify(result);
  const rows = extractBacktestResults({ type: 'tool_result', name: 'strategy_backtest', content: [{ type: 'json', data: { stdout_json: { output: stdout } } }] });
  assert.equal(rows.length, 1); assert.equal(rows[0].proposalId, 'prp_fixture');
});

test('artifact locators recover proposal ids instead of loading promoted strategy', () => {
  const rows = extractBacktestResults({ out_dir: 'C:\\workspace\\evolution\\proposals\\prp_local\\after\\strategies\\sdk_fixture\\backtests\\sdk_20260926_120000' });
  assert.equal(rows[0].proposalId, 'prp_local');
  assert.equal(rows[0].ts, 'sdk_20260926_120000');
});

test('each reply contains its own results; duplicate representations are merged', () => {
  const first = reply('first', result);
  first.turn.tool_trace = [{ action: 'strategy_backtest', result }];
  assert.equal(collectBacktestResults({ messages: [first] }).length, 1);
  const second = reply('second', { ...result, backtest_ts: '20260926_130000' });
  assert.equal(collectBacktestResults({ messages: [first, second] }).length, 2);
  assert.doesNotMatch(html(second), /20260926_120000/);
});

test('failures show a single diagnostic, no performance or report button', () => {
  const failure = { result_type: 'backtest_result', ok: false, reason: 'backtest_sdk_unsupported', proposal_id: 'prp_fixture',
    message: 'Historical replay cannot run this live-only surface.' };
  const message = reply('failed', { type: 'tool_result', name: 'strategy_backtest', is_error: true,
    error: { kind: 'execution_error', message: 'generic failure' }, content: [{ type: 'json', data: failure }] });
  const rows = collectBacktestResults({ messages: [message] });
  assert.equal(rows.length, 1); assert.equal(rows[0].status, 'blocked');
  const rendered = html(message);
  assert.match(rendered, /Historical replay cannot/);
  assert.doesNotMatch(rendered, /open-backtest-report|Total return|Completed/);
  const partial = extractBacktestResults({ ...result, ok: false, reason: 'no_historical_data' }, 0, 'strategy_backtest');
  assert.equal(partial[0].status, 'blocked'); assert.equal(partial[0].ts, '');
});

test('failure card localizes an unknown execution reason while keeping producer instructions in details', () => {
  const raw = 'Do not retry the unchanged call or invent performance. Preserve the admitted scope.';
  const message = reply('execution-failure', { result_type: 'backtest_result', ok: false,
    backtest_status: 'failed', reason: 'execution_error', proposal_id: 'prp_fixture', message: raw });
  for (const locale of ['en', 'zh']) {
    const rendered = html(message, locale);
    const status = rendered.match(/<p[^>]*role="status"[^>]*>([\s\S]*?)<\/p>/)?.[1] || '';
    assert.doesNotMatch(status, /Do not retry|Preserve the admitted scope/);
    assert.match(status, locale === 'zh' ? /失败/ : /failed/i);
    assert.match(rendered, /<details[\s\S]*Do not retry the unchanged call/);
  }
});

test('neither prose, input tool calls nor plain research becomes a completed backtest', () => {
  assert.equal(collectBacktestResults({ messages: [{ role: 'user', text: JSON.stringify(result) }] }).length, 0);
  assert.equal(extractBacktestResults({ kind: 'tool_use', action: 'strategy_backtest', arguments: result }).length, 0);
  assert.equal(collectBacktestResults({ messages: [{ role: 'assistant', id: 'plain', ts: 1, turn: { reply_text: JSON.stringify(result) } }] }).length, 0);
  assert.equal(html(reply('empty', { ok: true, message: 'Research done' })), '');
});

test('cards mount before the final-reply condition and in external calls too', () => {
  const source = fs.readFileSync(path.join(__dirname, '../components/chat/ChatMessage.tsx'), 'utf8');
  assert.ok(source.indexOf('<BacktestReplyCards message={msg} />') < source.indexOf('{reply ? <section'));
  const external = fs.readFileSync(path.join(__dirname, '../components/chat/ExternalCallMessage.tsx'), 'utf8');
  assert.match(external, /<BacktestReplyCards message=\{message\} \/>/);
});

test('saved strategy cards read streaming evidence and survive a later failure', () => {
  const { activeProposalsFromTurn } = require('../components/chat/TurnBlocks.tsx');
  const proposal={action:'strategy_draft_proposal',kind:'strategy_package_proposal',
    proposal_id:'prp_source',strategy_id:'source',state:'draft',files:['main.py'],validation:{ok:true}};
  const blocks=[{block:{kind:'tool_result',ok:true,action:'strategy_draft_proposal',result:proposal}},
    {block:{kind:'tool_result',ok:false,action:'strategy_backtest',error:'historical source unavailable'}}];
  assert.equal(activeProposalsFromTurn({blocks}).length,1);
  assert.equal(activeProposalsFromTurn({blocks}).at(0).id,'prp_source');
  const source=fs.readFileSync(path.join(__dirname,'../components/chat/ChatMessage.tsx'),'utf8');
  assert.match(source,/activeProposalsFromTurn\(\{[\s\S]*?blocks: sourceBlocks/);
  assert.doesNotMatch(source,/const proposals = !msg.loading/);
});

test('observation replay shows activity and never turns a flat account into performance', () => {
  const rendered = html(reply('observer', { ...result, title:'Observed event strategy', evaluation_mode:'observation',
    execution_mode:'agent', performance_evidence:false, provenance:{data_kind:'historical'},
    replay:{decisions:25, dispatches:7, skipped:18, errors:0, agent_execution:'not_run'},
    metrics_display:{total_return_pct:'0.00%', max_drawdown_pct:'0.00%'} }));
  assert.match(rendered, /Observed event strategy|Event &amp; dispatch replay/);
  assert.match(rendered, /data-metric="dispatches">7/);
  assert.match(rendered, /Agent model not executed/);
  assert.match(rendered, /View events and diagnostics/);
  assert.doesNotMatch(rendered, /0\.00%|Total return|Recorded backtest equity/);
});

test('the preview uses recorded values only and missing provenance stays unverified', () => {
  const rendered = html(reply('curve', { ...result, equity_preview:[{time:1,value:100},{time:2,value:102},{time:2,value:101.8}] }));
  assert.match(rendered, /Recorded backtest equity/);
  assert.match(rendered, /101\.8 USD/);
  assert.match(rendered, /Source not recorded/);
  assert.doesNotMatch(rendered, /Historical market data/);
});

test('CSV trade values are readable without losing tiny quantities or order identifiers', () => {
  const { BacktestTables } = require('../components/backtest/BacktestTables.tsx');
  const rendered = renderToStaticMarkup(React.createElement(NextIntlClientProvider, { locale:'zh', messages:{}, timeZone:'UTC' },
    React.createElement(BacktestTables, { tables:[{ id:'trades', columns:['qty','price','intent_id','ts'],
      rows:[['0.00000001','86378.64700099999','00001234','1790377200']] }] })));
  assert.match(rendered, /成交明细/);
  assert.match(rendered, />0\.00000001<\/span>/);
  assert.match(rendered, />86,378\.65<\/span>/);
  assert.match(rendered, /title="86378\.64700099999"/);
  assert.match(rendered, />00001234<\/span>/);
  assert.match(rendered, /2026-09-25 23:00:00/);
});

test('benchmark underperformance is a review warning, not a failed backtest', () => {
  // Legacy reports may have persisted FAIL solely because they trailed buy-and-hold.
  const rendered = html(reply('evaluated', { ...result, verdict:'FAIL', flags:['benchmark_capture_below_threshold'] }));
  assert.match(rendered, /Review limitations/);
  assert.match(rendered, /underperformed buy-and-hold/);
  assert.doesNotMatch(rendered, /failed|failure/i);
  assert.match(rendered, /View equity curve and trades/);
});

test('execution counters distinguish attempts, queued intents and settled fills', () => {
  const rendered = html(reply('orders', { ...result, replay: {
    order_attempts: 600, orders_submitted: 600, orders_filled: 205, orders_rejected: 395,
    sdk_errors: 0, forced_closes: 1, order_accounting_ok: true,
    rejection_reasons: { max_open_trades: 395 },
  } }));
  assert.match(rendered, /data-execution-metric="orders_filled">205/);
  assert.match(rendered, /data-execution-metric="orders_rejected">395/);
  assert.match(rendered, /Strategy ok is not a fill/);
  assert.match(rendered, /Liquidations: 1/);
});

test('legacy zero attempts do not fabricate missing fill counters', () => {
  const rendered = html(reply('legacy', { ...result, replay: { order_attempts: 0, status_counts: { ok: 12546 } } }));
  assert.match(rendered, /No order attempts were recorded/);
  assert.match(rendered, /data-execution-metric="orders_filled">—/);
  assert.doesNotMatch(rendered, /data-execution-metric="orders_filled">12546/);
  const current = html(reply('current', { ...result, replay: {
    execution_evidence_version: 1, order_attempts: 0, orders_submitted: 0,
    orders_filled: 0, orders_rejected: 0, sdk_errors: 0,
    reason_counts: { no_new_trade_signal: 167, confirmation_veto: 1 },
  } }));
  assert.doesNotMatch(current, /No order attempts were recorded|Older engines/);
  assert.match(current, /data-execution-metric="orders_filled">0/);
});

test('legacy multi-market benchmark warns without rewriting saved data', () => {
  const { BacktestExecutionEvidence } = require('../components/backtest/BacktestExecutionEvidence.tsx');
  const rendered = renderToStaticMarkup(React.createElement(NextIntlClientProvider, { locale:'zh', messages:{}, timeZone:'UTC' },
    React.createElement(BacktestExecutionEvidence, { replay: { order_attempts:0 }, legacyBenchmark:true })));
  assert.match(rendered, /旧版多品种基准/);
  assert.match(rendered, /勿将此报告的基准、超额收益作为有效评估依据/);
});

// Bounded fields from the 20261022_115816 ETH receipt. No runtime writes or full payload.
const scriptReceipt = { ...result, strategy_id:'eth_ema_agent_confirm', proposal_id:'prp_d1a59b77ca19', backtest_ts:'20261022_115816',
  result_type:'backtest_result', backtest_status:'completed', execution_mode:'script', evaluation_mode:'trading', performance_evidence:false,
  start_utc:'2026-09-22T11:00:00Z', end_utc:'2026-10-22T10:00:00Z', requested_window_complete:true,
  data_manifest:{ requested_window_complete:true, end_exclusive:true, requested_start_utc:'2026-09-22T11:00:00+00:00', requested_end_utc:'2026-10-22T11:00:00+00:00' },
  provenance:{ data_kind:'historical', assumptions:{requested_window_days:30,warmup_bars:50} },
  metrics:{decisions:'720',dispatches:'0',skipped:'688',errors:'0',backtest_days:'29.958333333333332'},
  metrics_display:{decisions:'720',dispatches:'0',skipped:'688',errors:'0',backtest_days:'29.958333333333332'},
  replay:{ decisions:720, dispatches:0, skipped:688, errors:0, scope:'mocked_inputs_code_replay_only',
    mock_surface_calls:[{surface:'subagents',mode:'stub',count:16}], order_attempts:32, orders_submitted:32,
    orders_filled:32, orders_rejected:0, forced_closes:0, sdk_errors:0, agent_execution:'not_run' } };

test('actual script receipt keeps a verified exclusive 30-day window and simulated execution evidence', () => {
  const rendered = html(reply('eth', scriptReceipt));
  assert.match(rendered, /Script replay \(simulated inputs\)/);
  assert.match(rendered, /data-coverage-status="complete"/);
  assert.ok(rendered.includes('Recorded span <strong>30</strong>'));
  assert.match(rendered, /data-execution-metric="order_attempts">32/);
  assert.match(rendered, /data-execution-metric="orders_filled">32/);
  assert.match(rendered, /Liquidations: 0/);
  assert.match(rendered, /not trading-performance evidence/);
  assert.match(rendered, /View simulated trades and market/);
  assert.doesNotMatch(rendered, /29.96|Incomplete coverage|rerun the strategy|Event &amp; dispatch|data-metric="dispatches"|Total return|Economic checks passed|Recorded backtest equity/);
  const chinese = html(reply('eth-zh',scriptReceipt), 'zh');
  assert.match(chinese, /脚本回放（模拟输入）/);
  assert.match(chinese, /不能作为交易绩效证据/);
  assert.match(chinese, /查看模拟成交与行情/);
});

test('raw nested metrics and sparse duplicates retain coverage and evidence without crossing run identities', () => {
  const nested = { ...result, metrics:{ ...scriptReceipt.metrics, tf:'1h', requested_window_days:30,
    requested_window_complete:true, start_utc:scriptReceipt.start_utc,end_utc:scriptReceipt.end_utc,
    execution_mode:'script',performance_evidence:false,replay:scriptReceipt.replay,provenance:scriptReceipt.provenance } };
  const message = reply('nested-script', nested);
  message.turn.tool_trace = [{action:'strategy_backtest',result}];
  const [projected] = collectBacktestResults({messages:[message]});
  assert.equal(projected.coverage.tf,'1h');
  assert.equal(projected.performanceEvidence,false);
  assert.equal(projected.replay.orders_filled,32);
  assert.match(html(message), /data-coverage-status="complete"/);
  const other = { ...result, backtest_ts:'20261022_120000' };
  const rows = collectBacktestResults({messages:[message,reply('other',other)]});
  assert.equal(rows[1].coverage.requested_window_complete,undefined);
  assert.equal(rows[1].performanceEvidence,undefined);
});

test('failed historical receipts cannot expose a completed result or old performance', () => {
  for (const failure of [{ok:false}, {backtest_status:'failed'}]) {
    const rendered = html(reply('failed-history',{...scriptReceipt,...failure,message:'Historical replay failed',metrics:{...scriptReceipt, total_return_pct:42}}));
    assert.match(rendered, /data-status="failed"/);
    assert.doesNotMatch(rendered, /data-status="completed"|data-coverage-status="complete"|data-execution-metric|Total return/);
  }
});

test('ordinary CEX performance and event dispatch remain separate from script stubs', () => {
  const { backtestEvidenceKind } = require('../lib/backtestEvidence.ts');
  assert.equal(backtestEvidenceKind({execution_mode:'script',performance_evidence:true}), 'trading');
  assert.equal(backtestEvidenceKind({execution_mode:'agent',performance_evidence:false}), 'observation');
  assert.equal(backtestEvidenceKind(scriptReceipt), 'script_simulated');
  assert.equal(backtestEvidenceKind({...scriptReceipt,performance_evidence:undefined}), 'script_simulated');
  const rendered = html(reply('cex',{...result,execution_mode:'script',performance_evidence:true,verdict:'PASS'}));
  assert.match(rendered, /Total return|Economic checks passed/);
  assert.doesNotMatch(rendered, /simulated inputs|Event &amp; dispatch replay/);
});

test('script detail keeps recorded trade rows and market candles without performance summary', () => {
  const { ScriptReplayReport } = require('../components/backtest/BacktestChart.tsx');
  const chart = { meta:{...scriptReceipt,tf:'1h',start:scriptReceipt.start_utc,end:scriptReceipt.end_utc},
    panels:[{id:'price',title:'ETH',type:'timeseries',series:[{kind:'candles',name:'ETH',data:[{time:1790000000,open:100,high:102,low:99,close:101}]}]}],
    tables:[{id:'trades',columns:['ts','side','qty','price','fee'],rows:[['1790000000','buy','0.1','100','0.02']]}],
    summary_cards:[{id:'total_return_pct',label:'Do not show performance',value:'42%'}] };
  for (const locale of ['en','zh']) {
    const rendered = renderToStaticMarkup(React.createElement(NextIntlClientProvider, {locale,messages:{},timeZone:'UTC'}, React.createElement(ScriptReplayReport,{chart})));
    assert.match(rendered, /data-report-kind="script_simulated"/);
    assert.match(rendered, /data-testid="backtest-trade-row"/);
    assert.match(rendered, /data-testid="backtest-loaded-range"/);
    assert.match(rendered, /data-coverage-status="complete"/);
    assert.doesNotMatch(rendered, /Do not show performance|42%|data-metric="dispatches"/);
  }
});

// Same producer projection used by chart.json and the native backtest receipt.
const retrospectiveLabel = 'current_model_retrospective_replay';
const decisionProof = {label:retrospectiveLabel,decision_receipts_verified:true,decision_count:16};
const retrospectiveReceipt = {...scriptReceipt,
  replay:{...scriptReceipt.replay,scope:retrospectiveLabel,agent_execution:retrospectiveLabel},
  provenance:{...scriptReceipt.provenance,decision_tape:decisionProof}};

test('nested host pool-price evidence renders quote-token simulation, source and recorded charts in en/zh', () => {
  const {backtestEvidenceKind} = require('../lib/backtestEvidence.ts');
  const {ScriptReplayReport} = require('../components/backtest/BacktestChart.tsx');
  const metrics = {execution_mode:'script',performance_evidence:false,accounting_currency:'USDC',
    initial_capital_usd:10000,final_equity_usd:10001,total_return_pct:0.01,
    replay:{scope:'historical_pool_price_simulation',strategy_execution_verified:true,
      order_attempts:1,orders_submitted:1,orders_filled:1,orders_rejected:0,sdk_errors:0},
    provenance:{data_kind:'historical',price_simulation:{scope:'historical_pool_price_simulation',
      execution_target:'jupiter:SOL/USDC',pool:{chain:'solana',protocol:'orca',pool_id:'fixture-pool',
        base:{address:'fixture-sol-address',symbol:'SOL',decimals:9},
        quote:{address:'fixture-usdc-address',symbol:'USDC',decimals:6}},
      quote_units:'USDC',price_units:'quote_token_per_base_token',volume_units:'upstream_reported_usd',
      sources:[{provider:'pool-history',dataset_id:'fixture-ohlcv'}],
      prepared_input_sha256:'fixture-input-hash'}}};
  const message = reply('pool-price',{...result,accounting_currency:'USDC',metrics,metrics_display:{}});
  // Actual compact producer shape: currency survives at top level while
  // provenance.price_simulation is omitted by the bounded projection.
  const compact = reply('compact-pool-price',{...result,execution_mode:'script',performance_evidence:false,
    accounting_currency:'USDC',replay:metrics.replay,metrics:{decisions:168},metrics_display:{},
    provenance:{data_kind:'historical',_truncated_keys:3}});
  assert.equal(collectBacktestResults({messages:[compact]})[0].metrics.accounting_currency,'USDC');
  const [projected] = collectBacktestResults({messages:[message]});
  assert.equal(backtestEvidenceKind(metrics),'pool_price_simulation');
  assert.equal(backtestEvidenceKind({...projected.metrics,replay:projected.replay,provenance:projected.provenance}),'pool_price_simulation');
  for (const replay of [{...metrics.replay,strategy_execution_verified:false},{...metrics.replay,strategy_execution_verified:undefined}]) {
    assert.equal(backtestEvidenceKind({...metrics,replay}),'script_simulated');
  }
  assert.notEqual(backtestEvidenceKind({...metrics,performance_evidence:true}),'pool_price_simulation');
  const chart = {meta:metrics,summary_cards:[{id:'total_return_pct',label:'Hidden performance KPI',value:'0.01%'}],panels:[
    {id:'equity',type:'line',title:'Equity vs B&H',series:[{kind:'line',name:'equity',data:[{time:1790000000,value:10000},{time:1790003600,value:10001}]},
      {kind:'line',name:'benchmark',data:[{time:1790000000,value:10000}]}]},
    {id:'price',type:'candlestick',title:'Pool SOL/USDC',series:[{kind:'candles',name:'SOL/USDC',data:[{time:1790000000,open:100,high:102,low:99,close:101}]}]}],
    tables:[{id:'trades',columns:['ts','side','qty','price','fee'],rows:[['1790000000','buy','0.1','100','0.02']]}]};
  const before = JSON.stringify({message,chart});
  for (const locale of ['en','zh']) {
    const compactCard = html(compact,locale);
    assert.match(compactCard,/data-evaluation="pool_price_simulation"/);
    assert.match(compactCard,/data-testid="pool-price-units">[^<]*: USDC\./);
    const card = html(message,locale);
    const detail = renderToStaticMarkup(React.createElement(NextIntlClientProvider,
      {locale,messages:{},timeZone:'UTC'},React.createElement(ScriptReplayReport,{chart})));
    assert.match(card,/data-evaluation="pool_price_simulation"/);
    assert.match(card,/data-execution-metric="orders_filled">1/);
    assert.doesNotMatch(card,/data-metric=|backtest-factor-library|<figcaption>/);
    assert.match(detail,/data-report-kind="pool_price_simulation"/);
    assert.match(detail,/data-simulation-panel="equity"/);
    assert.match(detail,/data-candle-count="1"/);
    assert.match(detail,/data-testid="backtest-trade-row"/);
    assert.ok(detail.includes('(USDC)'));
    for (const rendered of [card,detail]) {
      assert.match(rendered,locale==='en'?/Historical pool-price simulation/:/历史池价模拟/);
      assert.match(rendered,locale==='en'?/not historical Jupiter route execution/:/不代表历史 Jupiter 路由执行/);
      assert.match(rendered,locale==='en'?/no USD peg or live USD conversion/:/不假设其与美元挂钩/);
      assert.match(rendered,/data-testid="pool-price-units"[^>]*>[\s\S]*?USDC/);
      assert.doesNotMatch(rendered,/fixture-pool|fixture-ohlcv|fixture-sol-address|fixture-usdc-address|prepared_input_sha256/);
      assert.doesNotMatch(rendered,/Agent stubs|Agent 桩响应|Script replay \(simulated inputs\)|脚本回放（模拟输入）|Hidden performance KPI|Buy &amp; Hold|10,000(?:\.00)? USD/);
    }
  }
  assert.equal(JSON.stringify({message,chart}),before);
});

test('script and retrospective reports render recorded equity and drawdown without inferred benchmarks', () => {
  const {ScriptReplayReport} = require('../components/backtest/BacktestChart.tsx');
  for (const meta of [scriptReceipt, retrospectiveReceipt]) for (const locale of ['en', 'zh']) {
    const chart = {meta, summary_cards:[], tables:[], panels:[
      {id:'equity',title:'Producer equity',type:'timeseries',series:[
        {kind:'line',name:'Recorded simulated balance',data:[{time:1790000000,value:100},{time:1790003600,value:98}]},
        {kind:'line',name:'Buy & Hold',data:[{time:1790000000,value:100},{time:1790003600,value:105}]}]},
      {id:'drawdown',title:'Producer drawdown',type:'timeseries',series:[
        {kind:'area',name:'Recorded simulated drawdown',data:[{time:1790000000,value:0},{time:1790003600,value:-2}]}]},
    ]};
    const before = JSON.stringify(chart);
    const rendered = renderToStaticMarkup(React.createElement(NextIntlClientProvider,
      {locale,messages:{},timeZone:'UTC'},React.createElement(ScriptReplayReport,{chart})));
    assert.match(rendered,/data-simulation-panel="equity"/);
    assert.match(rendered,/data-simulation-panel="drawdown"/);
    assert.equal((rendered.match(/data-testid="backtest-chart"/g)||[]).length,2);
    assert.match(rendered,locale === 'zh' ? /模拟资金曲线/ : /Simulated balance curve/);
    assert.doesNotMatch(rendered,/Buy &amp; Hold|Economic checks passed|Total return/);
    assert.equal(JSON.stringify(chart),before);
    const empty = renderToStaticMarkup(React.createElement(NextIntlClientProvider,
      {locale,messages:{},timeZone:'UTC'},React.createElement(ScriptReplayReport,{chart:{...chart,panels:[]}})));
    assert.doesNotMatch(empty,/data-simulation-panel|data-testid="backtest-chart"/);
  }
});

test('retrospective classification requires producer identity and verified nonempty decision receipts', () => {
  const {backtestEvidenceKind} = require('../lib/backtestEvidence.ts');
  const original = JSON.stringify(retrospectiveReceipt);
  assert.equal(backtestEvidenceKind(retrospectiveReceipt),'retrospective_replay');
  assert.equal(backtestEvidenceKind({...retrospectiveReceipt,execution_mode:'agent'}),'retrospective_replay');
  assert.equal(backtestEvidenceKind({...retrospectiveReceipt,provenance:{},
    decision_replay:{...decisionProof,model_decisions_verified:true,performance_evidence:false}}),'retrospective_replay');
  for (const proof of [undefined,null,[],true,{},
    {...decisionProof,label:'other'}, {...decisionProof,decision_receipts_verified:false},
    {...decisionProof,decision_receipts_verified:'true'}, {...decisionProof,model_decisions_verified:false},
    ...[0,-1,1.5,'16',NaN,Infinity].map(decision_count => ({...decisionProof,decision_count}))]) {
    const receipt = {...retrospectiveReceipt,provenance:{decision_tape:proof}};
    assert.equal(backtestEvidenceKind(receipt),'script_simulated');
    assert.equal(backtestEvidenceKind({...receipt,execution_mode:'agent'}),'observation');
  }
  assert.equal(backtestEvidenceKind({...retrospectiveReceipt,replay:scriptReceipt.replay}),'script_simulated');
  assert.equal(backtestEvidenceKind({...retrospectiveReceipt,kind:'freeform_backtest'}),'custom_research');
  assert.equal(backtestEvidenceKind({...retrospectiveReceipt,kind:'rate_policy_replay'}),'rate_simulation');
  assert.notEqual(backtestEvidenceKind({...retrospectiveReceipt,performance_evidence:true}),'retrospective_replay');
  assert.equal(JSON.stringify(retrospectiveReceipt),original);
});

test('verified retrospective chat receipts and nested metrics keep historical simulation labels in both locales', () => {
  for (const receipt of [retrospectiveReceipt, {...result,metrics:{...retrospectiveReceipt,
    decision_replay:{...decisionProof,model_decisions_verified:true}}}]) {
    const message = reply('retrospective',receipt);
    assert.equal(collectBacktestResults({messages:[message]})[0].performanceEvidence,false);
    for (const locale of ['en','zh']) {
      const rendered = html(message,locale);
      assert.match(rendered,/data-evaluation="retrospective_replay"/);
      assert.ok(rendered.includes(locale === 'en' ? 'Current-model retrospective replay (historical simulation)' : '追溯模型回放（历史模拟）'));
      assert.match(rendered,locale === 'en' ? /View historical simulation/ : /查看历史模拟/);
      assert.match(rendered,locale === 'en' ? /not contemporaneous decisions/ : /并非历史当时的决策/);
      assert.match(rendered,locale === 'en' ? /not live execution or trading-performance evidence/ : /不代表实盘执行，也不构成交易绩效证据/);
      assert.match(rendered,/performance_evidence=false/);
      assert.match(rendered,/data-execution-metric="orders_filled">32/);
      assert.doesNotMatch(rendered,/Agent stubs|Agent 桩响应|does not prove real Agent decisions|data-metric="total_return_pct"|Economic checks passed|backtest-factor-library|Recorded backtest equity/);
    }
  }
});

test('bounded producer retrospective receipt stays distinct from script stubs in cards and chart details', () => {
  const producerShape = {
    ok: true, result_type: 'backtest_result', backtest_status: 'completed',
    strategy_id: 'btc_ema_trend_agent', proposal_id: 'prp_65eb85296c74', backtest_ts: '20261030_153558',
    execution_mode: 'script', evaluation_mode: 'trading', performance_evidence: false,
    replay: { scope: 'current_model_retrospective_replay', agent_execution: 'current_model_retrospective_replay', decision_count: 13,
      mock_surface_calls: [{ surface: 'subagents', mode: 'retrospective', count: 13 }], orders_filled: 20 },
    decision_replay: { label: 'current_model_retrospective_replay', decision_receipts_verified: true, model_decisions_verified: true, decision_count: 13, performance_evidence: false },
    provenance: { data_kind: 'historical', decision_tape: { label: 'current_model_retrospective_replay', decision_receipts_verified: true, decision_count: 13, tape_sha256: 'bounded-tape-digest' } },
    metrics_display: { total_return_pct: -8.4568, max_drawdown_pct: 9.2318, sharpe_ratio: -5.0247, total_trades: 20 },
  };
  const { backtestEvidenceKind } = require('../lib/backtestEvidence.ts');
  assert.equal(backtestEvidenceKind(producerShape), 'retrospective_replay');
  assert.equal(backtestEvidenceKind({ ...producerShape, decision_replay: { label: 'current_model_retrospective_replay' } }), 'retrospective_replay');
  const message = reply('bounded-producer-retrospective', producerShape);
  const [projected] = collectBacktestResults({ messages: [message] });
  assert.equal(projected.decisionReplay.decision_count, 13);
  assert.equal(backtestEvidenceKind({ ...projected.metrics, decision_replay: projected.decisionReplay, execution_mode: projected.executionMode, performance_evidence: projected.performanceEvidence, replay: projected.replay, provenance: {} }), 'retrospective_replay');
  for (const locale of ['en', 'zh']) {
    const rendered = html(message, locale);
    assert.match(rendered, /data-evaluation="retrospective_replay"/);
    assert.match(rendered, locale === 'en' ? /Current-model retrospective replay/ : /追溯模型回放（历史模拟）/);
    assert.match(rendered, locale === 'en' ? /not contemporaneous decisions/ : /并非历史当时的决策/);
    assert.match(rendered, /performance_evidence=false/);
    assert.doesNotMatch(rendered, /Script replay|脚本回放|Agent stubs|Agent 桩响应/);
  }
  const { ScriptReplayReport } = require('../components/backtest/BacktestChart.tsx');
  const detail = renderToStaticMarkup(React.createElement(NextIntlClientProvider, { locale: 'zh', messages: {}, timeZone: 'UTC' },
    React.createElement(ScriptReplayReport, { chart: { meta: producerShape, panels: [], tables: [], summary_cards: [] } })));
  assert.match(detail, /data-report-kind="retrospective_replay"/);
  assert.match(detail, /追溯模型回放（历史模拟）/);
  assert.doesNotMatch(detail, /脚本回放|Agent 桩响应/);
});

test('retrospective detail retains simulated trades while replacing script stub claims', () => {
  const {ScriptReplayReport} = require('../components/backtest/BacktestChart.tsx');
  const chart = {meta:{...retrospectiveReceipt,tf:'1h',start:scriptReceipt.start_utc,end:scriptReceipt.end_utc},
    panels:[],tables:[{id:'trades',columns:['ts','side','qty','price','fee'],rows:[['1790000000','buy','0.1','100','0.02']]}],
    summary_cards:[{id:'total_return_pct',label:'Do not show performance',value:'42%'}]};
  for (const locale of ['en','zh']) {
    const rendered = renderToStaticMarkup(React.createElement(NextIntlClientProvider,
      {locale,messages:{},timeZone:'UTC'},React.createElement(ScriptReplayReport,{chart})));
    assert.match(rendered,/data-report-kind="retrospective_replay"/);
    assert.match(rendered,locale === 'en' ? /Current-model retrospective replay/ : /追溯模型回放/);
    assert.match(rendered,/performance_evidence=false/);
    assert.match(rendered,/data-testid="backtest-trade-row"/);
    assert.match(rendered,/data-coverage-status="complete"/);
    assert.doesNotMatch(rendered,/Agent stubs|Agent 桩响应|Do not show performance|42%/);
  }
  assert.equal(chart.meta.performance_evidence,false);
});

const customReceipt = { ok:true, kind:'freeform_backtest', replay_kind:'strategy_sdk_freeform',
  strategy_id:'custom_calculation', proposal_id:'prp_custom', backtest_ts:'20261023_120000',
  performance_evidence:false, coverage_ok:null, requested_window_complete:null,
  provenance:{data_kind:'historical_observations'},
  replay:{scope:'isolated_custom_calculation',process_exited:true,strategy_execution_verified:false,errors:0},
};

test('custom calculation identity takes precedence over performance and script dispatch fallbacks', () => {
  const { backtestEvidenceKind } = require('../lib/backtestEvidence.ts');
  for (const identity of [
    {kind:'freeform_backtest'}, {backtest_kind:'freeform_backtest'},
    {replay_kind:'strategy_sdk_freeform'}, {replay:customReceipt.replay},
  ]) {
    assert.equal(backtestEvidenceKind({...identity,performance_evidence:false}), 'custom_research');
    assert.equal(backtestEvidenceKind({...identity,execution_mode:'script',evaluation_mode:'observation',performance_evidence:false}), 'custom_research');
  }
});

test('raw custom receipts show localized research limitations without invented Agent counters', () => {
  for (const locale of ['en','zh']) for (const dataKind of ['historical_observations','unverified']) {
    const rendered = html(reply('custom',{...customReceipt,provenance:{data_kind:dataKind}}),locale);
    assert.match(rendered, /data-evaluation="custom_research"/);
    assert.match(rendered, /Custom research calculation|自定义研究计算/);
    assert.match(rendered, /not verified historical trading performance|不构成已验证的历史交易绩效/);
    assert.match(rendered, /View calculation outputs|查看计算输出/);
    assert.match(rendered, dataKind === 'historical_observations' ? /Historical observations|历史观测数据/ : /Unverified data|数据待核实/);
    assert.doesNotMatch(rendered, /data-metric=|data-execution-metric=|Native strategy engine|原生策略引擎|Economic checks passed|Replay checks passed|Agent model not executed|Event &amp; dispatch|Historical market data|Extract &amp; reuse factors/);
  }
});

test('custom null coverage stays unknown and recorded gaps survive sparse receipts', () => {
  for (const complete of [null,false]) {
    const receipt = {...customReceipt,requested_window_complete:complete,coverage_ok:complete,
      metrics:{backtest_kind:'freeform_backtest',backtest_days:30,total_return_pct:12.5,sharpe_ratio:null},
      equity_preview:[{time:1,value:100},{time:2,value:112.5}],
      coverage_message:complete === false ? 'Prepared observations have coverage gaps.' : ''};
    const message = reply('custom-coverage',receipt);
    message.turn.tool_trace = [{action:'strategy_backtest',result:{ok:true,strategy_id:receipt.strategy_id,proposal_id:receipt.proposal_id,backtest_ts:receipt.backtest_ts}}];
    const [projected] = collectBacktestResults({messages:[message]});
    assert.equal(projected.coverage.requested_window_complete,complete);
    assert.equal(projected.metrics.sharpe_ratio,null);
    const rendered = html(message);
    assert.match(rendered,new RegExp(`data-coverage-status="${complete === false ? 'partial' : 'unknown'}"`));
    assert.match(rendered, /Recorded calculation outputs/);
    assert.match(rendered, /12.5|112.5/);
    assert.match(rendered, /Raw calculation curve/);
    if (complete === false) assert.match(rendered, /coverage gaps/);
    assert.doesNotMatch(rendered, /data-metric=|data-execution-metric=|includes fees|Economic checks passed|Window verified/);
  }
});

test('custom detail shows curves and trades while deferring raw diagnostics', () => {
  const { CustomResearchReport } = require('../components/backtest/BacktestChart.tsx');
  const chart = {meta:{...customReceipt,backtest_days:30},
    panels:[{id:'equity',title:'Recorded custom curve',type:'timeseries',series:[{kind:'line',name:'Custom equity',data:[{time:1,value:100},{time:2,value:112.5}]}]}],
    tables:[{id:'trades',columns:['qty','price'],rows:[['0.1','100']]}],
    summary_cards:[{label:'total_return_pct',value:12.5},{label:'sharpe_ratio',value:null},
      ...['decisions','dispatches','skipped','errors'].map(label => ({label,value:0}))]};
  const original = JSON.stringify(chart);
  for (const locale of ['en','zh']) {
    const rendered = renderToStaticMarkup(React.createElement(NextIntlClientProvider, {locale,messages:{},timeZone:'UTC'},React.createElement(CustomResearchReport,{chart})));
    assert.match(rendered, /data-report-kind="custom_research"/);
    assert.match(rendered, /Custom research calculation|自定义研究计算/);
    assert.match(rendered, /Recorded custom curve/);
    assert.match(rendered, /data-testid="backtest-chart"/);
    assert.match(rendered, /0.1/);
    assert.doesNotMatch(rendered, /<pre|12.5|null/);
    assert.match(rendered, /data-coverage-status="unknown"/);
    assert.doesNotMatch(rendered, /decisions|dispatches|skipped|errors|data-execution-metric=|data-testid="backtest-metrics"|data-metric=|Strategy ok is not a fill|Historical event and branch|Economic checks passed/);
  }
  assert.equal(JSON.stringify(chart),original);
});

test('empty chart panels render localized empty state without mounting a chart', () => {
  const { CustomResearchReport } = require('../components/backtest/BacktestChart.tsx');
  const rendered = renderToStaticMarkup(React.createElement(NextIntlClientProvider, {
    locale: 'en', messages: { pageCommon: { noData: 'No data' } }, timeZone: 'UTC',
  }, React.createElement(CustomResearchReport, { chart: {
    meta: { ...customReceipt }, panels: [
      { id: 'equity', title: 'Empty equity', type: 'timeseries', series: [{ kind: 'line', data: [] }] },
      { id: 'signals', title: 'Signals', type: 'timeseries', series: [{ kind: 'markers', data: [{ time: 1 }] }] },
    ], tables: [], summary_cards: [],
  } })));
  assert.equal((rendered.match(/data-testid="backtest-chart"/g) || []).length, 1);
  assert.match(rendered, /No data/);
});

test('custom display counters are ignored while recorded zero values and null diagnostics remain', () => {
  const { customResearchDiagnostics } = require('../lib/backtestEvidence.ts');
  const metrics_display = {decisions:'0',dispatches:'0',skipped:'0',errors:'0',
    total_return_pct:'0.00%',total_trades:0,sharpe_ratio:null,non_finite:Infinity};
  assert.deepEqual(customResearchDiagnostics(metrics_display),{total_return_pct:'0.00%',total_trades:0,sharpe_ratio:null});
  const rendered = html(reply('custom-default-counters',{...customReceipt,metrics_display}));
  assert.match(rendered,/0.00%/);
  assert.match(rendered,/sharpe_ratio/);
  assert.doesNotMatch(rendered,/decisions|dispatches|skipped|errors|data-metric=|data-execution-metric=/);
});
