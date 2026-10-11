import type { AssistantMessage } from "./chat";

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
