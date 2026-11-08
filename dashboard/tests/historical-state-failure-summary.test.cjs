// Local fixtures only: no backend, browser, runtime, or network calls.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText, filename);
const { toolFailureSummary } = require('../lib/toolFailureSummary.ts');
const { copy } = require('../lib/i18n.ts');
const malformed = 'Missing or malformed historical contract state';
const budget = 'historical_sample_budget_exceeded';
const legacyBudget = 'Requested samples exceed sample or request budget';
const key = name => `copy.components_chat_ReadableExecution.${name}`;
const budgetPrefix = 'Requested window requires 56 samples including both endpoints; max_samples allows 30. Requested step_blocks is 3457. ';
const budgetMinimum = 'The minimum step_blocks to fit max_samples is 6555. ';
const budgetRpc = 'This request also requires 954 RPC requests; the request budget allows 512. A sample-only correction may not fit it. ';
const budgetSuffix = 'Preserve the requested pool, window and tick bounds; no request was changed. Do not retry unchanged or raise budgets; any step change requires explicit caller review.';
const numericBudget = budgetPrefix + budgetMinimum + budgetRpc + budgetSuffix;

test('producer missing-skill validation uses required-parameter copy and keeps the diagnostic', () => {
  for (const [message, expected] of [
    ['Skill tool requires a non-empty \"skill\" argument.', /missing the required parameter, “skill”/],
    ['Skill tool requires a non-empty skill argument', /missing the required parameter, “skill”/],
    ['Skill tool requires a non-empty \"skill\" argument', /missing the required parameter, “skill”/],
  ]) {
    const failure = { ok: false, error: message };
    const summary = toolFailureSummary(failure, false);
    assert.match(summary, expected);
    assert.doesNotMatch(summary, /Skill tool requires a non-empty/);
    assert.equal(failure.error, message);
  }
  assert.match(toolFailureSummary({ ok: false, error: 'Skill tool requires a non-empty \"skill\" argument.' }, true), /缺少必填参数“skill”/);
});

for (const zh of [true, false]) {
  const locale = zh ? 'zh' : 'en';
  test(`${locale}: exact historical state failure is localized across error envelopes`, () => {
    const expected = copy(zh, key('historicalStateInvalid'));
    assert.match(expected, zh ? /历史合约状态缺失或格式无效/ : /incomplete or invalid historical contract data/);
    for (const failure of [
      { ok: false, error: { kind: 'provider_error', message: malformed, detail: { provider: 'uniswap', action: 'pool_history' } } },
      { ok: false, error: malformed },
      { isError: true, content: [{ type: 'text', text: JSON.stringify({ error: { message: malformed } }) }] },
      { isError: true, structuredContent: { error: { message: malformed } } },
      { result: { ok: false, error: { message: malformed } } },
    ]) {
      const original = JSON.stringify(failure);
      assert.equal(toolFailureSummary(failure, zh), expected);
      assert.doesNotMatch(expected, /Missing or malformed historical contract state/);
      assert.equal(JSON.stringify(failure), original, 'raw diagnostics stay unchanged');
    }
  });

  test(`${locale}: structured sample budget reason takes priority over provider prose`, () => {
    const expected = copy(zh, key('historicalSampleBudgetExceeded'));
    assert.match(expected, zh ? /采样数量或查询次数限制/ : /sample or query limit/);
    const error = { kind: 'schema_validation', message: 'Requested window requires 56 samples including both endpoints; max_samples allows 30. Requested step_blocks is 3457. The minimum step_blocks to fit max_samples is 6555.',
      detail: { provider: 'uniswap', action: 'pool_history', reason: budget, required_samples: 56, max_samples: 30 } };
    for (const failure of [
      { ok: false, error },
      { isError: true, content: [{ type: 'text', text: JSON.stringify({ error }) }] },
      { isError: true, structured_content: { error } },
      { message: error.message, diagnostics: { error } },
      { ok: false, recovery: { reason: budget } },
      { ok: false, error: { message: legacyBudget } },
      { isError: true, content: [{ type: 'text', text: legacyBudget }] },
    ]) {
      const original = JSON.stringify(failure);
      assert.equal(toolFailureSummary(failure, zh), expected);
      assert.doesNotMatch(expected, /historical_sample_budget_exceeded|56 samples/);
      assert.equal(JSON.stringify(failure), original);
    }
  });

  test(`${locale}: complete numeric producer message localizes without structured detail`, () => {
    for (const minimum of [budgetMinimum, 'No step_blocks can fit both endpoints with max_samples=1. ']) {
      for (const rpc of ['', budgetRpc]) {
        const message = (minimum === budgetMinimum ? budgetPrefix : budgetPrefix.replace('allows 30.', 'allows 1.')) + minimum + rpc + budgetSuffix;
        for (const failure of [message, { error: message }, { message }, { error: { message } },
          { isError: true, content: [{ type: 'text', text: message }] }]) {
          const original = JSON.stringify(failure);
          assert.equal(toolFailureSummary(failure, zh), copy(zh, key('historicalSampleBudgetExceeded')));
          assert.equal(JSON.stringify(failure), original);
        }
      }
    }
  });

  test(`${locale}: numeric signature rejects quotes, embedded prose and incomplete or altered grammar`, () => {
    for (const message of [
      `"${numericBudget}"`, `The report says: ${numericBudget}`, `${numericBudget} Additional prose.`,
      numericBudget.replace('56 samples', 'many samples'),
      numericBudget.replace('56 samples', '-56 samples'),
      numericBudget.replace(budgetSuffix, ''),
      numericBudget.replace('no request was changed.', 'the request was changed.'),
      numericBudget.replace('Requested step_blocks', '\nRequested step_blocks'),
    ]) assert.notEqual(toolFailureSummary({ error: { message } }, zh), copy(zh, key('historicalSampleBudgetExceeded')));
  });

  test(`${locale}: request payloads cannot classify an unrelated failure`, () => {
    const unrelated = 'The quote service is unavailable. Try again later.';
    for (const field of ['payload', 'arguments', 'request']) {
      for (const fake of [
        { error: { message: malformed, detail: { reason: budget } } },
        { error: { message: legacyBudget } },
        { error: { message: numericBudget } },
        JSON.stringify({ message: malformed, reason: budget }),
        { query: `${malformed}; ${budget}` },
      ]) {
        assert.equal(toolFailureSummary({ ok: false, [field]: fake }, zh), copy(zh, key('failed')));
        assert.equal(toolFailureSummary({ ok: false, error: unrelated, [field]: fake }, zh), unrelated);
        assert.equal(toolFailureSummary({ result: { ok: false, [field]: fake } }, zh), copy(zh, key('failed')));
      }
    }
  });

  test(`${locale}: embedded phrases and unrelated errors retain existing handling`, () => {
    for (const message of [
      `Provider documentation mentions ${malformed} as an example.`,
      `The report says: ${malformed}`,
      `The report says: ${legacyBudget}`,
      `${legacyBudget}: this is an example in documentation.`,
      'Historical contract state ABI decode failed',
      'The quote service is unavailable. Try again later.',
      'Requested window requires 56 samples including both endpoints.',
    ]) assert.equal(toolFailureSummary({ ok: false, error: { message } }, zh), message);
    for (const reason of [`${budget}_example`, 'unknown_provider_failure']) {
      assert.equal(toolFailureSummary({ ok: false, error: { detail: { reason } } }, zh), copy(zh, key('failed')));
    }
  });
}
