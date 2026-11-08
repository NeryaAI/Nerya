import type { AssistantMessage } from "./chat";

/** A text-only notice, not a completion verdict or a synthesized receipt. */
export function shouldShowTurnExecutionNotice(message: AssistantMessage, finalReply: string): boolean {
  const turn = message.turn;
  const settings = turn?.context_snapshot?.run_settings as { work_mode?: unknown } | undefined;
  const verifier = turn?.verifier_outcome;
  if (message.loading || !finalReply || turn?.delivery != null || settings?.work_mode !== "execute"
    || verifier?.model_done !== true || verifier.hard_status !== "missing" || verifier.trusted !== false
    || verifier.has_hard_evidence !== false || verifier.has_validation_attempt !== false
    // An absent trace in old metadata does not establish an empty trace.
    || !Array.isArray(turn?.tool_trace)) return false;

  // Exact structured action identity only. Sending the reply is not execution.
  const hasExecution = (record: { action?: unknown }) => record.action !== "send_message";
  if (turn.tool_trace.some(hasExecution) || turn.actions?.some(hasExecution)
    || turn.decision?.actions?.some(hasExecution)
    || (turn.decision?.action && turn.decision.action !== "send_message")) return false;
  if (turn.blocks?.some(envelope => {
    const block = envelope.block || envelope;
    return ["tool_use", "tool_result"].includes(String(block.kind || envelope.kind)) && block.action !== "send_message";
  })) return false;
  return ![...(turn.activity_events || []), ...(message.live_events || [])].some(event =>
    ["tool.start", "tool.complete", "tool.progress", "tool.output"].includes(event.kind) && hasExecution(event));
}
