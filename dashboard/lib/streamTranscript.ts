import { liveEventsToBlocks, type AssistantMessage } from "./chat";
import { mergeStreamEvents } from "./streamEvents";
import { enrichCommittedBlocks } from "./executionTimeline";

/** One projection for chat, result cards and workspace panels. */
export function projectMessageBlocks(message: AssistantMessage) {
  const turnId = message.turn?.turn_id || (message.backend_message_id?.endsWith(":assistant")
    ? message.backend_message_id.slice(0, -10) : "");
  const events = mergeStreamEvents(message.turn?.activity_events || [], message.live_events || []).filter(event => {
    if (!turnId || !["approval.request", "approval.resolved"].includes(event.kind)) return true;
    const record = event.record && typeof event.record === "object" ? event.record as Record<string, unknown> : {};
    const eventTurnId = String(event.turn_id || record.turn_id || "");
    // Cached events can outlive an incorrect attachment; never project another
    // turn's approval into this receipt while waiting for polling to repair it.
    return !eventTurnId || eventTurnId === turnId;
  });
  // The live reducer renders content, so carry terminal transport metadata to
  // the shared projection even before polling replaces the running snapshot.
  const interrupted = events.filter(event => event.kind === "model.model_transport" && event.status === "interrupted")
    .map(event => ({ block: { kind: "model_transport", status: "interrupted",
      stream_id:event.stream_id, epoch:event.epoch, session_id:event.session_id, turn_id:event.turn_id } }));
  return enrichCommittedBlocks(message.turn?.blocks || [], [...liveEventsToBlocks(events), ...interrupted], Boolean(message.loading));
}
