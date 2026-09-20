import { topLevelDecisionText, type AssistantMessage, type ChatThread, type NativeBlock, type NativeBlockEnvelope, type TurnPayload } from "./chat";

export type ChatResult = { id: string; title: string; text: string; ts: number; turnId?: string; agentId?: string; attempt?: number };
const text = (value: unknown): string => typeof value === "string" ? value.trim() : "";
const normalized = (value: unknown): string => text(value).replace(/\s+/g, " ");

/** Final fields win over legacy reasoning; never publish an in-flight draft. */
export function finalReplyText(message: AssistantMessage): string {
  if (message.loading || message.error || !message.turn) return "";
  const turn = message.turn;
  return text(turn.reply_text) || text(turn.final_text) || text(turn.decision?.text)
    || topLevelDecisionText(turn);
}

/** Canvas reads the same saved turns as chat, not a second copy or a generated summary. */
export function collectChatResults(thread: ChatThread | null): ChatResult[] {
  if (!thread) return [];
  let request = thread.title;
  const results: ChatResult[] = [];
  for (const message of thread.messages) {
    if (message.role === "user") { request = message.text || request; continue; }
    const body = finalReplyText(message);
    if (body) results.push({ id: message.id, title: request.split("\n")[0].slice(0, 120),
      text: body, ts: message.ts, turnId: message.turn?.turn_id });
  }
  return results;
}

/** Remove only the final text/its streamed fragments from the execution trace. */
export function withoutFinalReply(blocks: NativeBlockEnvelope[], reply: string): NativeBlockEnvelope[] {
  const final = normalized(reply);
  if (!final) return blocks;
  return blocks.filter((env) => {
    const block = env.block || env as NativeBlock;
    if ((block.kind || env.kind) !== "text") return true;
    const chunk = normalized(block.text);
    return Boolean(chunk) && !final.includes(chunk);
  });
}

export function turnWithoutFinalReply(turn: TurnPayload, reply: string): TurnPayload {
  const final = normalized(reply);
  return { ...turn, blocks: withoutFinalReply(turn.blocks || [], reply),
    events: turn.events?.filter((event) => !final || normalized(event.text) !== final) };
}
