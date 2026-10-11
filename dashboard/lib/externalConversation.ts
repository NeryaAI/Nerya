import { copy as i18nCopy } from "./i18n";
import { callDepth, publicProgress, recordOf, type ExternalCallNode, type ExternalCallTrace } from './externalCalls';

import type { UserMessage } from './chat';

export type ConversationEntry =
  | { kind: 'user'; id: string; message: UserMessage }
  | { kind: 'message'; id: string; label: string; text: string; trace: ExternalCallTrace; state?: string }
  | { kind: 'tool'; id: string; trace: ExternalCallTrace; node: ExternalCallNode; depth: number; mirroredId?: string };
const activityOrder = ['intent', 'hypothesis', 'evidence', 'conclusion', 'next', 'status'] as const;
export const toolName = (name: string) => name.replace(/^nerya_(?:native_)?/, '');
const textOf = (value: unknown): string => typeof value === 'string' ? value : '';
function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  if (value && typeof value === 'object') return JSON.stringify(Object.keys(value).sort().map(key => [key, stable(recordOf(value)[key])]));
  return JSON.stringify(value) ?? '';
}

/** A call updates in place; a turn is NOT a display group. Never regroup A/B/A turns. */
export function conversationEntries(traces: ExternalCallTrace[], users: UserMessage[] = []): ConversationEntry[] {
  const calls = [...new Map(traces.map(trace => [trace.call_id, trace])).values()].sort((a, b) =>
    (a.sequence != null && b.sequence != null ? a.sequence - b.sequence : 0) ||
    (Date.parse(a.started_at || '') || 0) - (Date.parse(b.started_at || '') || 0));
  const entries: ConversationEntry[] = [];
  for (const trace of calls) {
    const seen = new Set<string>();
    const message = (label: string, value: unknown, suffix = label, state?: string) => {
      const text = textOf(value).trim();
      if (!text || seen.has(text)) return;
      seen.add(text);
      entries.push({ kind: 'message', id: `${trace.call_id}:${suffix}`, label, text, trace, state });
    };
    // Authored activity describes facts known BEFORE this tool call, not its future result.
    for (const key of activityOrder) message(key, trace.activity?.[key]);
    message('purpose', trace.purpose);
    if (trace.tool === 'nerya_progress' && trace.status === 'succeeded') {
      const progress = publicProgress(trace);
      message('progress', progress.current, 'progress', textOf(progress.status));
      if (Array.isArray(progress.result)) progress.result.forEach((result, i) => message('result', result, `result-${i}`));
      message('next', progress.next, 'progress-next');
      continue;
    }
    const nodes = trace.nodes || [];
    // The native dispatcher is a 1:1 transport envelope, not a second file read.
    // Coalesce ONLY an exact direct mirror; meaningful descendants remain separate rows.
    const mirror = nodes.find(node => node.parent_call_id === trace.call_id &&
      toolName(node.tool) === toolName(trace.tool) && stable(node.arguments) === stable(trace.arguments));
    entries.push({ kind: 'tool', id: trace.call_id, trace, node: trace, depth: 0, mirroredId: mirror?.call_id });
    for (const node of nodes) {
      if (node === mirror) continue;
      entries.push({ kind: 'tool', id: `${trace.call_id}:${node.call_id}`, trace, node,
        depth: Math.max(1, callDepth(node, nodes, trace.call_id) + (mirror ? 0 : 1)) });
    }
  }
  if (users.length) {
    for (const message of new Map(users.map(user => [user.backend_message_id || user.id, user])).values())
      entries.push({ kind: 'user', id: message.backend_message_id || message.id, message });
    const sequence = (entry: ConversationEntry) => entry.kind === 'user' ? entry.message.external_request?.sequence : entry.trace.sequence;
    const timestamp = (entry: ConversationEntry) => entry.kind === 'user' ? entry.message.ts : Date.parse(entry.trace.started_at || '') || 0;
    entries.sort((a, b) => { const x = sequence(a), y = sequence(b);
      return x != null && y != null ? x - y : timestamp(a) - timestamp(b); });
  }
  return entries;
}

function decode(value: unknown): unknown {
  if (typeof value !== 'string' || value.length > 1_048_576 || !/^[\s]*[\[{]/.test(value)) return value;
  try { return JSON.parse(value); } catch { return value; }
}
export function plainOutput(value: string): string {
  return value.replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, '').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\r(?!\n)/g, '\n').replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '');
}

export type ToolOutput = { data: Record<string, unknown>; text: string; stdout: string; stderr: string;
  diffs: string[]; error: string; truncated: boolean };
/** Only unwrap documented result envelopes/content parts, never arbitrary business fields. */
export function toolOutput(value: unknown): ToolOutput {
  const data: Record<string, unknown> = {};
  const texts: string[] = [], diffs: string[] = [];
  let stdout = '', stderr = '', error = '', truncated = false;
  const add = (list: string[], text: unknown) => { if (typeof text === 'string' && text && !list.includes(text)) list.push(text); };
  function visit(input: unknown, depth = 0) {
    if (depth > 4) return;
    const decoded = decode(input);
    if (typeof decoded === 'string') { add(texts, decoded); return; }
    if (Array.isArray(decoded)) { data.items = decoded; return; }
    const row = recordOf(decoded);
    Object.assign(data, recordOf(row.metadata), row);
    if (typeof row.error === 'string') error = row.error;
    else if (typeof recordOf(row.error).message === 'string') error = String(recordOf(row.error).message);
    truncated ||= row.truncated === true || row.truncated_bytes === true || row.log_truncated === true;
    if (typeof row.stdout === 'string') stdout = row.stdout;
    if (typeof row.stderr === 'string') stderr = row.stderr;
    add(diffs, row.diff);
    if (row.structuredContent) visit(row.structuredContent, depth + 1);
    if (row.data && !Array.isArray(row.data)) visit(row.data, depth + 1);
    const parts = Array.isArray(row.content) ? row.content : [];
    for (const part of parts) {
      const p = recordOf(part);
      if (p.type === 'json' || p.type === 'shell') visit(p.data, depth + 1);
      else if (p.type === 'diff') add(diffs, p.text);
      else if (p.type === 'stdout') stdout += textOf(p.text);
      else if (p.type === 'stderr') stderr += textOf(p.text);
      else if (p.type === 'text') visit(p.text, depth + 1);
    }
    if (!parts.length) {
      if (typeof row.content === 'string') add(texts, row.content);
      else if (typeof row.text === 'string') {
        const decodedText = decode(row.text);
        if (decodedText !== row.text) visit(decodedText, depth + 1);
        else add(texts, row.text);
      } else if (typeof row.summary === 'string') add(texts, row.summary);
      else if (typeof row.message === 'string') add(texts, row.message);
      else if (typeof row.preview === 'string') add(texts, row.preview);
    }
  }
  visit(value);
  return { data, text: plainOutput(texts.join('\n\n')), stdout: plainOutput(stdout), stderr: plainOutput(stderr),
    diffs, error, truncated };
}

export type ToolKind = 'read' | 'files' | 'search' | 'edit' | 'shell' | 'script' | 'web' | 'tool';
export function toolKind(node: ExternalCallNode): ToolKind {
  const name = toolName(node.tool).toLowerCase();
  if (['read_file', 'read'].includes(name)) return 'read';
  if (['list_dir', 'glob'].includes(name)) return 'files';
  if (['grep', 'search', 'search_files'].includes(name)) return 'search';
  if (['edit_file', 'write_file', 'edit', 'apply_patch'].includes(name)) return 'edit';
  if (['run_shell', 'execute', 'shell', 'bash'].includes(name)) return 'shell';
  if (['script_run', 'run_python', 'run_script'].includes(name)) return 'script';
  if (/web|browser|fetch_url/.test(name)) return 'web';
  return 'tool';
}

export function targetsOf(node: ExternalCallNode, data: Record<string, unknown> = {}): string[] {
  const args = recordOf(node.arguments), targets: string[] = [];
  const add = (v: unknown) => { if (typeof v === 'string' && v.trim() && !targets.includes(v)) targets.push(v); };
  for (const key of ['path', 'file', 'dir', 'target', 'url', 'script', 'script_path']) add(args[key]);
  for (const key of ['paths', 'items', 'edits']) {
    const items = args[key];
    if (Array.isArray(items)) for (const item of items) add(typeof item === 'string' ? item : recordOf(item).path);
  }
  if (!targets.length) add(data.path);
  return targets;
}

export function toolTitle(node: ExternalCallNode, zh: boolean): string {
  const nativeTitles: Record<string, string> = {
    skill: "copy.lib_externalConversation.001", skill_view: "copy.lib_externalConversation.002",
    strategy_draft_proposal: "copy.lib_externalConversation.003",
    strategy_generate_proposal: "copy.lib_externalConversation.004",
    strategy_validate: "copy.lib_externalConversation.005",
    strategy_submit_proposal: "copy.lib_externalConversation.006",
    strategy_backtest: "copy.lib_externalConversation.007",
    strategy_run_tick: "copy.lib_externalConversation.008",
  };
  const native = nativeTitles[toolName(node.tool).toLowerCase()];
  if (native) return i18nCopy(zh, native ?? "");
  const kind = toolKind(node);
  const labels: Record<ToolKind, string> = {
    read: "copy.lib_externalConversation.009", files: "copy.lib_externalConversation.010", search: "copy.lib_externalConversation.011",
    edit: "copy.lib_externalConversation.012", shell: "copy.lib_externalConversation.013", script: "copy.lib_externalConversation.014",
    web: "copy.lib_externalConversation.015", tool: "copy.lib_externalConversation.016",
  };
  if (toolName(node.tool) === 'write_file') return i18nCopy(zh, "copy.lib_externalConversation.017");
  return i18nCopy(zh, labels[kind] ?? "");
}

/** Native read_file offsets are zero-based. Returned range beats requested range. */
export function fileRange(node: ExternalCallNode, output: ToolOutput) {
  const args = recordOf(node.arguments), data = output.data;
  const rawOffset = typeof data.offset === 'number' ? data.offset : args.offset;
  const offset = typeof rawOffset === 'number' && rawOffset >= 0 ? rawOffset : null;
  const limit = typeof data.limit === 'number' ? data.limit : typeof args.limit === 'number' ? args.limit : null;
  return { start: offset === null ? null : offset + 1, end: offset !== null && limit !== null ? offset + limit : null,
    total: typeof data.total_lines === 'number' ? data.total_lines : null };
}

export function fileBody(node: ExternalCallNode, output: ToolOutput): string {
  let body = output.text;
  // Native read_file prepends this display header; it is not file line 1.
  if (toolKind(node) === 'read') body = body.replace(/^# [^\n]+ \(\d+ lines, \d+ bytes\)\n\n/, '');
  return body;
}

export function resultRows(output: ToolOutput): Array<{ path: string; line?: string; text: string }> {
  const data = output.data;
  const rows = [data.matches, data.entries, data.files, data.results, data.items].find(Array.isArray);
  if (!Array.isArray(rows)) return [];
  return rows.map(item => {
    if (typeof item === 'string') return { path: item, text: '' };
    const row = recordOf(item), line = row.line_number ?? row.line;
    return { path: String(row.path ?? row.name ?? row.file ?? ''),
      line: typeof line === 'number' || typeof line === 'string' ? String(line) : undefined,
      text: textOf(row.text ?? row.match ?? row.kind ?? row.type) };
  }).filter(row => row.path || row.text);
}
