import type { ChatMessage, NativeBlock } from "./chat";
import { finalReplyText, publicReplyText, withoutLegacyTerminalText } from "./chatResults";
import { partitionTranscript, renderableExecutionBlocks } from "./executionTimeline";
import { projectMessageBlocks } from "./streamTranscript";
import type { FindEntry } from "../components/chat/timelineReveal";

/** Index the public surfaces in their rendered order, not the result canvas. */
export function conversationFindEntries(messages: ChatMessage[], publicOnly = false): FindEntry[] {
  return messages.map(message => {
    const id = message.backend_message_id || message.id;
    if (message.role === "user") return {id, text:message.text};
    if (publicOnly) return {id, text:finalReplyText(message)};
    if (message.turn?.external_call) return {id, text:""};
    const blocks = projectMessageBlocks(message).filter(env => {
      const b = (env.block || env) as NativeBlock;
      return (!env.role || env.role === "assistant") && (!b.role || b.role === "assistant") && !b.retry;
    });
    const {work, reply} = partitionTranscript(
      withoutLegacyTerminalText(message, renderableExecutionBlocks(blocks)),
      finalReplyText(message), publicReplyText(message, blocks),
    );
    const commentary = work.flatMap(env => {
      const b = (env.block || env) as NativeBlock;
      return b.kind === "text" && typeof b.text === "string" && b.text.trim() ? [b.text] : [];
    }).join("\n\n");
    const sections: NonNullable<FindEntry["sections"]> = [];
    if (commentary) sections.push({section:"commentary", text:commentary});
    if (reply) sections.push({section:"reply", text:reply});
    return {id, text:sections.map(part => part.text).join("\n\n"), sections};
  });
}
