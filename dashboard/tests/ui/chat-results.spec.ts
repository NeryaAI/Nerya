import { test, expect } from "@playwright/test";
import { collectChatResults, finalReplyText, turnWithoutFinalReply, withoutFinalReply } from "../../lib/chatResults";
import type { AssistantMessage, ChatThread, NativeBlockEnvelope } from "../../lib/chat";

const message: AssistantMessage = { id: "answer", role: "assistant", ts: 20,
  turn: { reply_text: "Final result", final_text: "Other final", decision: { text: "Legacy answer", reasoning: "Earlier reasoning" } } };

test("final fields win, while loading and errors never project a final result", () => {
  expect(finalReplyText(message)).toBe("Final result");
  expect(finalReplyText({ ...message, loading: true })).toBe("");
  expect(finalReplyText({ ...message, error: "failed" })).toBe("");
  expect(finalReplyText({ ...message, turn: { final_text: "Final field" } })).toBe("Final field");
  expect(finalReplyText({ ...message, turn: { decision: { text: "Legacy answer" } } })).toBe("Legacy answer");
});

test("final text and streamed fragments are removed without deleting tool evidence", () => {
  const blocks: NativeBlockEnvelope[] = [
    { block: { kind: "tool_use", action: "read_file" } },
    { block: { kind: "tool_result", result: "Final result", ok: true } },
    { block: { kind: "text", text: "First I will read the file." } },
    { block: { kind: "text", text: "Final\nresult" } },
    { kind: "text", text: "result" },
  ];
  const remaining = withoutFinalReply(blocks, "Final result");
  expect(remaining).toHaveLength(3);
  expect(remaining[1].block?.result).toBe("Final result");
  expect(blocks).toHaveLength(5);
  expect(turnWithoutFinalReply({ blocks, events: [{ text: "Final result" }, { text: "Read complete" }] }, "Final result").events).toEqual([{ text: "Read complete" }]);
});

test("Canvas versions follow source turns and disappear with edited or deleted messages", () => {
  const thread: ChatThread = { id: "session-one", title: "Task", created_ts: 1, updated_ts: 30, messages: [
    { id: "u1", role: "user", text: "First task", ts: 10 }, message,
    { id: "u2", role: "user", text: "Second task", ts: 21 },
    { ...message, id: "answer2", turn: { reply_text: "Second result" }, ts: 22 },
    { ...message, id: "draft", loading: true },
  ] };
  expect(collectChatResults(thread).map((r) => [r.id, r.title, r.text])).toEqual([
    ["answer", "First task", "Final result"], ["answer2", "Second task", "Second result"],
  ]);
  const updated = { ...thread, messages: thread.messages.filter((m) => m.id !== "answer") };
  expect(collectChatResults(updated).map((r) => r.id)).toEqual(["answer2"]);
  expect(collectChatResults({ ...thread, id: "other", messages: [] })).toEqual([]);
  expect(collectChatResults(null)).toEqual([]);
});
