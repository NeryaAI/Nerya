import { copy as i18nCopy } from './i18n';
import { contentValue, record } from './agentConversation';

// Exact producer grammar, including its scope-preservation clause, when detail.reason is lost.
const historicalSampleBudgetMessage = /^Requested window requires [1-9]\d* samples including both endpoints; max_samples allows [1-9]\d*\. Requested step_blocks is [1-9]\d*\. (?:The minimum step_blocks to fit max_samples is [1-9]\d*\.|No step_blocks can fit both endpoints with max_samples=1\.) (?:This request also requires [1-9]\d* RPC requests; the request budget allows [1-9]\d*\. A sample-only correction may not fit it\. )?Preserve the requested pool, window and tick bounds; no request was changed\. Do not retry unchanged or raise budgets; any step change requires explicit caller review\.$/;

/** Error-bearing fields only; request arguments never determine a diagnosis. */
function failureText(value: unknown, depth = 0): string[] {
  if (depth > 5 || value == null) return [];
  if (typeof value === 'string') {
    const decoded = contentValue(value);
    return typeof decoded === 'string' ? [decoded] : failureText(decoded, depth + 1);
  }
  const data = record(value);
  const texts = ['error', 'error_code', 'code', 'reason', 'message', 'detail', 'stderr']
    .flatMap(key => failureText(data[key], depth + 1));
  for (const key of ['result', 'data', 'diagnostics', 'recovery', 'structuredContent', 'structured_content']) {
    if (data[key] != null) texts.push(...failureText(data[key], depth + 1));
  }
  if (Array.isArray(data.blockers)) for (const blocker of data.blockers.slice(0, 40)) {
    texts.push(...failureText(blocker, depth + 1));
  }
  if (Array.isArray(data.content)) for (const part of data.content.slice(0, 40)) {
    const item = record(part);
    if (item.type === 'text') texts.push(...failureText(item.text, depth + 1));
    else if (item.type === 'json' || item.type === 'shell') texts.push(...failureText(item.data, depth + 1));
  }
  return texts.filter(text => text.trim());
}

/** Shared public summary; callers retain exact diagnostics in disclosure. */
export function toolFailureSummary(value: unknown, zh: boolean): string {
  const texts = failureText(value);
  if (texts.some(text => text.trim() === 'decision_response_truncated')) {
    return i18nCopy(zh, 'copy.components_chat_ReadableExecution.decisionResponseTruncated');
  }
  if (record(record(value).recovery).action === 'inspect_pool_discovery') {
    return i18nCopy(zh, 'copy.components_chat_ReadableExecution.poolDiscoveryIncomplete');
  }
  if (texts.some(text => text.trim() === 'protocol_replay_model_required')) {
    return i18nCopy(zh, 'copy.components_chat_ReadableExecution.protocolReplayRequired');
  }
  if (texts.some(text => text.includes('archive RPC') || text.includes('RPC deadline'))) {
    return i18nCopy(zh, 'copy.components_chat_ReadableExecution.historicalRpcDeadline');
  }
  if (texts.some(text => text.includes('The historical read produced no verified historical state'))) {
    return i18nCopy(zh, 'copy.components_chat_ReadableExecution.historicalReadFailed');
  }
  if (texts.some(text => text.includes('lp_pool_identity_required')
    || text.includes('missing a complete canonical pool declaration'))) {
    return i18nCopy(zh, 'copy.components_chat_ReadableExecution.poolBindingRequired');
  }
  if (texts.some(text => text.trim() === 'historical_provider_unsupported')) {
    return i18nCopy(zh, 'copy.components_chat_ReadableExecution.historicalProviderUnsupported');
  }
  if (texts.some(text => text.trim() === 'historical_rpc_authorization_required')) {
    return i18nCopy(zh, 'copy.components_chat_ReadableExecution.historicalAuthorizationRequired');
  }
  if (texts.some(text => text.trim() === 'Missing or malformed historical contract state')) {
    return i18nCopy(zh, 'copy.components_chat_ReadableExecution.historicalStateInvalid');
  }
  if (texts.some(text => ['historical_sample_budget_exceeded', 'Requested samples exceed sample or request budget'].includes(text.trim())
    || historicalSampleBudgetMessage.test(text.trim()))) {
    return i18nCopy(zh, 'copy.components_chat_ReadableExecution.historicalSampleBudgetExceeded');
  }
  if (texts.some(text => /^request_scope_unverified(?:\b|:)/.test(text.trim()))) {
    return i18nCopy(zh, 'copy.components_chat_ReadableExecution.scopeUnverified');
  }
  if (texts.some(text => /^request_scope_mismatch(?:\b|:)/.test(text.trim()))) {
    return i18nCopy(zh, 'copy.components_chat_ReadableExecution.scopeMismatch');
  }
  const unexpected = texts.map(text => text.trim().match(/^unexpected field:\s*([\w.-]+)$/i)).find(Boolean);
  if (unexpected) return i18nCopy(zh, 'copy.components_chat_ReadableExecution.unexpectedField', {field: unexpected[1]});
  if (texts.some(text => /^Skill tool requires a non-empty (?:"skill"|skill) argument\.?$/.test(text.trim()))) {
    return i18nCopy(zh, 'copy.components_chat_ReadableExecution.requiredParameter', {field: 'skill'});
  }
  const explanation = texts.find(text => {
    const trimmed = text.trim();
    return trimmed.length <= 600 && /[\s。！？.!?]/.test(trimmed)
      && !/[{}\[\]\n]|\b\w+_\w+\b|Traceback|\b\w*Error:/.test(trimmed);
  });
  return explanation || i18nCopy(zh, 'copy.components_chat_ReadableExecution.failed');
}
