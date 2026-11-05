const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { NextIntlClientProvider } = require('next-intl');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, f);
require.extensions['.css'] = m => { m.exports = {}; };

const legacy = '本轮任务未完成：工具调用重复且未取得进展。\n执行情况：12 步，9 次工具调用，其中 3 次报错。\n错误详情：tool_use_error: ' + 'raw payload '.repeat(150)
  + '\n此前生成的内容仅供参考，本轮没有获得可靠的完整结果。\n- 已有证据：data_api ok:{"rows":[{"value":123}],"source":"fixture"}';
const exception = '本轮 Agent 在运行过程中异常退出，未能生成最终回复。\n\n错误类型: LLMError\n错误信息: tool_use_error: {"rows":[]}';
const healthy = '任务尚未完成。数据已经取得，但后续验证未通过。请先查看验证记录，再决定是否继续。';
const compact = text => text.replace(/\s+/g, ' ').slice(0, 1200) + (text.length > 1200 ? '…' : '');
const row = (summary = compact(legacy), state = 'failed') => ({
  command_id: 'exact2', task_id: 'task-exact2', task_session_id: 'taskchat-exact2',
  title: 'Aave research', summary, state, created_at: 123,
});
const raw = /已有证据|data_api|tool_use_error|执行情况|错误类型|错误信息|raw payload|&quot;rows&quot;/;

// Real list rendering with fixed hook data, matching assistant-task-delivery.test.cjs.
function component(rows = []) {
  const filename = path.resolve(__dirname, '../components/shell/AssistantResults.tsx');
  const instance = new Module(filename, module);
  instance.filename = filename;
  instance.paths = Module._nodeModulePaths(path.dirname(filename));
  const localRequire = instance.require.bind(instance);
  instance.require = request => request === 'react' ? { ...React, useEffect() {}, useState(initial) {
    return [initial && typeof initial === 'object' && 'rows' in initial
      ? { assistant: 'main', rows, error: false, loading: false } : initial, () => {}];
  } } : localRequire(request);
  require.extensions['.tsx'](instance, filename);
  return instance.exports;
}
function renderList(rows, locale = 'zh') {
  const { AssistantResults } = component(rows);
  return renderToStaticMarkup(React.createElement(NextIntlClientProvider, {
    locale, messages: require('../messages/' + locale + '/assistants.json'), timeZone: 'UTC',
  }, React.createElement(AssistantResults, { assistantId: 'main' })));
}
const command = (result, state = 'failed') => ({ state, result, turn_id: 'turn-exact2',
  delivery: { completion: { allowed: false, status: 'incomplete' } } });

test('Aave result separates finished execution from incomplete delivery above unchanged authored claims', () => {
  const fixture = require('../../tests/fixtures/assistant_result_aave_incomplete.json');
  const value = {...fixture, created_at: 123, task_complete: false,
    delivery_completion: {allowed: false, status: 'partial'},
    unresolved_deliveries: [{command_id: fixture.command_id, reasons: fixture.delivery.completion.reasons}]};
  const before = JSON.stringify(value);
  for (const locale of ['en', 'zh']) {
    const html = renderList([value], locale);
    const label = locale === 'en' ? 'Delivery incomplete' : '交付未完成';
    assert.ok(html.includes(label), html);
    assert.ok(html.includes(locale === 'en' ? 'Turn finished' : '本轮已结束'));
    assert.ok(html.indexOf(label) < html.indexOf('完成总结'));
    assert.ok(html.includes('✅ 已提交待审草案'));
    assert.match(html, /data-delivery-state="incomplete"/);
    assert.doesNotMatch(html, /Delivery complete|交付已完成/);
  }
  assert.equal(component().assistantResultPreview(value, command({reply_text: value.summary}, 'succeeded')).text, value.summary);
  assert.equal(JSON.stringify(value), before);
});

test('list distinguishes allowed partial and complete delivery from absent or blocked evidence', () => {
  const base = row('Authored report stays visible.', 'succeeded');
  const values = [
    {...base, command_id: 'legacy'},
    {...base, command_id: 'partial', task_complete: false, delivery: {command_id: 'partial',
      completion: {allowed: true, status: 'partial'}}, delivery_completion: {allowed: false, status: 'partial'},
      unresolved_deliveries: [{command_id: 'partial', reasons: ['scope_limited']}]},
    {...base, command_id: 'complete', task_complete: true, delivery: {completion: {allowed: true, status: 'complete'}}},
    {...base, command_id: 'later', task_complete: false, delivery: {completion: {allowed: true, status: 'complete'}},
      unresolved_deliveries: [{command_id: 'earlier', reasons: ['check_failed:coverage']}]},
    {...base, command_id: 'later-partial', task_complete: false, delivery: {command_id: 'later-partial',
      completion: {allowed: true, status: 'partial'}},
      unresolved_deliveries: [{command_id: 'earlier', reasons: ['check_failed:coverage']}]},
  ];
  const before = JSON.stringify(values);
  for (const locale of ['en', 'zh']) {
    const html = renderList(values, locale);
    const buttons = html.match(new RegExp('<button[^>]*data-testid="assistant-result".*?</button>', 'gs'));
    for (const [i, status] of ['unconfirmed', 'partial', 'complete', 'incomplete', 'incomplete'].entries()) {
      assert.ok(buttons[i].includes(require('../messages/' + locale + '/assistants.json').assistants.deliveryStates[status]));
      assert.ok(buttons[i].includes(base.summary));
    }
  }
  assert.equal(JSON.stringify(values), before);
});

test('actual failed list hides truncated old aborted and exception host payloads', () => {
  const rows = [row(), { ...row(compact(exception)), command_id: 'exception' },
    { ...row(compact('Now I will create the report. ' + legacy.replace('12 步，9 次工具调用，其中 3 次报错', '13 步，20 次工具调用')), 'interrupted'), command_id: 'mixed' }];
  assert.ok(!rows[0].summary.includes('此前生成的内容'), 'fixture must lack the full-body closing marker');
  const before = JSON.stringify(rows);
  for (const locale of ['zh', 'en']) {
    const output = renderList(rows, locale);
    assert.equal((output.match(/data-testid="assistant-result"/g) || []).length, 3);
    assert.doesNotMatch(output, raw);
    assert.match(output, locale === 'zh' ? /任务未完成/ : /task is incomplete/);
  }
  assert.equal(JSON.stringify(rows), before);
});

test('list changes exactly the two host payloads; prose, quotes, code and success reports survive', () => {
  const { assistantResultSummary } = component();
  const fence = String.fromCharCode(96).repeat(3);
  const rows = [row(), row(compact(exception)), ...[healthy,
    '已有证据：data_api ok:{"rows":[]}', '本轮任务未完成：需要你选择数据范围。',
    '> ' + legacy.replaceAll('\n', '\n> '), fence + 'text\n' + legacy + '\n' + fence,
    '用户贴出的错误如下：\n' + legacy, '# 验证报告\n\n' + exception,
    JSON.stringify({ status: 'passed', report: legacy }),
    compact('> ' + legacy), compact(fence + 'text\n' + legacy + '\n' + fence),
    compact('用户贴出的错误如下：\n' + legacy), compact('“' + legacy + '”'),
    compact('The quoted output is: "' + legacy + '"'),
  ].map(body => row(body)), row(legacy, 'succeeded'), row(legacy, 'running'), row(legacy, 'custom_state')];
  const before = JSON.stringify(rows);
  const projected = rows.map(value => assistantResultSummary(value));
  assert.equal(projected.filter((text, i) => text !== rows[i].summary).length, 2);
  for (const text of projected.slice(0, 2)) { assert.doesNotMatch(text, raw); assert.ok(text.length < 120); }
  assert.deepEqual(projected.slice(2), rows.slice(2).map(value => value.summary));
  assert.equal(JSON.stringify(rows), before);
  assert.ok(renderList([row(healthy)]).includes(healthy));
});

test('compact list recognizes mixed host payloads and requires exact host markers and machine state', () => {
  const { assistantResultSummary } = component();
  for (const state of ['failed', 'interrupted', 'blocked']) for (const body of [legacy, exception]) {
    for (const summary of [body, compact(body), compact('Now I will create the report. ' + body)]) {
      assert.doesNotMatch(assistantResultSummary(row(summary, state)), raw);
    }
  }
  for (const summary of [
    '本轮任务未完成：需要你确认。正常正文包含执行情况，但没有主机统计。',
    '本轮任务未完成：需要你确认。 执行情况：13 步，20 次工具',
    '本轮 Agent 在运行过程中异常退出，未能生成最终回复。后续是用户解释。',
    'Normal prose '.repeat(110) + compact(legacy),
  ]) assert.equal(assistantResultSummary(row(summary)), summary);
  const mixed = compact('Now I will create the report. ' + legacy);
  assert.equal(assistantResultSummary(row(mixed, 'succeeded')), mixed);
});

test('selected preview uses command state for old reply, final and block-only bodies', () => {
  const { assistantResultPreview } = component();
  const { finalReplyText, legacyReplyDiagnostic } = require('../lib/chatResults.ts');
  for (const body of [legacy, exception]) for (const field of ['reply_text', 'final_text', 'blocks']) {
    const result = { [field]: field === 'blocks' ? [{ role: 'assistant', block: { kind: 'text', text: body } }] : body,
      artifact_index: { files: [{ path: 'report.md' }] }, verifier_outcome: { status: 'failed' }, execution_state: { status: 'failed' } };
    const selected = row('stale list summary', 'succeeded');
    const source = command(result);
    const before = JSON.stringify({ selected, source });
    const preview = assistantResultPreview(selected, source);
    const message = { id: selected.command_id, role: 'assistant', ts: 123000, execution_status: source.state, turn: result };
    assert.equal(preview.text, finalReplyText(message));
    assert.ok(preview.text);
    assert.doesNotMatch(preview.text, raw);
    assert.equal(legacyReplyDiagnostic(message), body, 'full diagnostics remain available to turn details');
    assert.equal(preview.id, 'exact2');
    assert.equal(preview.ts, 123000);
    assert.equal(preview.turnId, 'turn-exact2');
    assert.equal(preview.taskSessionId, 'taskchat-exact2');
    assert.equal(preview.delivery, source.delivery);
    assert.equal(preview.evidence.artifacts, result.artifact_index);
    assert.equal(preview.evidence.verifier, result.verifier_outcome);
    assert.equal(preview.evidence.execution, result.execution_state);
    assert.equal(JSON.stringify({ selected, source }), before);
  }
});

test('selected preview keeps modern summary and quoted report bodies unchanged', () => {
  const { assistantResultPreview } = component();
  const fence = String.fromCharCode(96).repeat(3);
  for (const body of [healthy, '> ' + legacy.replaceAll('\n', '\n> '),
    fence + 'text\n' + legacy + '\n' + fence, 'Report findings\n' + legacy]) {
    assert.equal(assistantResultPreview(row(), command({ reply_text: body })).text, body);
  }
  assert.equal(assistantResultPreview(row(), command({ reply_text: legacy }, 'succeeded')).text, legacy);
});

test('selected mixed preview delegates counter-bound suffix handling to the shared final formatter', () => {
  const { assistantResultPreview } = component();
  const { finalReplyText, legacyReplyDiagnostic } = require('../lib/chatResults.ts');
  const body = 'Now I will create the report.\n\n' + legacy.replace('12 步，9 次工具调用，其中 3 次报错', '8 步，10 次工具调用')
    + '\n已执行的工具调用不会自动重放；恢复前请核对已完成的写入或交易操作。';
  const result = { reply_text: body, budget: { iterations: 8, tool_calls: 10, aborted: true, abort_reason: 'stream_interrupted' } };
  const source = command(result, 'interrupted');
  const before = JSON.stringify(source);
  const message = { id: 'exact2', role: 'assistant', ts: 123000, execution_status: source.state, turn: result };
  const preview = assistantResultPreview(row(compact(body), 'interrupted'), source);
  assert.equal(preview.text, finalReplyText(message));
  assert.match(preview.text, /模型响应中断/);
  assert.doesNotMatch(preview.text, raw);
  assert.equal(legacyReplyDiagnostic(message), body);
  assert.equal(JSON.stringify(source), before);
});

test('selected preview fallback is safe when a legacy result has no final body', () => {
  const { assistantResultPreview, assistantResultSummary } = component();
  for (const result of [null, {}, { reply_text: ' ', final_text: '' }]) {
    const selected = row();
    assert.equal(assistantResultPreview(selected, command(result)).text, assistantResultSummary(selected));
    assert.doesNotMatch(assistantResultPreview(selected, command(result)).text, raw);
  }
  assert.equal(assistantResultPreview(row(healthy), command(null)).text, healthy);
});
