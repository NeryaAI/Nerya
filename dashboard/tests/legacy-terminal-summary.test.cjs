const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, f);
require.extensions['.css'] = m => { m.exports = {}; };
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { NextIntlClientProvider } = require('next-intl');
const { finalReplyText, publicReplyText, collectChatResults, legacyReplyDiagnostic, terminalReplyDiagnostic, withoutLegacyTerminalText } = require('../lib/chatResults.ts');
const { legacyTerminalSummary } = require('../lib/legacyTerminalSummary.ts');
const { AssistantBubble } = require('../components/chat/ChatMessage.tsx');
const { AssistantReplyBubble } = require('../components/chat/AssistantReplyBubble.tsx');
const { ChatResultsPanel } = require('../components/chat/ChatResultsPanel.tsx');
const { ConversationContext } = require('../components/chat/ConversationContext.tsx');
const messages = Object.assign({}, ...fs.readdirSync(__dirname + '/../messages/zh').filter(f => f.endsWith('.json')).map(f => require('../messages/zh/' + f)));
const html = element => renderToStaticMarkup(React.createElement(NextIntlClientProvider, { locale: 'zh', messages, timeZone: 'UTC' }, element));
const legacy = '本轮任务未完成：工具调用重复且未取得进展。\n执行情况：12 步，9 次工具调用，其中 3 次报错。\n错误详情：tool_use_error: failed\n此前生成的内容仅供参考，本轮没有获得可靠的完整结果。\n- 已有证据：data_api ok:{"rows":[{"value":123}],"source":"fixture"}\n- 已有证据：tool_use_error: failed\n已执行的工具调用不会自动重放；恢复前请核对已完成的写入或交易操作。';
const exception = '本轮 Agent 在运行过程中异常退出，未能生成最终回复。\n\n错误类型: LLMError\n错误信息: tool_use_error: failed';
const healthy = '任务尚未完成。数据已经取得，但后续验证未通过。请先查看验证记录，再决定是否继续。';
const env = text => ({ role: 'assistant', block: { kind: 'text', text } });
const message = (body = legacy, turn = {}, extra = {}) => ({ role: 'assistant', id: 'legacy', ts: 1, turn: { reply_text: body, blocks: [env(body)], budget: { aborted: true, abort_reason: 'repeated_tool_call' }, ...turn }, ...extra });
const results = msg => collectChatResults({ title: 'Request', messages: [msg] });
const raw = /已有证据|data_api|tool_use_error|执行情况|错误类型|错误信息/;

test('all public projections replace only the old aborted host wrapper and retain the source', () => {
  const msg = message();
  const before = JSON.stringify(msg);
  const reply = finalReplyText(msg);
  assert.doesNotMatch(reply, raw);
  assert.match(reply, /未完成.*重复/);
  assert.equal(publicReplyText(msg, msg.turn.blocks), reply);
  assert.equal(results(msg)[0].text, reply);
  assert.equal(JSON.stringify(msg), before);
});

test('legacy block-only and error turns cannot fall back to the raw wrapper', () => {
  for (const body of [legacy, exception]) for (const error of [undefined, 'tool_use_error: failed']) {
    const msg = message(body, { reply_text: undefined }, { error, execution_status: 'failed' });
    const reply = finalReplyText(msg);
    assert.ok(reply);
    assert.doesNotMatch(reply, raw);
    assert.equal(publicReplyText(msg, msg.turn.blocks), reply);
    assert.equal(results(msg)[0].text, reply);
  }
});

test('machine termination metadata is required, including command-only legacy turns', () => {
  assert.equal(finalReplyText(message(legacy, { budget: {} })), legacy);
  assert.equal(finalReplyText(message(legacy, { budget: {}, stopped_reason: 'end_turn', transition_reason: 'completed' })), legacy);
  for (const turn of [{ budget: { aborted: true } }, { budget: { abort_reason: 'timeout' } }, { budget: {}, abort_reason: 'timeout' }, { budget: {}, stopped_reason: 'timeout' }, { budget: {}, transition_reason: 'runtime_error' }, { budget: {}, execution_status: 'failed' }]) {
    assert.doesNotMatch(finalReplyText(message(legacy, turn)), raw);
  }
  assert.doesNotMatch(finalReplyText(message(legacy, { budget: {} }, { execution_status: 'cancelled' })), raw);
  assert.equal(finalReplyText(message(legacy, { budget: {} }, { error: 'timeout' })), '');
});

test('healthy prose, quoted diagnostics, code, and structured reports are unchanged', () => {
  const fence = String.fromCharCode(96).repeat(3);
  for (const body of [healthy, '已有证据：data_api ok:{"rows":[]}', '本轮任务未完成：需要你选择数据范围。', '> ' + legacy.replaceAll('\n', '\n> '), fence + 'text\n' + legacy + '\n' + fence, '用户贴出的错误如下：\n' + legacy, '# 验证报告\n\n已有证据：数据完整。\n\n' + fence + 'json\n{"ok":true}\n' + fence, JSON.stringify({ status: 'passed', report: legacy })]) {
    const msg = message(body);
    assert.equal(finalReplyText(msg), body);
    assert.equal(publicReplyText(msg, msg.turn.blocks), body);
    assert.equal(results(msg)[0].text, body);
  }
  const msg = message(healthy, { blocks: [env(legacy)] });
  assert.equal(publicReplyText(msg, msg.turn.blocks), healthy);
  assert.equal(finalReplyText({ ...msg, error: 'runtime error' }), healthy);
  assert.equal(finalReplyText(message(legacy, {}, { loading: true })), '');
  assert.equal(publicReplyText(message(legacy, { external_call: {} }), [env(legacy)]), '');
});

test('mixed legacy abort suffix requires durable counts and a complete host footer', () => {
  const body = 'Partial model narrative that did not finish.\n\n' + legacy;
  const budget = { aborted: true, abort_reason: 'repeated_tool_call', iterations: 12, tool_calls: 9 };
  const msg = message(body, { budget });
  const before = JSON.stringify(msg);
  assert.doesNotMatch(finalReplyText(msg), raw);
  assert.equal(legacyReplyDiagnostic(msg), body);
  assert.equal(JSON.stringify(msg), before);
  for (const altered of [{ ...budget, iterations: 13 }, { ...budget, tool_calls: 8 }, { ...budget, aborted: false }]) {
    assert.equal(finalReplyText(message(body, { budget: altered })), body);
  }
  const fence = String.fromCharCode(96).repeat(3);
  for (const quoted of ['> quoted\n\n' + body, fence + 'text\n' + body + '\n' + fence, body.slice(0, -10)]) {
    assert.equal(finalReplyText(message(quoted, { budget })), quoted);
  }
});

test('stop reasons produce truthful bilingual status, cause and next action without retry promises', () => {
  const cases = [
    ['cancelled:operator_cancel', /已取消/, /cancelled/],
    ['cancelled:command_control_unavailable', /系统.*停止/, /system stopped/],
    ['cancelled:command_lease_lost', /系统.*停止/, /system stopped/],
    ['cancelled:event_persistence_failed', /系统.*停止/, /system stopped/],
    ['task_poll_no_progress', /后台任务.*停止等待/, /background task/],
    ['repeated_tool_call', /重复.*进展/, /Repeated actions/],
    ['token_budget_exceeded', /长度或用量限制/, /length or usage limit/],
    ['interrupted_max_tokens', /长度或用量限制/, /length or usage limit/],
    ['interrupted_length', /长度或用量限制/, /length or usage limit/],
    ['model_output_truncated', /长度或用量限制/, /length or usage limit/],
    ['timeout', /超时/, /timed out/],
    ['stream_interrupted', /模型响应中断/, /response was interrupted/],
    ['stream_interrupted: insufficient_quota', /额度或请求限制/, /quota or request limit/],
    ['LLMError: insufficient quota', /额度或请求限制/, /quota or request limit/],
    ['unknown_host_reason', /异常中止/, /stopped unexpectedly/],
  ];
  for (const [reason, zh, en] of cases) {
    const msg = message(legacy, { budget: { aborted: true, abort_reason: reason } });
    for (const [locale, match] of [[true, zh], [false, en]]) {
      const summary = legacyTerminalSummary(msg, legacy, locale);
      assert.match(summary, match);
      assert.doesNotMatch(summary, raw);
      assert.doesNotMatch(summary, /自动重试|will retry|automatically retry/);
      assert.ok(summary.length < (locale ? 110 : 260));
    }
  }
  const quota = message(exception.replace('tool_use_error: failed', 'insufficient_quota: service limit'), { budget: { aborted: true, abort_reason: 'stream_interrupted' } });
  assert.match(finalReplyText(quota), /额度或请求限制/);
});

test('multi-block rendering never resurfaces a superseded host summary in the execution trace', () => {
  for (const body of [legacy, healthy]) {
    const msg = message(body, { blocks: [env(legacy), env('Intermediate progress'),
      { block: { kind: 'tool_result', call_id: 'fixture', action: 'read_file', ok: false, error: 'Fixture failure' } }, env(body)] });
    const output = html(React.createElement(AssistantBubble, { msg }));
    assert.match(output, /data-testid="execution-timeline"/);
    assert.doesNotMatch(output, raw);
    assert.doesNotMatch(output, /全部完成/);
    assert.equal(legacyReplyDiagnostic(msg), legacy);
    assert.equal(withoutLegacyTerminalText(msg, msg.turn.blocks).some(row => row.block.kind === 'tool_result'), true);
  }
});

test('existing turn details expose the original legacy diagnostic and its copy action', async () => {
  const originalState = React.useState;
  const originalNavigator = Object.getOwnPropertyDescriptor(global, 'navigator');
  let tree, copied;
  // Open the existing dialog without a browser; inspect its real React subtree.
  React.useState = initial => originalState(initial === false ? true : initial);
  function Capture() { tree = ConversationContext({ message: message() }); return tree; }
  function find(node, predicate) {
    if (!node || typeof node !== 'object') return null;
    if (predicate(node)) return node;
    for (const child of React.Children.toArray(node.props?.children)) {
      const match = find(child, predicate);
      if (match) return match;
    }
    return null;
  }
  try {
    html(React.createElement(Capture));
    const diagnostic = find(tree, node => node.props?.['data-testid'] === 'legacy-terminal-diagnostic');
    assert.ok(diagnostic);
    assert.equal(find(diagnostic, node => node.type === 'pre').props.children, legacy);
    const output = renderToStaticMarkup(diagnostic);
    assert.match(output, /已有证据/);
    assert.match(output, /tool_use_error/);
    Object.defineProperty(global, 'navigator', { configurable: true, value: { clipboard: { writeText: async text => { copied = text; } } } });
    await find(diagnostic, node => node.type === 'button').props.onClick();
    assert.equal(copied, legacy);
  } finally {
    React.useState = originalState;
    if (originalNavigator) Object.defineProperty(global, 'navigator', originalNavigator);
    else delete global.navigator;
  }
});

test('warning and error role text is not promoted to a public result', () => {
  for (const role of ['user', 'warning', 'error', 'tool']) {
    const blocks = [{ ...env(legacy), role }];
    const msg = message('', { blocks });
    assert.equal(finalReplyText(msg), '');
    assert.equal(publicReplyText(msg, blocks), '');
    assert.deepEqual(results(msg), []);
  }
});

test('new clean final keeps separate host stream diagnostics available', () => {
  const detail = 'transport closed diagnostic';
  const msg = message(healthy, {blocks: [env(healthy), {block: {kind: 'model_transport', status: 'interrupted', detail}}]});
  assert.equal(finalReplyText(msg), healthy);
  assert.equal(terminalReplyDiagnostic(msg), detail);
  assert.equal(terminalReplyDiagnostic({...msg, loading: true}), '');
  assert.equal(terminalReplyDiagnostic(message(healthy)), '');
});

test('React assistant, task timeline bubble, and result panel show one concise conclusion', () => {
  for (const body of [legacy, exception, healthy]) for (const error of [undefined, 'tool_use_error: failed']) {
    if (body === healthy && error) continue;
    const msg = message(body, {}, { error, execution_status: 'failed' });
    const reply = finalReplyText(msg);
    for (const element of [React.createElement(AssistantReplyBubble, { message: msg }), React.createElement(AssistantBubble, { msg }), React.createElement(ChatResultsPanel, { results: results(msg) })]) {
      const output = html(element);
      assert.ok(output.includes(reply), output);
      assert.doesNotMatch(output, raw);
      assert.equal(output.split(reply).length - 1, 1);
    }
    const task = html(React.createElement(AssistantBubble, { msg }));
    assert.match(task, /data-testid="turn-context"/);
    if (error) assert.match(task, /role="alert"/);
  }
});
