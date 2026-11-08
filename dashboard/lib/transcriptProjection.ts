import type { NativeBlock, NativeBlockEnvelope } from "./chat";
import { mergeStreamText } from "./streamEvents";

/** Runtime persists both deltas and snapshots. A snapshot replaces the original
 * row instead of appending a second copy, including after reload/interruption. */
export function normalizeTranscriptBlocks(envelopes: NativeBlockEnvelope[]): NativeBlockEnvelope[] {
  const rows: NativeBlockEnvelope[] = [];
  const streams = new Map<string, number>();
  const seen = new Set<string>();
  const streamKey = (envelope: NativeBlockEnvelope, block: NativeBlock) =>
    [block.epoch || envelope.epoch || "", block.turn_id || envelope.turn_id || "", block.stream_id || ""].join(":");
  // A retry revokes only the failed model preview, never tool receipts. Scan
  // first so reconnects and late deltas cannot resurrect a discarded attempt.
  const discarded = new Set(envelopes.filter(envelope => {
    const block = (envelope?.block || envelope) as NativeBlock;
    return block?.kind === "model_transport" && block.status === "discarded" && block.stream_id;
  }).map(envelope => streamKey(envelope, (envelope.block || envelope) as NativeBlock)));
  for (const envelope of envelopes) {
    if (!envelope || typeof envelope !== "object" || envelope.role === "user") continue;
    const block = (envelope.block || envelope) as NativeBlock;
    const originalKind = String(block.kind || envelope.kind || "");
    const isDelta = originalKind === "text_delta" || originalKind === "thinking_delta";
    const kind = isDelta ? originalKind.replace(/_delta$/, "") : originalKind;
    if (block.stream_id && discarded.has(streamKey(envelope, block)) &&
        (["text", "thinking", "tool_input_delta"].includes(kind) ||
         (kind === "tool_use" && block.phase === "input_streaming"))) continue;
    // Only deduplicate actual durable envelope IDs; equal fragments are valid.
    const id = envelope.event_id
      ? `event:${String(envelope.event_id)}`
      : envelope.seq !== undefined
      ? `${envelope.epoch || ""}:${envelope.turn_id || ""}:${envelope.seq}`
      : "";
    if (id && seen.has(id)) continue;
    if (id) seen.add(id);
    if ((kind === "text" || kind === "thinking") && block.stream_id && !block.retry) {
      const domain = String(block.epoch || envelope.epoch || "") + ":" + String(block.turn_id || envelope.turn_id || "");
      const key = kind + ":" + domain + ":" + String(block.stream_id);
      const index = streams.get(key);
      const previous = index === undefined ? undefined : rows[index];
      const prior = previous?.block;
      if (prior?.completed === true && isDelta) {
        continue;
      }
      const text = mergeStreamText(
        String(prior?.text || ""),
        String(block.text || ""),
        isDelta ? "append" : "replace",
      );
      const row: NativeBlockEnvelope = {
        ...(previous || envelope), kind,
        block: { ...prior, ...block, kind, text, index: prior?.index ?? block.index ?? rows.length,
          completed: block.completed ?? (!isDelta),
        },
      };
      if (index === undefined) { streams.set(key, rows.length); rows.push(row); }
      else rows[index] = row;
    } else if (!["model_transport", "tool_input_delta", "usage"].includes(originalKind)) {
      rows.push(isDelta ? { ...envelope, kind, block: { ...block, kind, completed:false } } : envelope);
    }
  }
  let tail = -1;
  for (let i=0;i<rows.length;i++) if (["text","thinking","tool_use","tool_result"].includes(String((rows[i].block || rows[i]).kind))) tail=i;
  return rows.map((envelope,index) => {
    const block = (envelope.block || envelope) as NativeBlock;
    return block.kind === "text" || block.kind === "thinking"
      ? { ...envelope, block: { ...block, presentation_active: index === tail && block.completed !== true } }
      : envelope;
  });
}
