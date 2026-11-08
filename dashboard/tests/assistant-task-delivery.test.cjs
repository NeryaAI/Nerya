const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { NextIntlClientProvider } = require('next-intl');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, filename);
require.extensions['.css'] = module => { module.exports = {}; };
const { assistantDeliveryFields, projectAssistantTaskMessage } = require('../lib/assistantTaskDelivery.ts');
const { collectChatResults } = require('../lib/chatResults.ts');
const { AssistantTaskReceipt } = require('../components/chat/AssistantTaskReceipt.tsx');
const { assistantTaskReceipts, assistantCreationReply, assistantFollowupReply } = require('../lib/assistantTaskReceipts.ts');
const { AssistantReplyBubble } = require('../components/chat/AssistantReplyBubble.tsx');
const task = {task_id:'awork_test',task_session_id:'taskchat_test',title:'Research',state:'succeeded'};
const message = (content, receipt) => ({id:'assistant_result:test',role:'assistant',ts:1,execution_status:'succeeded',turn:{reply_text:content},assistant_task:receipt});
const legacy = '**Research** · 已完成 / Completed\n\nAgent findings.\n\n[查看任务对话 / Open task conversation](/chat/taskchat_test)';

test('host publication keeps earlier delivery blockers above unchanged model claims', () => {
  const meta = { source: 'assistant_task_result', task_complete: false,
    delivery_completion: {allowed:false,status:'unavailable'},
    unresolved_deliveries:[{command_id:'old-command',delivery_id:'old-delivery',delivery_revision:16,reasons:['check_pending:reported']}] };
  const receipt = {...task, ...assistantDeliveryFields(meta)};
  const claim = 'Task fully completed. 任务已完整交付。';
  const value = message(claim, receipt);
  const before = JSON.stringify(value);
  for (const locale of ['en', 'zh']) {
    const output = render(AssistantReplyBubble, {message:value}, locale);
    const label = locale === 'en' ? 'Delivery is not confirmed' : '交付尚未确认';
    assert.ok(output.includes(label));
    assert.ok(output.indexOf(label) < output.indexOf(claim));
    assert.ok(output.includes('check_pending:reported'));
    assert.ok(output.includes(claim));
    assert.equal((output.match(/assistant-delivery-warning/g)||[]).length,1);
  }
  assert.equal(JSON.stringify(value), before);
  assert.equal(assistantDeliveryFields({...meta,task_complete:true,delivery_completion:{allowed:true,status:'complete'}}).task_complete,false);
  assert.deepEqual(assistantDeliveryFields({...meta,source:'model'}),{});
  assert.deepEqual(assistantDeliveryFields({source:'assistant_task_result'}),{});
});

test('replayed delivery metadata renders each old record and its gaps once', () => {
  const old = {command_id: 'old-command', delivery_id: 'old-delivery', delivery_revision: 16,
    reasons: ['check_pending:reported', 'check_pending:reported', 'check_missing:coverage']};
  const other = {...old, command_id: 'other-command', delivery_id: 'other-delivery',
    reasons: ['check_pending:reported']};
  const meta = {source: 'assistant_task_result', task_complete: false,
    delivery_completion: {allowed: false, status: 'unavailable'},
    unresolved_deliveries: [old, {...old}, other]};
  const before = JSON.stringify(meta);
  const fields = assistantDeliveryFields(meta);
  assert.deepEqual(fields.unresolved_deliveries, [
    {...old, reasons: ['check_pending:reported', 'check_missing:coverage']}, other,
  ]);
  const revision = {...old, delivery_revision: 17, reasons: ['check_stale:reported']};
  assert.deepEqual(assistantDeliveryFields({...meta, unresolved_deliveries: [
    old, {...old, reasons: ['check_failed:validation']}, revision,
  ]}).unresolved_deliveries, [
    {...old, reasons: ['check_pending:reported', 'check_missing:coverage', 'check_failed:validation']}, revision,
  ]);
  assert.equal(assistantDeliveryFields({...meta, unresolved_deliveries: [
    ...Array(16).fill(old), other,
  ]}).unresolved_deliveries.length, 2, 'replays must not consume the distinct-record limit');
  assert.deepEqual(assistantDeliveryFields({...meta, ...fields}), fields);
  for (const locale of ['en', 'zh']) {
    const archived = message('Original findings.', {...task, ...meta});
    const current = {...task, ...meta, command_id: 'test'};
    for (const props of [{message: archived}, {message: message('Original findings.', task), tasks: [current]}]) {
      const output = render(AssistantReplyBubble, props, locale);
      assert.equal((output.match(/check_missing:coverage/g) || []).length, 1);
      // A different delivery can independently have the same unmet check.
      assert.equal((output.match(/check_pending:reported/g) || []).length, 2);
      assert.equal((output.match(/assistant-delivery-warning/g) || []).length, 1);
      assert.ok(output.includes('Original findings.'));
    }
  }
  assert.equal(JSON.stringify(meta), before);
});

function render(Component, props, locale) {
  return renderToStaticMarkup(React.createElement(NextIntlClientProvider, {
    locale, messages: require('../messages/' + locale + '/assistants.json'), timeZone: 'UTC',
  }, React.createElement(Component, props)));
}

// Local dependency injection follows visual-polish.test.cjs: render real
// components and translations with fixed hook data, without network or a browser.
function component(file, overrides = {}) {
  const filename = path.resolve(__dirname, '../components', file);
  const instance = new Module(filename, module);
  instance.filename = filename;
  instance.paths = Module._nodeModulePaths(path.dirname(filename));
  const localRequire = instance.require.bind(instance);
  instance.require = request => Object.hasOwn(overrides, request) ? overrides[request] : localRequire(request);
  require.extensions['.tsx'](instance, filename);
  return instance.exports;
}

test('creation receipts survive native typed JSON, streaming and reload without final prose',()=>{
 const receipt={ok:true,protocol:'nerya.assistant_task.v2',...task,state:'queued'};
 const value={id:'create',role:'assistant',ts:1,loading:true,live_events:[{seq:1,kind:'tool.complete',call_id:'c',action:'assistant_task_create',ok:true,result:JSON.stringify(receipt),display_result:{content:[{type:'json',data:receipt}]}}]};
 assert.deepEqual(assistantTaskReceipts(value),[{...task,state:'queued'}]);
 const saved={...value,loading:false,live_events:[],turn:{blocks:[{block:{kind:'tool_result',call_id:'c',action:'assistant_task_create',ok:true,result:JSON.stringify(receipt)}}]}};
 assert.deepEqual(assistantTaskReceipts(saved),assistantTaskReceipts(value));
 saved.turn.blocks[0].block.ok=false;assert.deepEqual(assistantTaskReceipts(saved),[]);
});

const verifiedCreation = {
  ...task, state: 'queued', ok: true, protocol: 'nerya.assistant_task.v2',
  publication_protocol: 'nerya.assistant_request.v1', operation: 'task_create',
  source: 'assistant_work+agent_commands', verified: true, admission_confirmed: true,
  command_id: 'acmd_test', binding: {command_id: 'parent_command'},
  url: '/chat/taskchat_test', task_complete: false,
  delivery_completion: {allowed: false, status: 'unavailable'}, unresolved_deliveries: [],
  verification: {hard_status: 'missing'}, limitations: [], reported_result: null,
};
const creationCopy = {
  zh: '已创建任务并确认命令已受理。\n\n[Research](/chat/taskchat_test)\n\n状态: `queued` · command: `acmd_test`\n\n任务交付完成尚未确认。\n\n已记录检查：`missing`',
  en: 'Task created; command admission confirmed.\n\n[Research](/chat/taskchat_test)\n\nState: `queued` · command: `acmd_test`\n\nTask delivery completion is not confirmed.\n\nRecorded checks: `missing`',
};
function creationMessage(copy, receipt = verifiedCreation) {
  return {id: 'creation', role: 'assistant', ts: 1, command_id: 'parent_command',
    turn: {reply_text: copy, blocks: [{block: {kind: 'tool_result', action: 'assistant_task_create',
      ok: true, result: receipt}}]}};
}

const followupBinding = {command_id: 'parent_followup', session_id: 'parent_session',
  turn_id: 'parent_turn', actor_id: 'local', input_digest: 'input_digest'};
const verifiedFollowup = {...verifiedCreation, protocol: 'nerya.assistant_receipt.v1', operation: 'task_follow_up',
  command_id: 'atmsg_followup', parent_session_id: 'parent_session', binding: followupBinding};
const followupCopy = {
  en: creationCopy.en.replace('Task created; command admission confirmed.', 'Follow-up request admitted.').replace('acmd_test', 'atmsg_followup'),
  zh: creationCopy.zh.replace('已创建任务并确认命令已受理。', '补充请求已受理。').replace('acmd_test', 'atmsg_followup'),
};
function followupMessage(copy, receipt = verifiedFollowup) {
  return {id: 'followup', role: 'assistant', ts: 1, command_id: 'parent_followup',
    turn: {reply_text: copy, command_id: 'parent_followup', session_id: 'parent_session', turn_id: 'parent_turn',
      assistant_request: {protocol: 'nerya.assistant_request.v1', disposition: 'task_follow_up',
        target_task_id: task.task_id, binding: followupBinding},
      assistant_receipt: receipt, publication: {source: 'host', receipt_verified: true}}};
}

test('host-verified follow-up renders localized admission and one live card on the same task', () => {
  for (const copy of Object.values(followupCopy)) for (const locale of ['en', 'zh']) {
    const value = followupMessage(copy);
    const before = JSON.stringify(value);
    const html = render(AssistantReplyBubble, {message: value, tasks: [{...task, state: 'running'}]}, locale);
    const labels = require('../messages/' + locale + '/assistants.json').assistants;
    assert.ok(html.includes(locale === 'en' ? 'Follow-up admitted.' : '补充请求已受理。'), html);
    assert.ok(html.includes('<span role="status">' + labels.taskStates.running + '</span>'));
    assert.doesNotMatch(html, /queued|atmsg_followup|missing|Recorded checks|已记录检查|assistant-task-created/);
    assert.equal(html.split('href="/chat/taskchat_test"').length - 1, 1);
    assert.equal((html.match(/data-testid="assistant-task-receipt"/g) || []).length, 1);
    assert.equal(JSON.stringify(value), before);
  }
});

test('verified follow-up streaming and saved tool blocks share the same receipt without a new creation', () => {
  const event = {seq: 1, kind: 'tool.complete', call_id: 'followup', action: 'assistant_task_message', ok: true,
    display_result: {content: [{type: 'json', data: verifiedFollowup}]}};
  const streaming = {id: 'stream-followup', role: 'assistant', ts: 1, command_id: 'parent_followup', loading: true,
    live_events: [event]};
  const saved = {id: 'saved-followup', role: 'assistant', ts: 1, command_id: 'parent_followup',
    turn: {reply_text: followupCopy.zh, blocks: [{block: {kind: 'tool_result', action: 'assistant_task_message',
      ok: true, result: JSON.stringify(verifiedFollowup)}}]}};
  for (const value of [streaming, saved]) {
    const receipts = assistantTaskReceipts(value);
    assert.equal(receipts.length, 1);
    assert.equal(receipts[0].task_id, task.task_id);
    assert.equal(receipts[0].task_complete, false);
    const html = render(AssistantReplyBubble, {message: value, tasks: [{...task, state: 'unconfirmed'}]}, 'en');
    assert.ok(html.includes('Follow-up admitted.'));
    assert.ok(html.includes('Needs recovery'));
    assert.doesNotMatch(html, /Task created|typing-dot|queued|atmsg_followup|missing/);
  }
  const wrongTarget = render(AssistantReplyBubble, {message: saved,
    tasks: [{...task, task_session_id: 'taskchat_other', state: 'succeeded'}]}, 'en');
  assert.ok(wrongTarget.includes('Queued'));
  assert.ok(!wrongTarget.includes('Turn finished'));
});

test('follow-up formatting requires host authority, matching operation and the current request binding', () => {
  const invalidReceipts = [
    {verified: false}, {ok: false}, {source: 'model'}, {protocol: 'nerya.assistant_task.v2'},
    {publication_protocol: 'other'}, {admission_confirmed: false}, {operation: 'task_read'},
    {task_id: 'awork_other'}, {task_session_id: 'taskchat_other'}, {task_id: '../other'},
    {command_id: 'acmd_creation'}, {url: '/chat/taskchat_other'}, {parent_session_id: 'wrong_parent'},
    ...Object.keys(followupBinding).map(key => ({binding: {...followupBinding, [key]: 'wrong_' + key}})),
    {binding: {command_id: 'parent_followup'}},
  ];
  for (const override of invalidReceipts) {
    const value = followupMessage(followupCopy.en, {...verifiedFollowup, ...override});
    assert.equal(assistantFollowupReply(value, followupCopy.en), null, JSON.stringify(override));
    assert.deepEqual(assistantTaskReceipts(value), [], JSON.stringify(override));
  }
  for (const change of [
    {publication: {source: 'model', receipt_verified: true}},
    {publication: {source: 'host', receipt_verified: false}},
    {command_id: 'different'}, {session_id: 'different'}, {turn_id: 'different'},
    {assistant_request: {...followupMessage('').turn.assistant_request, target_task_id: 'awork_other'}},
  ]) {
    const value = followupMessage(followupCopy.en);
    Object.assign(value.turn, change);
    assert.equal(assistantFollowupReply(value, followupCopy.en), null, JSON.stringify(change));
    assert.deepEqual(assistantTaskReceipts(value), []);
  }
  const unbound = followupMessage(followupCopy.en);
  delete unbound.command_id;
  delete unbound.turn.command_id;
  assert.equal(assistantFollowupReply(unbound, followupCopy.en), null);
});

test('host-verified reads remain reads and do not produce admission or creation UI', () => {
  const copy = followupCopy.en.replace('Follow-up request admitted.', 'Current task state retrieved.');
  const read = {...verifiedFollowup, operation: 'task_read', reported_result: {
    authority: 'untrusted_model_output', text: 'Original result quote.', truncated: false,
  }};
  const saved = followupMessage(copy, read);
  saved.turn.assistant_request.disposition = 'task_read';
  const tool = {id: 'read', role: 'assistant', ts: 1, command_id: 'parent_followup',
    turn: {reply_text: copy, blocks: [{block: {kind: 'tool_result', action: 'assistant_task_read', ok: true, result: read}}]}};
  for (const value of [saved, tool]) {
    assert.equal(assistantCreationReply(value, copy), null);
    assert.equal(assistantFollowupReply(value, copy), null);
    assert.deepEqual(assistantTaskReceipts(value), []);
    const html = render(AssistantReplyBubble, {message: value}, 'en');
    assert.ok(html.includes('Current task state retrieved.'));
    assert.doesNotMatch(html, /assistant-task-created|assistant-task-followup-admitted|assistant-task-receipt/);
  }
});

test('follow-up formatting preserves prose, limitations and archived delivery results', () => {
  const fence = String.fromCharCode(96).repeat(3);
  for (const copy of ['A poem about queued commands and missing receipts.',
    'Quoted host receipt:\n\n' + followupCopy.en, fence + 'text\n' + followupCopy.zh + '\n' + fence]) {
    const value = followupMessage(copy);
    assert.equal(assistantFollowupReply(value, copy), null);
  }
  const ordinary = {id: 'ordinary-followup', role: 'assistant', ts: 1, turn: {reply_text: followupCopy.en}};
  assert.equal(assistantFollowupReply(ordinary, followupCopy.en), null);
  const suffix = 'Limitations: Partial coverage.\n\nOriginal model prose about queued work.';
  const unresolved = [{command_id: 'old', delivery_id: 'delivery_old', delivery_revision: 1, reasons: ['check_failed:coverage']}];
  const value = followupMessage(followupCopy.en + '\n\n' + suffix,
    {...verifiedFollowup, unresolved_deliveries: unresolved});
  assert.equal(assistantFollowupReply(value, value.turn.reply_text), suffix);
  const tasks = [{...task, state: 'succeeded'}];
  const html = render(AssistantReplyBubble, {message: value, tasks}, 'en');
  assert.ok(html.includes('Partial coverage.'));
  assert.ok(html.includes('Original model prose about queued work.'));
  assert.ok(html.includes('check_failed:coverage'));
  for (const state of ['failed', 'unconfirmed', 'succeeded']) {
    const archived = message('Original archived result.', {...task, state, task_complete: false, unresolved_deliveries: unresolved});
    Object.assign(archived.turn, value.turn, {reply_text: 'Original archived result.'});
    const output = render(AssistantReplyBubble, {message: archived, tasks}, 'en');
    const labels = require('../messages/en/assistants.json').assistants;
    assert.ok(output.includes('<span role="status">' + labels.taskStates[state] + '</span>'));
    assert.ok(output.includes('Original archived result.'));
    assert.ok(output.includes('check_failed:coverage'));
    assert.doesNotMatch(output, /Follow-up admitted\.|Task created\./);
  }
});

test('tool actions cannot relabel read receipts or unverified follow-up acknowledgements as writes', () => {
  for (const [action, receipt] of [
    ['assistant_task_create', {...verifiedCreation, operation: 'task_read'}],
    ['assistant_task_message', {...verifiedFollowup, verified: false}],
    ['assistant_task_message', {...verifiedFollowup, operation: 'task_read'}],
    ['assistant_task_read', verifiedFollowup],
    ['assistant_task_message', {ok: true, task_id: task.task_id, task_session_id: task.task_session_id,
      command_id: 'atmsg_followup', state: 'queued', delivery: 'follow_up'}],
  ]) {
    const value = {id: 'unverified', role: 'assistant', ts: 1, command_id: 'parent_followup',
      turn: {blocks: [{block: {kind: 'tool_result', action, ok: true, result: receipt}}]}};
    assert.deepEqual(assistantTaskReceipts(value), []);
    assert.equal(assistantFollowupReply(value, ''), null);
    assert.equal(assistantCreationReply(value, ''), null);
  }
});

test('old verified creation copy becomes localized acknowledgement with one current recovery state', () => {
  for (const copy of Object.values(creationCopy)) for (const locale of ['en', 'zh']) {
    const value = creationMessage(copy);
    const before = JSON.stringify(value);
    const html = render(AssistantReplyBubble, {message: value,
      tasks: [{...task, kind: 'background', state: 'unconfirmed'}]}, locale);
    const labels = require('../messages/' + locale + '/assistants.json').assistants;
    assert.ok(html.includes(locale === 'en' ? 'Task created.' : '已创建任务。'));
    assert.ok(html.includes('<span role="status">' + labels.taskStates.unconfirmed + '</span>'));
    assert.doesNotMatch(html, /queued|acmd_test|missing|Recorded checks|已记录检查/);
    assert.equal(html.split('href="/chat/taskchat_test"').length - 1, 1);
    assert.equal(html.split('Research').length - 1, 1);
    assert.equal(JSON.stringify(value), before);
  }
});

test('verified streaming creation and host-metadata-only reload share the concise receipt', () => {
  const event = {seq: 1, kind: 'tool.complete', call_id: 'c', action: 'assistant_task_create', ok: true,
    display_result: {content: [{type: 'json', data: verifiedCreation}]}};
  const streaming = {id: 'stream', role: 'assistant', ts: 1, loading: true, live_events: [event]};
  const saved = {id: 'reload', role: 'assistant', ts: 1, turn: {reply_text: creationCopy.zh,
    assistant_receipt: verifiedCreation, publication: {source: 'host', receipt_verified: true}}};
  for (const value of [streaming, saved]) {
    const html = render(AssistantReplyBubble, {message: value, tasks: [{...task, state: 'unconfirmed'}]}, 'en');
    assert.ok(html.includes('Task created.'));
    assert.ok(html.includes('Needs recovery'));
    assert.doesNotMatch(html, /typing-dot|queued|acmd_test|missing/);
    assert.equal(html.split('href="/chat/taskchat_test"').length - 1, 1);
  }
});

test('creation projection requires verified provenance and preserves ordinary and creative prose', () => {
  const prose = ['A poem about queued commands. 已记录检查：missing',
    '```text\n' + creationCopy.zh + '\n```', 'Quoted receipt:\n\n' + creationCopy.en];
  for (const copy of prose) {
    assert.equal(assistantCreationReply(creationMessage(copy), copy), null);
  }
  for (const override of [{verified: false}, {source: 'model'}, {operation: 'task_read'},
    {publication_protocol: 'other'}, {admission_confirmed: false}, {ok: false},
    {task_id: '../other'}, {task_session_id: '../other'}, {binding: {command_id: 'different'}}]) {
    assert.equal(assistantCreationReply(creationMessage(creationCopy.en, {...verifiedCreation, ...override}), creationCopy.en), null);
  }
  const ordinary = {id: 'ordinary', role: 'assistant', ts: 1, turn: {reply_text: creationCopy.en}};
  assert.equal(assistantCreationReply(ordinary, creationCopy.en), null);
  const rendered = render(AssistantReplyBubble, {message: ordinary}, 'en');
  assert.ok(rendered.includes('acmd_test'));
  const extra = 'Here is the requested poem.\n\nThe queued moon waits.';
  const mixed = creationMessage(creationCopy.en + '\n\n' + extra);
  assert.equal(assistantCreationReply(mixed, mixed.turn.reply_text), extra);
  assert.ok(render(AssistantReplyBubble, {message: mixed}, 'en').includes('The queued moon waits.'));
});

test('creation warnings and archived failed or partial results survive a later current success', () => {
  const blockers = [{command_id: 'old-command', delivery_id: 'old-delivery', delivery_revision: 2,
    reasons: ['check_failed:coverage']}];
  const value = creationMessage(creationCopy.en + '\n\nLimitations: Only partial coverage.',
    {...verifiedCreation, limitations: ['Only partial coverage.'], unresolved_deliveries: blockers});
  const tasks = [{...task, state: 'succeeded'}];
  const html = render(AssistantReplyBubble, {message: value, tasks}, 'en');
  assert.ok(html.includes('Task created.'));
  assert.ok(html.includes('Only partial coverage.'));
  assert.ok(html.includes('check_failed:coverage'));
  assert.ok(html.includes('assistant-delivery-warning'));
  for (const state of ['failed', 'unconfirmed', 'succeeded']) {
    const archived = message('Actual model answer with partial delivery.', {...task, state, task_complete: false,
      unresolved_deliveries: blockers});
    archived.turn.blocks = value.turn.blocks;
    const output = render(AssistantReplyBubble, {message: archived, tasks}, 'en');
    const label = require('../messages/en/assistants.json').assistants.taskStates[state];
    assert.ok(output.includes('<span role="status">' + label + '</span>'));
    assert.ok(output.includes('Actual model answer with partial delivery.'));
    assert.ok(output.includes('check_failed:coverage'));
    assert.doesNotMatch(output, /Task created\./);
  }
});

test('old records and old cached transcripts peel only the system delivery wrapper', () => {
  for (const receipt of [{...task,legacy:true},undefined]) {
    const projected = projectAssistantTaskMessage(message(legacy,receipt));
    assert.equal(projected.turn.reply_text,'Agent findings.');
    assert.equal(projected.assistant_task.task_session_id,'taskchat_test');
    assert.equal(collectChatResults({title:'Research',messages:[projected]})[0].text,'Agent findings.');
    assert.deepEqual(projectAssistantTaskMessage(projected),projected);
  }
});
test('new agent prose remains verbatim, including Chinese quotations, code and similar envelopes', () => {
  for (const content of [legacy,'Quoted user text: 查看任务对话.\n\n```text\n' + legacy + '\n```','Full agent conclusion. '+ 'Evidence. '.repeat(200)]) {
    const original = message(content,task);
    assert.equal(projectAssistantTaskMessage(original),original);
  }
  const ordinary = {...message(legacy),id:'ordinary-agent-answer'};
  assert.equal(projectAssistantTaskMessage(ordinary),ordinary);
  const mismatch = message(legacy,{...task,legacy:true,task_session_id:'taskchat_other'});
  assert.equal(projectAssistantTaskMessage(mismatch),mismatch);
  const nested = legacy.replace('Agent findings.',legacy);
  const projected = projectAssistantTaskMessage(message(nested,{...task,legacy:true}));
  assert.equal(projected.turn.reply_text,legacy);
  assert.equal(projectAssistantTaskMessage(projected),projected);
});
test('task receipts localize every terminal state and retain the same conversation target', () => {
  for (const locale of ['en','zh']) {
    const messages = require('../messages/'+locale+'/assistants.json');
    for (const state of ['succeeded','failed','interrupted','removed']) {
      const html = renderToStaticMarkup(React.createElement(NextIntlClientProvider,{locale,messages,timeZone:'UTC'},
        React.createElement(AssistantTaskReceipt,{task:{...task,state}})));
      assert.ok(html.includes(messages.assistants.openTaskConversation));
      assert.ok(html.includes(messages.assistants.taskStates[state]));
      assert.ok(html.includes('href="/chat/taskchat_test"'));
      if (locale === 'en') assert.doesNotMatch(html,/\p{Script=Han}/u);
    }
  }
});

test('receipt delivery labels use host authority and preserve execution state independently', () => {
  const cases = [
    [{}, 'unconfirmed'],
    [{task_complete: true}, 'unconfirmed'],
    [{task_complete: false, delivery_completion: {allowed: false, status: 'unavailable'}}, 'unconfirmed'],
    [{task_complete: false, delivery_completion: {allowed: false, status: 'partial'},
      unresolved_deliveries: [{command_id: 'old-command', reasons: ['check_failed:strategy_structure']}]}, 'incomplete'],
    [{delivery_completion: {allowed: false, status: 'incomplete'}}, 'incomplete'],
    [{delivery_completion: {allowed: false, status: 'complete'}}, 'incomplete'],
    [{delivery_completion: {allowed: false, status: 'partial'}}, 'incomplete'],
    [{task_complete: false, delivery_completion: {allowed: true, status: 'partial'}}, 'partial'],
    [{task_complete: true, delivery_completion: {allowed: true, status: 'complete'}}, 'complete'],
    [{task_complete: false, delivery_completion: {allowed: true, status: 'complete'}}, 'incomplete'],
    [{task_complete: true, delivery_completion: {allowed: true, status: 'complete'},
      unresolved_deliveries: [{command_id: 'old-command', reasons: ['check_pending:reported']}]}, 'incomplete'],
    [{state: 'failed', task_complete: true, delivery_completion: {allowed: true, status: 'complete'}}, 'incomplete'],
  ];
  for (const locale of ['en', 'zh']) for (const [fields, status] of cases) {
    const receipt = {...task, ...fields};
    const before = JSON.stringify(receipt);
    const html = render(AssistantTaskReceipt, {task: receipt}, locale);
    const labels = require('../messages/' + locale + '/assistants.json').assistants;
    assert.ok(html.includes(labels.deliveryStates[status]), html);
    assert.match(html, new RegExp('data-delivery-state="' + status + '"'));
    assert.ok(html.includes('<span role="status">' + labels.taskStates[receipt.state] + '</span>'));
    assert.equal(JSON.stringify(receipt), before);
  }
  assert.doesNotMatch(render(AssistantTaskReceipt, {task: {...task, state: 'running'}}, 'en'), /data-delivery-state/);
});

test('succeeded legacy and check-passed replies render turn-finished once without claiming delivery', () => {
  const creation = {kind: 'tool_result', action: 'assistant_task_create', ok: true,
    result: {ok: true, protocol: 'nerya.assistant_task.v2', ...task, state: 'queued'}};
  const prose = 'Completed checks. Delivery not confirmed.';
  const checked = message(prose, task);
  checked.turn.artifact_index = {tests_run: [{command: 'check', exit_code: 0}]};
  checked.turn.blocks = [{block: creation}, {block: creation}];
  for (const locale of ['en', 'zh']) {
    const expected = locale === 'en' ? 'Turn finished' : '本轮已结束';
    for (const value of [message(legacy, {...task, legacy: true}), message(legacy), checked]) {
      const projected = projectAssistantTaskMessage(value);
      assert.equal(projected.turn.delivery, undefined);
      const html = render(AssistantReplyBubble, {message: projected}, locale);
      assert.ok(html.includes('<span role="status">' + expected + '</span>'));
      assert.equal((html.match(/data-testid="assistant-task-receipt"/g) || []).length, 1);
      assert.equal(html.split('href="/chat/taskchat_test"').length - 1, 1);
      assert.doesNotMatch(html, /role="status">(?:Completed|已完成)</);
      assert.ok(html.includes(value === checked ? prose : 'Agent findings.'));
    }
  }
});

const results = ['succeeded', 'failed'].map((state, index) => ({
  command_id: 'command_' + index, task_id: 'awork_' + index, task_session_id: 'taskchat_' + index,
  title: 'Result ' + index, summary: 'Original agent summary ' + index, state, created_at: 0,
}));

test('assistant results render actual succeeded, failed and unknown states in both locales', () => {
  const rows = [...results, {...results[0], command_id: 'unknown', title: 'Unknown result', state: 'custom_state'}];
  const {AssistantResults} = component('shell/AssistantResults.tsx', {
    react: {...React, useEffect() {}, useState(initial) {
      return [initial && typeof initial === 'object' && 'rows' in initial
        ? {assistant: 'main', rows, error: false, loading: false} : initial, () => {}];
    }},
  });
  for (const locale of ['en', 'zh']) {
    const html = render(AssistantResults, {assistantId: 'main'}, locale);
    const buttons = html.match(new RegExp('<button[^>]*data-testid="assistant-result".*?</button>', 'gs')) || [];
    assert.equal(buttons.length, 3);
    for (const [index, expected] of (locale === 'en'
      ? ['Turn finished', 'Needs attention', 'custom_state']
      : ['本轮已结束', '需要处理', 'custom_state']).entries()) {
      assert.ok(buttons[index].includes(expected));
      assert.ok(buttons[index].includes(rows[index].summary));
    }
    assert.doesNotMatch(html, /Completed|已完成/);
  }
});

test('overview shows failed results with their actual state and no duplicate legacy output cards', () => {
  const passThrough = ({children}) => React.createElement(React.Fragment, null, children);
  const {AssistantOverviewPopover} = component('chat/AssistantOverviewPopover.tsx', {
    '../shell/AssistantResults': {useAssistantResults: () => ({rows: results, error: false, loading: false})},
    '@radix-ui/react-popover': {Root: passThrough, Trigger: passThrough, Portal: passThrough, Content: passThrough, Close: passThrough},
  });
  const tasks = {rows: [
    ...results.map(row => ({...row, kind: 'background', state: 'succeeded', output: 'Saved legacy output'})),
    {task_id: 'legacy_only', title: 'Legacy only', kind: 'background', state: 'succeeded', output: 'Legacy prose'},
  ], loading: false, error: '', busy: '', actionError: '', stop() {}, retry() {}};
  for (const locale of ['en', 'zh']) {
    const html = render(AssistantOverviewPopover, {assistant: {id: 'main', name: 'Nerya'}, tasks,
      onEdit() {}, onOpenReports() {}, onOpenResult() {}}, locale);
    const outputs = html.match(new RegExp('<section[^>]*data-testid="assistant-outputs".*?</section>', 's'))?.[0];
    assert.ok(outputs);
    const buttons = outputs.match(new RegExp('<button.*?</button>', 'gs')) || [];
    assert.equal(buttons.length, 3);
    assert.ok(buttons[0].includes('Result 0'));
    assert.ok(buttons[0].includes(locale === 'en' ? 'Turn finished' : '本轮已结束'));
    assert.ok(buttons[1].includes('Result 1'));
    assert.ok(buttons[1].includes(locale === 'en' ? 'Needs attention' : '需要处理'));
    assert.ok(buttons[2].includes('Legacy only'));
    assert.doesNotMatch(outputs, /Completed|已完成/);
  }
});
