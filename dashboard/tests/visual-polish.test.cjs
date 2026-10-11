// Offline rendering fixtures: no service, browser, timers or model calls.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { NextIntlClientProvider } = require('next-intl');

function compile(filename) {
  return ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText;
}
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => module._compile(compile(filename), filename);
require.extensions['.css'] = module => { module.exports = new Proxy({}, { get: (_, key) => key === '__esModule' ? false : String(key) }); };
const { en, zh } = require('../messages');

// Inject only the component's hook state and unrelated shell services. Child
// markup still renders through React, Radix and the real translation bundle.
function component(file, states = [], locale = 'en') {
  const filename = path.resolve(__dirname, '../components/chat', file);
  const instance = new Module(filename, module);
  instance.filename = filename;
  instance.paths = Module._nodeModulePaths(path.dirname(filename));
  const localRequire = instance.require.bind(instance);
  let index = 0;
  instance.require = request => {
    if (request === 'react') return { ...React, useEffect() {}, useState(initial) { return [index < states.length ? states[index++] : initial, () => {}]; } };
    if (request === 'next-intl') return { ...localRequire(request), useLocale: () => locale };
    if (request === 'next/navigation') return { usePathname: () => '/chat/task-1' };
    if (request === './ChatHistoryActions') return { ConversationActions: () => null };
    if (request === '../shell/ShellNavigationTrigger') return { ShellNavigationTrigger: () => null };
    if (request === '../shell/ShellNotifications') return { ShellNotifications: () => null };
    return localRequire(request);
  };
  instance._compile(compile(filename), filename);
  return instance.exports;
}
function render(Component, props = {}, locale = 'en') {
  return renderToStaticMarkup(React.createElement(NextIntlClientProvider, { locale, messages: locale === 'zh' ? zh : en, timeZone: 'UTC' }, React.createElement(Component, props)));
}
const status = (extra = {}) => ({ execution: 'running', waiting_for: null, completion: null, validation: 'unknown', needs_attention: false, external: false, ...extra });
const { shortTaskStatus } = require('../components/chat/taskStatusCopy.ts');

test('compact status preserves the waiting owner ahead of an old external completion', () => {
  for (const [waiting_for, english, chinese] of [['user', 'Awaiting reply', '等待回答'], ['approval', 'Awaiting approval', '等待审批'], ['configuration', 'Setup needed', '等待配置']]) {
    const value = status({ waiting_for, completion: 'external_reported' });
    assert.equal(shortTaskStatus(value, false), english);
    assert.equal(shortTaskStatus(value, true), chinese);
  }
  assert.equal(shortTaskStatus(status({ completion: 'external_reported' }), false), 'Reported complete');
  assert.equal(shortTaskStatus(status({ execution: 'unconfirmed' }), false), 'Unconfirmed');
  assert.equal(shortTaskStatus(status({ execution: 'failed' }), true), '执行失败');
});

test('first list failure shows only its error; loading and a successful empty response remain distinct', () => {
  for (const [error, loading, expected, absent] of [
    [true, false, 'Unable to refresh', ['No chats yet', 'Loading chats']],
    [false, true, 'Loading chats', ['Unable to refresh', 'No chats yet']],
    [false, false, 'No chats yet', ['Unable to refresh', 'Loading chats']],
  ]) {
    const { TaskList } = component('TaskList.tsx', [false, [], error, loading]);
    const html = render(TaskList);
    assert.ok(html.includes(expected));
    for (const text of absent) assert.ok(!html.includes(text));
  }
});

test('a cached task keeps its title and navigation when refresh fails', () => {
  const title = 'Research '.repeat(60);
  const row = { session_id: 'task-1', title, workbench_status: status({ waiting_for: 'approval', needs_attention: true }) };
  const { TaskList } = component('TaskList.tsx', [false, [row], true, false]);
  const html = render(TaskList);
  assert.match(html, /Unable to refresh/);
  assert.doesNotMatch(html, /No chats yet/);
  assert.ok(html.includes('href="/chat/task-1"'));
  assert.match(html, /class="taskTitle"/);
  assert.ok(html.includes(title));
  assert.doesNotMatch(html, /class="taskStatus"/);
  assert.doesNotMatch(html, /class="sr-only"/);
});

test('header exposes the waiting label, loading and stale idle state accurately', () => {
  const { ChatTaskHeader } = component('ChatTaskHeader.tsx');
  const props = { thread: { id: 'task-1', title: 'Research', messages: [] }, agents: [], results: [], sending: false, approvalCount: 1, showTabs: true, tab: '', canvasVisible: false, onSelect() {}, onToggleCanvas() {}, onOpenResult() {}, workStatus: status({ completion: 'external_reported' }) };
  assert.ok(render(ChatTaskHeader, props).includes('class="statusLabel">Awaiting approval</span>'));
  assert.ok(render(ChatTaskHeader, { ...props, approvalCount: 0, loading: true }).includes('class="statusLabel">Loading</span>'));
  const offline = render(ChatTaskHeader, { ...props, approvalCount: 0, connection: 'offline', workStatus: status({ execution: 'idle' }) });
  assert.match(offline, /data-idle="false"/);
  assert.match(offline, /Ready · May be out of date/);
});

test('skill failures never report loaded or ready in either language', () => {
  for (const locale of ['en', 'zh']) {
    const { SkillLoadCard } = component('tool-cards/SkillLoadCard.tsx', [], locale);
    for (const failure of [{ ok: false }, { ok: true, error: 'Fixture error' }]) {
      const html = render(SkillLoadCard, { block: { action: 'Skill', payload: { skill: 'research' }, ...failure }, variant: 'result' }, locale);
      assert.ok(html.includes(locale === 'zh' ? 'Skill 加载失败' : 'Skill failed to load'));
      assert.ok(!html.includes((locale === 'zh' ? zh : en).skillLoadCard.loaded));
      assert.ok(!html.includes('>' + (locale === 'zh' ? zh : en).skillLoadCard.ready + '<'));
    }
    assert.ok(render(SkillLoadCard, { block: { ok: true }, variant: 'result' }, locale).includes((locale === 'zh' ? zh : en).skillLoadCard.loaded));
    assert.ok(render(SkillLoadCard, { block: {}, variant: 'use', pending: true }, locale).includes((locale === 'zh' ? zh : en).skillLoadCard.loading));
  }
});

test('dock shows an explicit return label when compact or expanded', () => {
  for (const locale of ['en', 'zh']) {
    const { TaskDockHeader } = component('TaskDockHeader.tsx', [], locale);
    const props = { tabs: [], choices: [], selected: '', onSelect() {}, onRemove() {}, onToggleSize() {}, onClose() {}, expanded: false };
    const label = locale === 'zh' ? '返回对话' : 'Back to chat';
    assert.ok(!render(TaskDockHeader, props, locale).includes('>' + label + '</span>'));
    for (const mode of [{ compact: true }, { expanded: true }]) assert.ok(render(TaskDockHeader, { ...props, ...mode }, locale).includes('>' + label + '</span>'));
  }
});
