import type { AssistantMessage } from "./chat";

/** Only persisted host publication metadata supplies completion authority. */
export function assistantDeliveryFields(meta: Record<string, unknown>) {
  if (meta.source !== "assistant_task_result") return {};
  const completion = meta.delivery_completion as { allowed?: unknown; status?: unknown } | null;
  const rows = Array.isArray(meta.unresolved_deliveries) ? meta.unresolved_deliveries.filter(
    (row): row is { command_id: string; delivery_id: string; delivery_revision: number; reasons: string[] } =>
      !!row && typeof row === "object" && typeof row.command_id === "string"
      && typeof row.delivery_id === "string" && Number.isInteger(row.delivery_revision)
      && Array.isArray(row.reasons) && row.reasons.every((reason: unknown) => typeof reason === "string"),
  ) : [];
  const unique = new Map<string, typeof rows[number]>();
  for (const row of rows) {
    // Replayed host metadata is one record per command, delivery and revision.
    // Merge gaps only within that identity; unrelated deliveries stay distinct.
    const key = JSON.stringify([row.command_id, row.delivery_id, row.delivery_revision]);
    const previous = unique.get(key);
    unique.set(key, { ...row, reasons: [...new Set([...(previous?.reasons || []), ...row.reasons])].slice(0, 64) });
  }
  const unresolved = [...unique.values()].slice(0, 16);
  if (typeof meta.task_complete !== "boolean" && !completion && !unresolved.length) return {};
  return { task_complete: meta.task_complete === true && completion?.allowed === true
      && completion.status === "complete" && !unresolved.length,
    delivery_completion: { allowed: completion?.allowed === true,
      status: typeof completion?.status === "string" ? completion.status : "unavailable" },
    unresolved_deliveries: unresolved };
}

/** Project system-authored delivery wrappers without changing agent prose or history. */
export function projectAssistantTaskMessage(message: AssistantMessage): AssistantMessage {
  if (!(message.backend_message_id || message.id).startsWith("assistant_result:")) return message;
  const text = message.turn?.reply_text || message.turn?.final_text || "";
  const receipt = message.assistant_task;
  if (receipt && !receipt.legacy) return message;
  // Only the historical delivery envelope is peeled off; matching text inside
  // ordinary replies, code fences or new agent-authored results stays untouched.
  const legacy = /^\*\*([^\n]+)\*\* · [^\n]+\n\n([\s\S]*)\n\n\[[^\n]+\]\(\/chat\/(taskchat_[a-zA-Z0-9_-]+)\)$/.exec(text);
  if (!legacy || receipt && legacy[3] !== receipt.task_session_id) return message;
  return {
    ...message,
    assistant_task: {
      task_id: receipt?.task_id || "",
      task_session_id: legacy[3], title: legacy[1],
      state: receipt?.state || message.execution_status || "",
      legacy: false,
    },
    turn: { ...message.turn, reply_text: legacy[2] },
  };
}
