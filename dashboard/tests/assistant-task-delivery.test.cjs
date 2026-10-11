const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { NextIntlClientProvider } = require('next-intl');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText, filename);
const { projectAssistantTaskMessage } = require('../lib/assistantTaskDelivery.ts');
const { collectChatResults } = require('../lib/chatResults.ts');
const { AssistantTaskReceipt } = require('../components/chat/AssistantTaskReceipt.tsx');
const task = {task_id:'awork_test',task_session_id:'taskchat_test',title:'Research',state:'succeeded'};
const message = (content, receipt) => ({id:'assistant_result:test',role:'assistant',ts:1,execution_status:'succeeded',turn:{reply_text:content},assistant_task:receipt});
const legacy = '**Research** · 已完成 / Completed\n\nAgent findings.\n\n[查看任务对话 / Open task conversation](/chat/taskchat_test)';

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
