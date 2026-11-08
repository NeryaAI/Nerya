const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), ts = require('typescript');
require.extensions['.ts'] = (m, f) => m._compile(ts.transpileModule(fs.readFileSync(f, 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true}}).outputText, f);
const {toolPresentation} = require('../lib/agentConversation.ts');
const {executionSteps} = require('../lib/executionTimeline.ts');
const {toolSemantics} = require('../lib/toolSemantics.ts');

const calls = [
  {skill: 'strategy_author'},
  {skill: 'strategy_author', file: 'references/workflows.md'},
  {skill: 'strategy_author', action: 'read', file: 'references/specialized-contracts.md'},
  {skill: 'strategy_author', action: 'load', file: 'references/replay-capabilities.md'},
  {skill: 'strategy_author', file: 'references/full-playbook.md', offset: 0, limit: 200},
  {skill: 'strategy_author', file: 'references/full-playbook.md', offset: 200, limit: 200},
];

for (const zh of [false, true]) test(`${zh ? 'zh' : 'en'}: collapsed Skill reads identify reference pages and offsets without merging calls`, () => {
  const rows = calls.flatMap((payload, i) => [
    {block: {kind: 'tool_use', action: 'Skill', call_id: `skill-${i}`, payload}},
    {block: {kind: 'tool_result', call_id: `skill-${i}`, ok: true, result: 'Reference content'}},
  ]);
  const before = JSON.stringify(rows);
  const steps = executionSteps(rows);
  assert.equal(steps.length, 6);
  const labels = steps.map(step => toolPresentation(step, 'completed', zh));
  assert.deepEqual(labels.map(({family, title, subject}) => ({family, title, subject})), [
    {family: 'skill', title: zh ? '加载技能' : 'Load skill', subject: 'strategy_author'},
    {family: 'skill', title: zh ? '读取技能' : 'Read skill', subject: 'strategy_author / references/workflows.md'},
    {family: 'skill', title: zh ? '读取技能' : 'Read skill', subject: 'strategy_author / references/specialized-contracts.md'},
    {family: 'skill', title: zh ? '读取技能' : 'Read skill', subject: 'strategy_author / references/replay-capabilities.md'},
    {family: 'skill', title: zh ? '读取技能' : 'Read skill', subject: `strategy_author / references/full-playbook.md · ${zh ? '偏移' : 'offset'} 0`},
    {family: 'skill', title: zh ? '读取技能' : 'Read skill', subject: `strategy_author / references/full-playbook.md · ${zh ? '偏移' : 'offset'} 200`},
  ]);
  assert.ok(labels.every(label => !label.pending && !label.failed));
  assert.equal(JSON.stringify(rows), before);
});

test('legacy Skill aliases retain skill identity, and invalid offsets add no page label', () => {
  for (const name of ['Skill', 'skill_view', 'skill_load', 'nerya_Skill']) {
    for (const zh of [false, true]) {
      assert.equal(toolSemantics(name, {skill_id: 'strategy_author', file: 'references/workflows.md', offset: 400}, zh).subject,
        `strategy_author / references/workflows.md · ${zh ? '偏移' : 'offset'} 400`);
      assert.equal(toolSemantics(name, {skill_id: 'strategy_author', offset: 200}, zh).subject, 'strategy_author');
      for (const offset of [undefined, null, -1, 0.5, Infinity, '200']) {
        assert.equal(toolSemantics(name, {skill_id: 'strategy_author', file: 'references/workflows.md', offset}, zh).subject,
          'strategy_author / references/workflows.md');
      }
    }
  }
  assert.equal(toolSemantics('read_file', {path: 'strategies/backtest.py'}, false).family, 'read');
  assert.equal(toolSemantics('script_run', {skill: 'strategy_author', script: 'main.py', offset: 200}, false).subject, 'strategy_author / main.py');
});

for (const zh of [false, true]) test(`${zh ? 'zh' : 'en'}: Skill inspect identifies the script and page before and after completion`, () => {
  for (const inputKey of ['payload', 'arguments']) {
    for (const [script, offset] of [['lp_replay.py', 0], ['lp_replay.py', 200], ['scripts/lp_replay.py', 200]]) {
      const input = {skill: 'backtest', action: 'inspect', script, offset, limit: 210};
      const rows = [{block: {kind: 'tool_use', action: 'Skill', call_id: 'inspect', [inputKey]: input}}];
      for (const completed of [false, true]) {
        if (completed) rows.push({block: {kind: 'tool_result', call_id: 'inspect', ok: true, result: 'Script source'}});
        const before = JSON.stringify(rows);
        const steps = executionSteps(rows);
        assert.equal(steps.length, 1);
        const label = toolPresentation(steps[0], completed ? 'completed' : 'running', zh);
        assert.equal(label.family, 'skill');
        assert.equal(label.title, zh ? '读取文件' : 'Read file');
        assert.equal(label.subject, `backtest / ${script} · ${zh ? '偏移' : 'offset'} ${offset}`);
        assert.equal(label.pending, !completed);
        assert.equal(label.failed, false);
        assert.equal(JSON.stringify(rows), before);
      }
    }
  }
});
