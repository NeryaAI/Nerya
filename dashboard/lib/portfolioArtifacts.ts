import { liveEventsToBlocks, type ChatThread } from "./chat";
import { record } from "./agentConversation";
import { portfolioSnapshot, type SnapshotAccount } from "./portfolioSnapshot";

export type PortfolioArtifact = { id: string; turnId: string; ts: number; accounts: SnapshotAccount[] };

/** Only recorded portfolio tools become snapshots; text and failed tools are not inferred. */
export function collectPortfolioArtifacts(thread: ChatThread | null): PortfolioArtifact[] {
  if (!thread) return [];
  return thread.messages.flatMap((message) => {
    if (message.role !== "assistant") return [];
    const blocks = [...liveEventsToBlocks([...(message.turn?.activity_events || []), ...(message.live_events || [])]), ...(message.turn?.blocks || [])];
    const calls = new Map<string, Record<string, unknown>>();
    for (const [i, envelope] of blocks.entries()) {
      const block = record(envelope.block || envelope);
      if (block.kind !== "tool_result" || block.action !== "portfolio_summary") continue;
      const key = `${block.attempt || 1}:${block.call_id || block.tool_use_id || block.tool_call_id || `record-${i}`}`;
      calls.set(key, block);
    }
    return [...calls].flatMap(([key, block]) => {
      const accounts = portfolioSnapshot(block);
      return accounts ? [{ id: `snapshot:${message.id}:${key}`, turnId: message.id, ts: message.ts, accounts }] : [];
    });
  });
}
