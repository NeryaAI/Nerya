// Offline rendering only: no runtime, browser, or approval callbacks are invoked.
// Run: node --test tests/approval-dock.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, filename);
require.extensions['.css'] = module => { module.exports = {}; };
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { NextIntlClientProvider } = require('next-intl');
const { AssistantBubble } = require('../components/chat/ChatMessage.tsx');
const { ApprovalRequestCard } = require('../components/chat/ApprovalRequestCard.tsx');
const { resolveMessageApproval } = require('../lib/approvalResolution.ts');
const { projectCommand } = require('../lib/conversationCommands.ts');
const { projectMessageBlocks } = require('../lib/streamTranscript.ts');
const translations = Object.fromEntries(['en', 'zh'].map(locale => [locale, Object.assign({},
  ...fs.readdirSync(path.join(__dirname, '../messages', locale)).filter(file => file.endsWith('.json'))
    .map(file => require('../messages/' + locale + '/' + file)))]));
const approvalId = 'fixture-shell-approval';
const card = {
  record: { id: approvalId, kind: 'tool_permission', state: 'pending' },
  prompt: { approval_id: approvalId, text: 'run_shell requires permission: inspect the project', buttons: [
    { label: 'Approve', callback_data: 'approve:' + approvalId },
    { label: 'Reject', callback_data: 'reject:' + approvalId },
    { label: 'Details', callback_data: 'details:' + approvalId },
  ] },
};
const event = { kind: 'approval_request', approval_id: approvalId, state: 'pending', prompt: card.prompt, record: card.record };
const pendingApprovals = new Map([[approvalId, card]]);
const message = { id: 'fixture-message', role: 'assistant', ts: 1790000000000, execution_status: 'awaiting_approval',
  turn: { stopped_reason: 'approval_pending', blocks: [{ block: event }] },
};
function noAction() { throw new Error('Rendering must never respond to an approval'); }
function render(child, locale = 'en') {
  return renderToStaticMarkup(React.createElement(NextIntlClientProvider, { locale, messages: translations[locale], timeZone: 'UTC' }, child));
}
function bubble(msg = message, props = {}) {
  return React.createElement(AssistantBubble, { msg, pendingApprovals, onApprovalAction: noAction, ...props });
}
const buttons = html => (html.match(/<button\b/g) || []).length;

for (const locale of ['en', 'zh']) {
  for (const streaming of [false, true]) {
    test(`${locale}: ${streaming ? 'streamed' : 'restored'} approval has one action owner and a readable transcript receipt`, () => {
      const msg = streaming ? { ...message, loading: true, turn: undefined, live_events: [{ ...event, kind: 'approval.request', seq: 1 }] } : message;
      const before = JSON.stringify(msg);
      const html = render(React.createElement(React.Fragment, null,
        React.createElement('section', { id: 'transcript' }, bubble(msg, { dockApprovalId: approvalId })),
        React.createElement('section', { id: 'dock' }, React.createElement(ApprovalRequestCard, { event, card, onAction: noAction })),
      ), locale);
      const transcript = html.split('<section id="dock">')[0];
      assert.match(transcript, /run_shell requires permission/);
      assert.match(transcript, /fixture-shell-appr/);
      assert.equal(buttons(transcript), 0, 'dock-owned transcript receipt must not duplicate Approve, Reject, or Details');
      assert.equal(buttons(html), 3, 'only the dock owns the three actions');
      assert.equal(JSON.stringify(msg), before, 'rendering does not mutate permission state');
    });
  }

  for (const state of ['approved', 'rejected', 'expired', 'cancelled']) {
    test(`${locale}: ${state} receipt stays readable after resolution and dock removal`, () => {
      const resolved = resolveMessageApproval(message, approvalId, state);
      for (const dockApprovalId of [approvalId, undefined]) {
        const html = render(bubble(resolved, { dockApprovalId }), locale);
        assert.match(html, /run_shell requires permission/);
        assert.ok(html.includes(translations[locale].approvals[state]));
        assert.equal(buttons(html), 0, 'resolved approvals never regain action buttons');
      }
    });
  }
}

function expiredMessage(blocks = [{ block: event }]) {
  const command = { command_id: 'fixture-expired-command', session_id: 'fixture-session', kind: 'send',
    state: 'interrupted', revision: 2, turn_id: 'fixture-expired-turn', created_at: 1790000000, updated_at: 1793167084,
    input: '', attachments: [], context: {}, show_user: false,
    result: { turn_id: 'fixture-expired-turn', stopped_reason: 'approval_expired', blocks },
  };
  return projectCommand({ id: command.session_id, messages: [] }, command).messages[0];
}

for (const locale of ['en', 'zh']) {
  for (const fallback of [false, true]) {
    test(`${locale}: authoritative approval_expired command leaves a readable receipt without ${fallback ? 'fallback' : 'prompt'} controls`, () => {
      const expiredEvent = fallback ? { ...event, prompt: { ...event.prompt, buttons: [] } } : event;
      const msg = expiredMessage([{ block: expiredEvent }]);
      const before = JSON.stringify(msg);
      const html = render(bubble(msg, { pendingApprovals: new Map() }), locale);
      assert.match(html, /run_shell requires permission/);
      assert.equal(buttons(html), 0, 'an expired approval must not invite a stale response');
      assert.ok(html.includes(translations[locale].approvals.expired));
      assert.equal(JSON.stringify(msg), before, 'expiry is presentation only');
    });
  }
}

test('expiry does not suppress a different approval still present in the pending projection', () => {
  const otherId = 'fixture-other-approval';
  const otherCard = { record: { id: otherId, kind: 'tool_permission', state: 'pending' },
    prompt: { approval_id: otherId, text: 'Another native permission remains pending' } };
  const otherEvent = { kind: 'approval_request', approval_id: otherId, state: 'pending', ...otherCard };
  const msg = expiredMessage([{ block: event }, { block: otherEvent }]);
  const html = render(bubble(msg, { pendingApprovals: new Map([[otherId, otherCard]]) }));
  assert.match(html, /run_shell requires permission/);
  assert.match(html, /Another native permission remains pending/);
  assert.equal(buttons(html), 2, 'only the still-pending approval keeps its fallback controls');
  assert.ok(html.includes(translations.en.approvals.expired));
  assert.equal(buttons(render(bubble(expiredMessage()))), 3, 'a current pending card for the same ID also remains authoritative');
});

test('interruption alone and financial receipts never imply native permission expiry', () => {
  const interrupted = { ...message, execution_status: 'interrupted', turn: { ...message.turn, stopped_reason: 'operator_stop' } };
  assert.equal(buttons(render(bubble(interrupted, { pendingApprovals: new Map() }))), 3);
  for (const kind of ['trade_intent', 'wallet_swap']) {
    const financial = { ...event, record: { ...card.record, kind } };
    assert.equal(buttons(render(bubble(expiredMessage([{ block: financial }]), { pendingApprovals: new Map() }))), 3);
  }
});

test('standalone approvals and requests not owned by the current dock retain their actions', () => {
  for (const dockApprovalId of [undefined, '', 'another-approval']) {
    assert.equal(buttons(render(bubble(message, { dockApprovalId }))), 3);
  }
});

test('read-only receipt also suppresses fallback buttons when no prompt buttons were provided', () => {
  const fallback = { ...event, prompt: { ...card.prompt, buttons: [] } };
  assert.equal(buttons(render(React.createElement(ApprovalRequestCard, { event: fallback, readOnly: true, onAction: noAction }))), 0);
  assert.equal(buttons(render(React.createElement(ApprovalRequestCard, { event: fallback, onAction: noAction }))), 2);
});

test('a busy decision disables the dock actions without creating transcript controls', () => {
  const html = render(bubble(message, { dockApprovalId: approvalId, resolvingApprovalIds: new Set([approvalId]) }));
  assert.equal(buttons(html), 0);
  const dock = render(React.createElement(ApprovalRequestCard, { event, card, busy: true, onAction: noAction }));
  assert.equal(buttons(dock), 3);
  assert.equal((dock.match(/disabled=""/g) || []).length, 3);
});

// Evaluate the actual ChatView selection, dock JSX and transcript ownership prop,
// without mounting its service hooks or substituting a second wiring fixture.
const viewSource = ts.createSourceFile('ChatView.tsx', fs.readFileSync(require.resolve('../components/chat/ChatView.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const viewVariables = new Map();
const viewFunctions = new Map();
let dockNode, bubbleNode, composerContainer;
const modalNodes = [];
function attribute(node, name) {
  return node.attributes.properties.find(prop => prop.name?.text === name)?.initializer;
}
function visit(node) {
  if (ts.isVariableDeclaration(node)) viewVariables.set(node.name.getText(viewSource), node.initializer);
  if (ts.isFunctionDeclaration(node) && node.name) viewFunctions.set(node.name.text, node);
  if (ts.isJsxOpeningElement(node) && attribute(node, 'data-testid')?.text === 'conversation-interaction-dock') dockNode = node.parent;
  if (ts.isJsxSelfClosingElement(node) && node.tagName.getText(viewSource) === 'AssistantBubble') bubbleNode = node;
  if (ts.isJsxSelfClosingElement(node) && node.tagName.getText(viewSource) === 'ChatInput' && node.parent.openingElement && attribute(node.parent.openingElement, 'hidden')) composerContainer = node.parent.openingElement;
  if ((ts.isJsxSelfClosingElement(node) || ts.isJsxOpeningElement(node)) && node.tagName.getText(viewSource) === 'PendingToolApprovalDialog') modalNodes.push(node);
  ts.forEachChild(node, visit);
}
visit(viewSource);
function evaluate(node, context) {
  assert.ok(node, 'ChatView wiring must exist');
  const compiled = ts.transpileModule('module.exports = (' + node.getText(viewSource) + ');', {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const sandbox = { ...context, module: { exports: {} }, require: name => { assert.equal(name, 'react/jsx-runtime'); return require(name); } };
  sandbox.exports = sandbox.module.exports;
  require('node:vm').runInNewContext(compiled, sandbox);
  return sandbox.module.exports;
}
function viewContext(overrides = {}) {
  const context = { activeApprovalIds: [approvalId], pendingApprovals, externalView: false,
    active: { id: 'fixture-session' }, resolvingApprovalIds: new Set(), resolveApproval: noAction, ApprovalRequestCard,
    workbench: { view: { pending_interactions: [] } }, awaitingApproval: true, ...overrides,
  };
  context.nativeApprovalId = evaluate(viewVariables.get('nativeApprovalId'), context);
  context.nativeApprovalCard = evaluate(viewVariables.get('nativeApprovalCard'), context);
  return context;
}

test('ChatView renders the same approval once in its shared dock and hides the ordinary composer', () => {
  const context = viewContext();
  const dock = evaluate(dockNode, context);
  const dockApprovalId = evaluate(attribute(bubbleNode, 'dockApprovalId').expression, context);
  assert.equal(dockApprovalId, approvalId);
  const html = render(React.createElement(React.Fragment, null, bubble(message, { dockApprovalId }), dock));
  const [transcript, controls] = html.split('data-testid="conversation-interaction-dock"');
  assert.match(transcript, /run_shell requires permission/);
  assert.equal(buttons(transcript), 0);
  assert.equal(buttons(controls), 3);
  assert.equal(modalNodes.length, 0, 'no parallel modal action owner');
  assert.equal(evaluate(attribute(composerContainer, 'hidden').expression, context), true);
  assert.equal(evaluate(viewVariables.get('showHero'), { ...context, conversationEmpty: true, showMissing: false, showLoadFailure: false, showLoading: false, draft: { recovery: [], storageError: null } }), false, 'a restored approval must not be hidden behind the empty-chat composer');
});

test('ChatView only delegates native tool permissions with a loaded card to the dock', () => {
  for (const overrides of [
    { pendingApprovals: new Map() },
    { externalView: true },
    ...['trade_intent', 'wallet_swap'].map(kind => ({ pendingApprovals: new Map([[approvalId, { ...card, record: { ...card.record, kind } }]]) })),
  ]) {
    const context = viewContext(overrides);
    assert.equal(context.nativeApprovalId, '');
    assert.equal(evaluate(attribute(bubbleNode, 'dockApprovalId').expression, context), undefined);
    assert.equal(buttons(render(evaluate(dockNode, context))), 0);
  }
  const batch = { ...card, record: { ...card.record, kind: 'tool_permission_batch' } };
  assert.equal(viewContext({ pendingApprovals: new Map([[approvalId, batch]]) }).nativeApprovalId, approvalId);
});

test('expired pending projection cannot claim the dock or hide another valid pending approval', () => {
  const context = {};
  for (const name of ['asRecord', 'approvalState', 'isApprovalResolved', 'approvalIdFromRecord', 'approvalBlockFromEnvelope', 'cardSessionId', 'pendingApprovalIdsForThread']) {
    context[name] = evaluate(viewFunctions.get(name), context);
  }
  const otherId = 'fixture-valid-pending';
  const other = { ...card, record: { ...card.record, id: otherId, session_id: 'fixture-session' } };
  const expired = { ...card, record: { ...card.record, state: 'expired', session_id: 'fixture-session' } };
  const thread = { id: 'fixture-session', messages: [expiredMessage()] };
  assert.deepEqual(Array.from(context.pendingApprovalIdsForThread(thread, new Map([[approvalId, expired], [otherId, other]]))), [otherId]);
  assert.deepEqual(Array.from(context.pendingApprovalIdsForThread(thread, new Map())), []);
  const html = render(React.createElement(ApprovalRequestCard, { event, card: expired, onAction: noAction }));
  assert.equal(buttons(html), 0, 'a durable expired record overrides a stale pending event');
  assert.ok(html.includes(translations.en.approvals.expired));
});

function approvalAttachment(initial) {
  let threads = initial;
  const context = { setThreads: update => { threads = update(threads); } };
  for (const name of ['asRecord', 'cardSessionId', 'approvalEventFromCard', 'attachPendingApprovalEvents']) {
    context[name] = evaluate(viewFunctions.get(name), context);
  }
  return {
    get threads() { return threads; },
    set threads(value) { threads = value; },
    refresh(cards) { context.attachPendingApprovalEvents(cards); },
  };
}
const currentApprovalId = 'tool_batch_turn_f3';
const currentCard = { ...card, record: { ...card.record, id: currentApprovalId, session_id: 'fixture-session', turn_id: 'turn_f3' },
  prompt: { ...card.prompt, approval_id: currentApprovalId, text: 'CURRENT turn_f3 permission',
    buttons: card.prompt.buttons.map(button => ({ ...button, callback_data: button.callback_data.replace(approvalId, currentApprovalId) })) },
};
const currentCards = new Map([[currentApprovalId, currentCard]]);
const currentMessage = { ...message, id: 'current-message', backend_message_id: 'turn_f3:assistant',
  turn: { turn_id: 'turn_f3', stopped_reason: 'approval_pending', blocks: [] } };

test('approval refresh before owning-turn hydration never injects the current receipt into an expired older message', () => {
  const old = expiredMessage();
  const fixture = approvalAttachment([{ id: 'fixture-session', messages: [old] }]);
  fixture.refresh(currentCards);
  assert.equal(fixture.threads[0].messages[0].live_events?.length || 0, 0, 'a missing owner must not fall back to the last assistant');
  const missingOwnerDock = viewContext({ activeApprovalIds: [currentApprovalId], pendingApprovals: currentCards, active: fixture.threads[0] });
  assert.equal(buttons(render(evaluate(dockNode, missingOwnerDock))), 3, 'missing-turn recovery remains available in the dock');
  fixture.threads = [{ ...fixture.threads[0], messages: [...fixture.threads[0].messages, currentMessage] }];
  fixture.refresh(currentCards);
  fixture.refresh(currentCards);
  const [historical, current] = fixture.threads[0].messages;
  assert.equal(current.live_events.length, 1, 'polling remains idempotent for the owning turn');
  const props = { pendingApprovals: currentCards, dockApprovalId: currentApprovalId };
  const oldHtml = render(bubble(historical, props));
  const currentHtml = render(bubble(current, props));
  assert.match(oldHtml, /fixture-shell-appr/);
  assert.ok(oldHtml.includes(translations.en.approvals.expired));
  assert.doesNotMatch(oldHtml, /tool_batch_turn_f3|CURRENT turn_f3/);
  assert.match(currentHtml, /tool_batch_turn_f3/);
  assert.doesNotMatch(currentHtml, /fixture-shell-appr/);
  assert.equal(buttons(oldHtml) + buttons(currentHtml), 0);
  const context = viewContext({ activeApprovalIds: [currentApprovalId], pendingApprovals: currentCards });
  assert.equal(buttons(render(evaluate(dockNode, context))), 3, 'the dock stays the single interactive owner');
});

test('refresh repairs an already-injected foreign pending receipt without removing the old expired receipt', () => {
  const old = { ...expiredMessage(), live_events: [{ kind: 'approval.request', approval_id: currentApprovalId,
    session_id: 'fixture-session', turn_id: 'turn_f3', prompt: currentCard.prompt, record: currentCard.record }] };
  const fixture = approvalAttachment([{ id: 'fixture-session', messages: [old, currentMessage] }]);
  fixture.refresh(currentCards);
  assert.equal(fixture.threads[0].messages[0].live_events.length, 0);
  assert.equal(fixture.threads[0].messages[0].turn.blocks[0].block.approval_id, approvalId);
  assert.equal(fixture.threads[0].messages[1].live_events[0].approval_id, currentApprovalId);
  assert.equal(old.live_events.length, 1, 'projection must not mutate the source message');
});

test('pending approvals require both session and turn ownership before transcript attachment', () => {
  for (const record of [
    { ...currentCard.record, session_id: '' },
    { ...currentCard.record, turn_id: '' },
    { ...currentCard.record, turn_id: 'not-loaded' },
  ]) {
    const fixture = approvalAttachment([
      { id: 'fixture-session', messages: [currentMessage] },
      { id: 'another-session', messages: [currentMessage] },
    ]);
    fixture.refresh(new Map([[currentApprovalId, { ...currentCard, record }]]));
    for (const thread of fixture.threads) assert.equal(thread.messages[0].live_events?.length || 0, 0);
  }
  const fixture = approvalAttachment([
    { id: 'fixture-session', messages: [currentMessage] },
    { id: 'another-session', messages: [currentMessage] },
  ]);
  fixture.refresh(currentCards);
  assert.equal(fixture.threads[0].messages[0].live_events[0].approval_id, currentApprovalId);
  assert.equal(fixture.threads[1].messages[0].live_events?.length || 0, 0, 'even matching turn IDs cannot cross sessions');
  const metadataOwned = { ...currentCard, record: { ...currentCard.record, session_id: '', turn_id: '' },
    prompt: { ...currentCard.prompt, metadata: { session_id: 'fixture-session', turn_id: 'turn_f3' } } };
  const fromMetadata = approvalAttachment([{ id: 'fixture-session', messages: [expiredMessage(), currentMessage] }]);
  fromMetadata.refresh(new Map([[currentApprovalId, metadataOwned]]));
  assert.equal(fromMetadata.threads[0].messages[0].live_events?.length || 0, 0);
  const attached = fromMetadata.threads[0].messages[1].live_events[0];
  assert.equal(attached.session_id, 'fixture-session');
  assert.equal(attached.turn_id, 'turn_f3');
});

test('projection excludes cached foreign approval requests without waiting for polling or hydration cleanup', () => {
  const currentEvent = { kind: 'approval.request', approval_id: currentApprovalId, seq: 10,
    turn_id: 'turn_f3', record: currentCard.record, prompt: currentCard.prompt };
  for (const source of ['live_events', 'activity_events']) {
    for (const recordOnly of [false, true]) {
      const retained = { ...currentEvent, ...(recordOnly ? { turn_id: undefined } : {}) };
      const old = expiredMessage();
      if (source === 'live_events') old.live_events = [retained];
      else old.turn.activity_events = [retained];
      const before = JSON.stringify(old);
      const props = { pendingApprovals: currentCards, dockApprovalId: currentApprovalId };
      const oldHtml = render(bubble(old, props));
      assert.doesNotMatch(oldHtml, /tool_batch_turn_f3|CURRENT turn_f3/, `${source}, nested owner only: ${recordOnly}`);
      assert.match(oldHtml, /fixture-shell-appr/);
      assert.ok(oldHtml.includes(translations.en.approvals.expired));
      assert.equal(buttons(oldHtml), 0);
      const currentHtml = render(bubble({ ...currentMessage, live_events: [retained] }, props));
      assert.match(currentHtml, /tool_batch_turn_f3/);
      assert.equal(buttons(currentHtml), 0);
      assert.equal(JSON.stringify(old), before, 'projection leaves cached input untouched');
    }
  }
});

test('projection ignores foreign approval resolutions but preserves own, legacy and non-approval events', () => {
  const old = expiredMessage();
  const blocks = projectMessageBlocks({ ...old, live_events: [
    { kind: 'approval.resolved', approval_id: approvalId, turn_id: 'turn_f3', state: 'approved', seq: 1 },
  ] });
  assert.notEqual(blocks[0].block.state, 'approved', 'another turn cannot resolve this receipt');
  assert.ok(!projectMessageBlocks({ ...old, live_events: [
    { kind: 'approval.resolved', approval_id: currentApprovalId, record: { turn_id: 'turn_f3' }, state: 'approved', seq: 1 },
  ] }).some(row => row.block?.approval_id === currentApprovalId));
  const own = { ...event, kind: 'approval.request', turn_id: old.turn.turn_id, seq: 2 };
  assert.ok(projectMessageBlocks({ ...old, turn: { turn_id: old.turn.turn_id, blocks: [] }, live_events: [own] }).some(row => row.block?.approval_id === approvalId));
  const legacy = { ...own, turn_id: undefined };
  assert.ok(projectMessageBlocks({ ...old, live_events: [legacy] }).some(row => row.block?.approval_id === approvalId));
  const unownedMessage = { ...message, live_events: [{ ...own, turn_id: 'turn_f3' }] };
  assert.ok(projectMessageBlocks(unownedMessage).some(row => row.block?.approval_id === approvalId), 'unknown message ownership does not invent a mismatch');
  const tool = { kind: 'tool.start', turn_id: 'turn_f3', call_id: 'other-stream-tool', action: 'read_file', payload: {}, seq: 3 };
  assert.ok(projectMessageBlocks({ ...old, live_events: [tool] }).some(row => row.block?.call_id === tool.call_id));
  const byBackend = { ...old, turn: { ...old.turn, turn_id: undefined }, live_events: [{ ...own, turn_id: 'turn_f3', approval_id: currentApprovalId }] };
  assert.ok(!projectMessageBlocks(byBackend).some(row => row.block?.approval_id === currentApprovalId), 'backend message identity also establishes the owner');
});
