import { test, expect } from "@playwright/test";
import { agentRuns, contentValue, pairOperations, readableResult, safeLink, toolPresentation } from "../../lib/agentConversation";
import type { AgentDetail, AgentOperation, AgentWork } from "../../components/chat/useAgentWork";
const row: AgentWork = { id: "child", session_id: "s", group_id: "g", parent_call_id: "call", name: "Reviewer", title: "Review", state: "running", attempt: 2, updated_at: 100, output: { summary: "Old result" } };
const event = (seq: number, kind: string, data: Record<string, unknown>): AgentOperation => ({ seq, kind, ts: seq, data });

test("structured results become readable content without losing Markdown links", () => {
  const value = { content: [{ type: "text", text: JSON.stringify({ summary: "Sources checked", findings: ["One missing date"], sources: [{ title: "Source", url: "https://example.com/", snippet: "Original evidence" }] }) }] };
  const result = readableResult(value, false);
  expect(result).toContain("Sources checked"); expect(result).toContain("### Findings");
  expect(result).toContain("[Source](<https://example.com/>)"); expect(result).toContain("Original evidence");
  expect(result).not.toContain('"summary"'); expect(result).not.toContain("[object Object]");
  expect(readableResult("[Source](https://example.com)", false)).toBe("[Source](https://example.com)");
  expect(readableResult({ summary: "保留原主题", findings: ["正文展示"] }, true)).toContain("### 发现");
  expect(contentValue({ summary: "Keep me", content: [] })).toEqual({ summary: "Keep me", content: [] });
  expect(safeLink("javascript:alert(1)")).toBe("");
  expect(readableResult({ summary: "Keep the business summary", data: { findings: ["Nested evidence"] } }, false)).toContain("Keep the business summary");
  expect(readableResult({ summary: "Keep the business summary", data: { findings: ["Nested evidence"] } }, false)).toContain("Nested evidence");
});

test("pair tool calls only with their own attempt and retain request evidence", () => {
  const steps = pairOperations([
    event(1, "tool_use", { attempt: 1, call_id: "same", action: "read_file", payload: { path: "one.md" } }),
    event(2, "tool_use", { attempt: 2, call_id: "same", action: "read_file", payload: { path: "two.md" } }),
    event(3, "tool_result", { attempt: 2, call_id: "same", ok: true, result: { content: "Two" } }),
    event(4, "tool_result", { attempt: 1, call_id: "same", ok: false, error: "Missing file" }),
  ]);
  expect(steps).toHaveLength(2); expect(steps[0].data.payload).toEqual({ path: "one.md" });
  expect(steps[1].data.result).toEqual({ content: "Two" });
  expect(toolPresentation(steps[0], "completed", false).state).toBe("Failed");
  expect(toolPresentation(steps[1], "completed", false).state).toBe("Done");
});

test("a missing response or nonzero command exit is not labelled successful", () => {
  const pending = pairOperations([event(1, "tool_use", { action: "run_shell", payload: { command: "test" } })])[0];
  expect(toolPresentation(pending, "running", false).pending).toBe(true);
  expect(toolPresentation(pending, "interrupted", false).state).toBe("No response recorded");
  const returned = pairOperations([event(1, "tool_result", { action: "run_shell", ok: true, result: { exit_code: 1, stderr: "Failed" } })])[0];
  expect(toolPresentation(returned, "completed", false).failed).toBe(true);
  const nestedFailure = pairOperations([event(1, "tool_result", { action: "custom_tool", ok: true, result: { ok: false, error: "Permission denied" } })])[0];
  expect(toolPresentation(nestedFailure, "completed", false).state).toBe("Failed");
});

test("resuming preserves the previous result but never fabricates a new one", () => {
  const detail: AgentDetail = { ok: true, agent: row, has_more: false, messages: [], events: [
    event(1, "instruction", { attempt: 1, text: "First task" }), event(2, "completed", { attempt: 1, output: row.output }),
    event(3, "instruction", { attempt: 2, text: "Follow-up task" }),
  ] };
  const runs = agentRuns(row, detail);
  expect(runs).toHaveLength(2); expect(runs[0].output).toEqual(row.output);
  expect(runs[1].instruction).toBe("Follow-up task"); expect(runs[1].output).toBeUndefined();
  expect(agentRuns({ ...row, state: "failed", error: "Specific error" }, detail)[1].error).toBe("Specific error");
});
