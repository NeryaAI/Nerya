import type { LiveEvent, NativeBlockEnvelope } from './chat';

export type BrowserCall = { id: string; operation: string; done: boolean; profileId?: string };
export type BrowserVisual = { action?: string; url?: string; ts?: number; cursor?: { x: number; y: number } | null;
  boxes?: { x: number; y: number; width: number; height: number }[] };
export type BrowserTraceEvent = {
  seq: number; ts: number; kind: string; action?: string; index?: number;
  target?: string; phase?: string; status?: string; error?: string; frame_id?: number;
  url?: string; tabs?: { id: string; url: string; selected: boolean }[];
};
export type BrowserTrace = {
  ok: boolean; error?: string; status: string; call_id?: string; conversation_id?: string;
  profile_id?: string; session_id?: string; paused?: boolean; controllable?: boolean;
  events: BrowserTraceEvent[]; cursor: number; storage_error?: boolean;
  frame_state?: string;
  visual?: BrowserVisual;
  frame?: { frame_id: number; ts: number; image: string; url?: string; visual?: BrowserVisual; tabs?: BrowserTraceEvent['tabs'] } | null;
};

const object = (v: unknown): Record<string, unknown> => v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {};

/** Only structured, actual browser tool calls create a live view; never parse prose. */
export function browserCalls(blocks: NativeBlockEnvelope[], events: LiveEvent[] = []): BrowserCall[] {
  const childBlocks: NativeBlockEnvelope[] = events.filter(e => e.kind === 'subagent.step' && e.skill === 'script_run' && typeof e.tool_call_id === 'string')
    .map(e => ({ block: { kind: e.step_kind === 'observe' ? 'tool_result' : 'tool_use', action: 'script_run',
      call_id: String(e.tool_call_id), payload: object(e.payload) } }));
  const calls = new Map<string, BrowserCall>();
  const done = new Set<string>();
  for (const env of [...blocks, ...childBlocks]) {
    const block = object(env.block || env);
    const id = String(block.call_id || '');
    if (block.kind === 'tool_result' && id) done.add(id);
    const payload = object(block.payload || block.input);
    if (block.action !== 'script_run' || payload.skill_id !== 'browser' || !id) continue;
    if (!['browser_session.py', 'scripts/browser_session.py'].includes(String(payload.name || payload.script))) continue;
    const args = Array.isArray(payload.args) ? payload.args : [];
    let command: Record<string, unknown> = {};
    const jsonIndex = args.indexOf('--json');
    try { if (jsonIndex >= 0 && typeof args[jsonIndex + 1] === 'string') command = object(JSON.parse(args[jsonIndex + 1])); } catch { /* Stream may still be partial. */ }
    if (command.backend === 'research' || ['camofox', 'cloakbrowser', 'lightpanda', 'obscura'].includes(String(command.engine)) || String(command.session_id || '').startsWith('bs_')) continue;
    calls.set(id, { id, operation: String(command.operation || 'browser'), done: false, profileId: String(command.profile_id || 'work') });
  }
  return [...calls.values()].map(call => ({ ...call, done: done.has(call.id) }));
}

export function mergeBrowserEvents(previous: BrowserTraceEvent[], next: BrowserTraceEvent[]): BrowserTraceEvent[] {
  return [...new Map([...previous, ...next].map(e => [e.seq, e])).values()].sort((a, b) => a.seq - b.seq);
}
