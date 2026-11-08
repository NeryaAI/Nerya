import type { ArtifactIndex, VerifierOutcome, ExecutionState } from "./workbench";
import { type AssistantMessage, type ChatThread, type NativeBlock, type NativeBlockEnvelope, type TaskDelivery, type TurnPayload } from "./chat";
import type { ConversationCommand } from "./conversationCommands";
import { isAbortedTerminal, legacyTerminalSummary } from "./legacyTerminalSummary";

export type ChatResult = { id: string; title: string; text: string; ts: number; turnId?: string; agentId?: string; taskSessionId?: string; attempt?: number; delivery?: TaskDelivery | null; evidence?: { artifacts?: ArtifactIndex; verifier?: VerifierOutcome; execution?: ExecutionState } };
const text = (value: unknown): string => typeof value === "string" ? value.trim() : "";
const normalized = (value: unknown): string => text(value).replace(/\s+/g, " ");

function publicTextBlocks(blocks: NativeBlockEnvelope[]): string[] {
  return blocks.flatMap(env => {
    const block = env.block || env as NativeBlock;
    return (!env.role || env.role === "assistant") && (!block.role || block.role === "assistant")
      && (block.kind || env.kind) === "text" && typeof block.text === "string" ? [block.text] : [];
  });
}

/** Only explicitly public fields can become a result; reasoning is never a fallback. */
export function finalReplyText(message: AssistantMessage): string {
  // External call/progress rows belong to the conversation, never the final-answer canvas.
  if (message.loading || !message.turn || message.turn.external_call) return "";
  const turn = message.turn;
  const body = text(turn.reply_text) || text(turn.final_text) || text(turn.decision?.text);
  // Block-only legacy finals must take the same path as explicit final fields.
  const legacy = legacyTerminalSummary(message, body || publicTextBlocks(turn.blocks || []).at(-1) || "");
  if (legacy) return legacy;
  return message.error && !isAbortedTerminal(message) ? "" : body;
}

/** Reuse the event reducer's text blocks; tool outputs and thinking stay in the trace. */
export function publicReplyText(message: AssistantMessage, blocks: NativeBlockEnvelope[]): string {
  if (message.turn?.external_call) return "";
  const final = finalReplyText(message);
  if (final) return final;
  const bodies = publicTextBlocks(blocks);
  return legacyTerminalSummary(message, bodies.at(-1) || "") || bodies.join("\n\n");
}

/** Keep recognized legacy diagnostics available in the existing turn dialog. */
export function legacyReplyDiagnostic(message: AssistantMessage): string {
  const turn = message.turn;
  const candidates = [turn?.reply_text, turn?.final_text, turn?.decision?.text,
    ...publicTextBlocks(turn?.blocks || [])].map(text);
  return [...new Set(candidates.filter(body => legacyTerminalSummary(message, body) !== null))].join("\n\n");
}

/** Host diagnostics stay inspectable even when the public final is concise. */
export function terminalReplyDiagnostic(message: AssistantMessage): string {
  const legacy = legacyReplyDiagnostic(message);
  if (!isAbortedTerminal(message)) return legacy;
  const details = (message.turn?.blocks || []).flatMap(env => {
    const block = env.block || env as NativeBlock;
    return block.kind === "model_transport" && block.status === "interrupted" ? [text(block.detail)] : [];
  });
  return [...new Set([legacy, ...details, text(message.error), text(message.turn?.error)]
    .filter(Boolean))].join("\n\n");
}

/** Omit only recognized host summaries from narration, never from saved data. */
export function withoutLegacyTerminalText(message: AssistantMessage, blocks: NativeBlockEnvelope[]): NativeBlockEnvelope[] {
  return blocks.filter(env => !publicTextBlocks([env]).some(body => legacyTerminalSummary(message, body) !== null));
}

export function withoutPublicText(blocks: NativeBlockEnvelope[]): NativeBlockEnvelope[] {
  return blocks.filter(env => ((env.block || env as NativeBlock).kind || env.kind) !== "text");
}

/** Canvas reads the same saved turns as chat, not a second copy or a generated summary. */
export function collectChatResults(thread: ChatThread | null, commands: ConversationCommand[] = []): ChatResult[] {
  if (!thread) return [];
  const owned = commands.filter(command => command.session_id === thread.id);
  const byCommand = new Map(owned.map(command => [command.command_id, command]));
  const byTurn = new Map(owned.map(command => [command.turn_id, command]));
  let request = thread.title;
  const results: ChatResult[] = [];
  for (const message of thread.messages) {
    if (message.role === "user") { request = message.text || request; continue; }
    const body = finalReplyText(message);
    if (body) {
      const commandId = message.command_id || message.turn?.command_id;
      const command = commandId ? byCommand.get(commandId)
        : message.turn?.turn_id ? byTurn.get(message.turn.turn_id) : undefined;
      // Match live projection: a current snapshot owns delivery, including null.
      // Ordinary follow-ups must never borrow another command's turn evidence.
      const snapshot = command && command.revision >= (message.command_revision || 0);
      const projected = snapshot && command.delivery !== undefined ? command.delivery : message.turn?.delivery;
      const delivery = projected && (!commandId || projected.command_id === commandId)
        && (!command || projected.command_id === command.command_id) ? projected : null;
      results.push({ id: message.id, title: request.split("\n")[0].slice(0, 120),
        text: body, ts: message.ts, turnId: message.turn?.turn_id, delivery, evidence: { artifacts:message.turn?.artifact_index,verifier:message.turn?.verifier_outcome,execution:message.turn?.execution_state } });
    }
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
