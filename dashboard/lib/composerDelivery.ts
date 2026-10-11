import type { CommandSnapshot, ConversationCommand } from "./conversationCommands";

export type ComposerDeliveryInput = {
  commands: readonly Pick<ConversationCommand, "kind" | "state" | "created_at">[];
  queue?: Pick<CommandSnapshot["queue"], "paused" | "pause_reason"> | null;
  connection: "online" | "connecting" | "offline" | "incompatible";
  pendingCount?: number;
  awaitingInput?: boolean;
  awaitingApproval?: boolean;
};
export type ComposerDelivery = {
  mode: "send" | "queue";
  reason: "idle" | "active" | "queued" | "held_queue" | "paused_empty" |
    "connection_unavailable" | "delivery_unconfirmed" | "execution_unconfirmed" |
    "awaiting_input" | "awaiting_approval";
  canSubmit: boolean;
  /** One explicit send may run while an *empty* session queue remains held.
   * This is only a per-message intent; it does not resume the queue or bypass
   * server-side decision, unknown-execution, lease, or approval guards. */
  runOnly: boolean;
};

/** Presentation only. The store owns admission; never resume a queue from this result. */
export function composerDelivery(input: ComposerDeliveryInput): ComposerDelivery {
  const work = input.commands.filter(command => command.kind !== "guide");
  const active = work.some(command => command.state === "running" || command.state === "stopping");
  const queued = work.some(command => command.state === "queued");
  const mode = active || queued ? "queue" : "send";
  const blocked = (reason: ComposerDelivery["reason"]): ComposerDelivery => ({
    mode, reason, canSubmit: false, runOnly: false,
  });
  if (input.connection !== "online") return blocked("connection_unavailable");
  if (input.pendingCount) return blocked("delivery_unconfirmed");
  if (work.some(command => command.state === "unconfirmed")) return blocked("execution_unconfirmed");

  // Snapshots are ordered by queue position, not execution time. Continuations
  // can precede their original waiting command in that array.
  const latest = work.filter(command => command.state !== "queued" && command.state !== "removed")
    .sort((a, b) => b.created_at - a.created_at)[0];
  if (input.awaitingInput || latest?.state === "awaiting_input") return blocked("awaiting_input");
  if (input.awaitingApproval || latest?.state === "awaiting_approval") return blocked("awaiting_approval");
  const paused = Boolean(input.queue?.paused);
  return {
    mode,
    reason: active ? "active" : queued ? (paused ? "held_queue" : "queued") : paused ? "paused_empty" : "idle",
    canSubmit: true,
    // ZCode's held-queue confirmation is for choosing what to do with actual
    // queued messages, not for sending a follow-up to an empty queue. A send
    // is already the user's explicit per-message action; use the existing
    // backend run_once lane rather than asking a second time. Preserve the
    // hold and never leapfrog queued work or a running turn.
    runOnly: !active && !queued && paused,
  };
}
