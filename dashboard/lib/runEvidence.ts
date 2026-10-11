import { taskRunApi, type TaskRun } from "./taskRuns";
import type { LiveEvent } from "./chat";

export type RunEvidence = { run: TaskRun; events: LiveEvent[]; cursor: number; hasMore: boolean };

/** Load bounded batches, not a silently truncated transcript. A terminal run can
 * still have more event pages; the caller keeps reading until hasMore is false.
 */
export async function loadRunEvidence(id: string, signal: AbortSignal, previous?: RunEvidence): Promise<RunEvidence> {
  const response = await taskRunApi.get(id, signal);
  if (!response.ok || response.run.run_id !== id) throw new Error("Run identity mismatch");
  let cursor = previous?.run.run_id === id ? previous.cursor : 0;
  const events = new Map<number, LiveEvent>((previous?.run.run_id === id ? previous.events : []).map(event => [event.seq, event]));
  let hasMore = false;
  for (let page = 0; page < 4; page += 1) {
    const batch = await taskRunApi.events(id, cursor, signal);
    if (!batch.ok || !Array.isArray(batch.events) || !Number.isFinite(batch.next_seq)) throw new Error("Run events unavailable");
    for (const event of batch.events) {
      if (typeof event.kind === "string" && typeof event.seq === "number") events.set(event.seq, event as LiveEvent);
    }
    hasMore = batch.has_more;
    if (hasMore && batch.next_seq <= cursor) throw new Error("Run event cursor did not advance");
    cursor = Math.max(cursor, batch.next_seq);
    if (!hasMore) break;
  }
  return { run: response.run, events: [...events.values()].sort((a, b) => a.seq - b.seq), cursor, hasMore };
}
