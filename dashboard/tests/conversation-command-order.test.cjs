const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, file) => module._compile(ts.transpileModule(fs.readFileSync(file, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText, file);
const { projectCommand } = require('../lib/conversationCommands.ts');

// Projection fixtures only: no browser, API, model, or runtime service.
const command = { command_id: 'child-command', session_id: 'child-session', turn_id: 'child-turn',
  kind: 'send', state: 'running', revision: 1, position: 1, created_at: 100, updated_at: 110,
  input: 'Complete the strategy without enabling trading.', attachments: [], context: {} };
const user = { id: 'hydrated-user', role: 'user', backend_message_id: 'child-turn:user',
  ts: 100250, text: command.input };
const thread = messages => ({ id: command.session_id, messages, updated_ts: 100250, message_count: messages.length });
const ids = value => value.messages.map(message => message.id);

test('a hydrated child prompt precedes its running assistant even when it has a later timestamp', () => {
  const source = thread([Object.freeze({ ...user })]);
  const event = { event_id: 'tool-start', seq: 1, kind: 'tool.start', call_id: 'validate-strategy' };
  const projected = projectCommand(source, command, [event]);
  assert.deepEqual(ids(projected), ['hydrated-user', 'child-command:assistant']);
  assert.equal(projected.messages[0], source.messages[0]);
  assert.equal(projected.messages[0].ts, 100250);
  assert.equal(projected.messages[1].ts, 100000);
  assert.equal(projected.messages[1].loading, true);
  assert.deepEqual(projected.messages[1].live_events, [event]);
  assert.deepEqual(ids(source), ['hydrated-user']);
});

for (const state of ['interrupted', 'succeeded']) {
  for (const userTs of [100000, 100250]) {
    test(`hydrated ${state} replies follow their own prompt with ${userTs === 100000 ? 'equal' : 'later user'} timestamps`, () => {
      const assistant = Object.freeze({ id: 'hydrated-assistant', role: 'assistant', ts: 100000,
        backend_message_id: 'child-turn:assistant', turn: { turn_id: command.turn_id, reply_text: 'Saved result' } });
      const prompt = Object.freeze({ ...user, ts: userTs });
      const source = thread([assistant, prompt]);
      const projected = projectCommand(source, { ...command, state, revision: 2, result: assistant.turn });
      assert.deepEqual(ids(projected), ['hydrated-user', 'hydrated-assistant']);
      assert.equal(projected.messages[0], prompt);
      assert.equal(projected.messages[0].ts, userTs);
      assert.equal(projected.messages[1].ts, 100000);
      assert.equal(projected.messages[1].loading, false);
      assert.equal(projected.messages[1].turn.reply_text, 'Saved result');
      assert.deepEqual(ids(projectCommand(projected, { ...command, state, revision: 2 })), ids(projected));
      assert.deepEqual(ids(source), ['hydrated-assistant', 'hydrated-user']);
    });
  }
}

test('original and follow-up prompts stay before their own replies through replay without moving unread timestamps', () => {
  const original = { ...command, state: 'interrupted', revision: 2, result: { reply_text: 'Execution interrupted' } };
  const followup = { ...command, command_id: 'followup-command', turn_id: 'followup-turn',
    created_at: 200, updated_at: 210, position: 2, input: 'Continue without enabling trading.' };
  const originalAssistant = { id: 'hydrated-assistant', role: 'assistant', ts: 100000,
    backend_message_id: 'child-turn:assistant', turn: { turn_id: command.turn_id } };
  const followupAssistant = { id: 'followup-assistant', role: 'assistant', ts: 200000,
    backend_message_id: 'followup-turn:assistant', turn: { turn_id: followup.turn_id } };
  // Unrelated equal-timestamp rows retain their relative order and object identity.
  const unrelated = [
    { id: 'unrelated-user', role: 'user', ts: 150000, text: 'Separate message' },
    { id: 'unrelated-assistant', role: 'assistant', ts: 150000, turn: { reply_text: 'Separate reply' } },
  ];
  const source = thread([originalAssistant, { ...user }, ...unrelated, followupAssistant,
    { ...user, id: 'followup-user', backend_message_id: 'followup-turn:user', ts: 200250, text: followup.input }]);
  source.messages.forEach(Object.freeze);
  const timestamps = value => Object.fromEntries(value.messages.map(message => [message.id, message.ts]));
  const unreadAfter = value => value.messages.filter(message => message.ts > 175000).map(message => message.id).sort();
  const event = { event_id: 'followup-tool', seq: 1, kind: 'tool.start', call_id: 'continue-validation' };
  let projected = source;
  for (const snapshot of [followup, original, followup, original, followup]) {
    projected = projectCommand(projected, snapshot, snapshot === followup ? [event] : []);
    assert.deepEqual(ids(projected), ['hydrated-user', 'hydrated-assistant', 'unrelated-user',
      'unrelated-assistant', 'followup-user', 'followup-assistant']);
    assert.deepEqual(timestamps(projected), timestamps(source));
    assert.deepEqual(unreadAfter(projected), unreadAfter(source));
    assert.equal(projected.message_count, 6);
    assert.equal(new Set(ids(projected)).size, 6);
    assert.equal(projected.messages[2], unrelated[0]);
    assert.equal(projected.messages[3], unrelated[1]);
  }
  assert.deepEqual(projected.messages.at(-1).live_events, [event]);
  assert.equal(projected.messages[1].execution_status, 'interrupted');
  assert.equal(projected.messages.at(-1).loading, true);
  assert.equal(projected.updated_ts, 210000);
  assert.deepEqual(ids(source), ['hydrated-assistant', 'hydrated-user', 'unrelated-user',
    'unrelated-assistant', 'followup-assistant', 'followup-user']);
});

test('a newly projected prompt precedes an existing equal-timestamp assistant without replacing its identity', () => {
  const assistant = { id: 'stream-assistant', role: 'assistant', ts: 100000, command_id: command.command_id };
  const projected = projectCommand(thread([assistant]), command);
  assert.deepEqual(ids(projected), ['child-command:user', 'stream-assistant']);
  assert.deepEqual(projected.messages.map(message => message.ts), [100000, 100000]);
  assert.deepEqual(ids(projectCommand(projected, command)), ids(projected));
});

test('command identity links a hydrated prompt even when it has no backend message identity', () => {
  const prompt = { ...user, command_id: command.command_id };
  delete prompt.backend_message_id;
  const projected = projectCommand(thread([prompt]), command);
  assert.deepEqual(ids(projected), ['hydrated-user', 'child-command:assistant']);
  assert.equal(projected.messages[0], prompt);
});

for (const mode of ['resume', 'hidden-user']) {
  test(`${mode} preserves an existing turn's order and timestamps without adding a prompt`, () => {
    const prompt = { ...user, command_id: command.command_id };
    const assistant = { id: 'saved-assistant', role: 'assistant', ts: 100000, backend_message_id: 'child-turn:assistant',
      command_id: command.command_id, command_created_at: 100, command_revision: 1,
      execution_status: 'interrupted', turn: { turn_id: command.turn_id, reply_text: 'Partial result' } };
    const later = { id: 'later-user', role: 'user', ts: 300000, text: 'Later turn' };
    const source = thread([prompt, assistant, later]);
    const continued = { ...command, command_id: 'continuation-command', created_at: 200, updated_at: 210,
      ...(mode === 'resume' ? { kind: 'resume' } : { show_user: false }) };
    const projected = projectCommand(source, continued);
    assert.deepEqual(ids(projected), ids(source));
    assert.deepEqual(projected.messages.map(message => message.ts), [100250, 100000, 300000]);
    assert.equal(projected.messages[0], prompt);
    assert.equal(projected.messages[2], later);
    assert.equal(projected.messages[1].command_id, 'continuation-command');
    assert.equal(projected.messages[1].loading, true);
    assert.deepEqual(ids(projectCommand(projected, continued)), ids(source));
    assert.equal(projectCommand(projected, { ...command, state: 'failed', revision: 99 }), projected,
      'an older command cannot replace a newer command sharing its turn');
    const assistantOnly = projectCommand(thread([assistant]), continued);
    assert.deepEqual(ids(assistantOnly), ['saved-assistant']);
    assert.equal(assistantOnly.messages[0].ts, 100000);
  });
}

test('an older revision cannot overwrite the terminal result or corrected order', () => {
  const terminal = projectCommand(thread([{ ...user }]), { ...command, state: 'interrupted', revision: 3,
    result: { reply_text: 'Provider stream interrupted' } });
  assert.equal(projectCommand(terminal, command), terminal);
  assert.equal(terminal.messages[1].turn.reply_text, 'Provider stream interrupted');
  assert.deepEqual(ids(terminal), ['hydrated-user', 'child-command:assistant']);
});

test('an older command outside the loaded page is inserted before the newer turn', () => {
  const newer = { ...command, command_id: 'new-command', turn_id: 'new-turn', created_at: 300, updated_at: 310 };
  const source = projectCommand(thread([]), newer);
  const projected = projectCommand(source, { ...command, state: 'interrupted', updated_at: 120 });
  assert.deepEqual(ids(projected), ['child-command:user', 'child-command:assistant',
    'new-command:user', 'new-command:assistant']);
  assert.equal(projected.messages.at(-1).command_id, 'new-command');
  assert.equal(projected.updated_ts, 310000);
});

test('queued, removed, guide and foreign-session snapshots leave the thread untouched', () => {
  const source = thread([{ id: 'assistant', role: 'assistant', ts: 100000 }, { ...user }]);
  for (const fields of [{ state: 'queued' }, { state: 'removed' }, { kind: 'guide' }, { session_id: 'other-session' }]) {
    assert.equal(projectCommand(source, { ...command, ...fields }), source);
  }
});
