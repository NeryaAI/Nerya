import { test, expect } from "@playwright/test";
import { parseRecordedDiff, workbenchFiles, workbenchTimeline, type WorkbenchResource } from "../../lib/canvasWorkbench";
import type { ChatThread, NativeBlockEnvelope } from "../../lib/chat";
const thread = (blocks: NativeBlockEnvelope[], loading = false): ChatThread => ({ id: "s", title: "Review", created_ts: 1, updated_ts: 2, messages: [{ id: "a", role: "assistant", ts: 2, loading, turn: { blocks } }] });

test("diff counts exclude headers and line numbers follow hunks", () => {
  const diff = parseRecordedDiff("--- a/a.md\n+++ b/a.md\n@@ -10,2 +20,3 @@\n context\n-old\n+new\n+extra\n\\ No newline at end of file");
  expect(diff.additions).toBe(2); expect(diff.deletions).toBe(1);
  expect(diff.lines[3]).toMatchObject({ old: 10, next: 20, kind: "context" });
  expect(diff.lines[4]).toMatchObject({ old: 11, kind: "remove" });
  expect(diff.lines[5]).toMatchObject({ next: 21, kind: "add" });
  expect(parseRecordedDiff("-one\n+two").lines[1].next).toBeUndefined();
});

test("timeline deduplicates delivery and excludes hidden and final text", () => {
  const call: NativeBlockEnvelope = { block: { kind: "tool_use", call_id: "r", action: "read_file", payload: { path: "a.md" } } };
  const reply: NativeBlockEnvelope = { block: { kind: "tool_result", call_id: "r", ok: true, result: { content: "Verified text" } } };
  const events = workbenchTimeline(thread([call, reply, call, reply, { block: { kind: "thinking", text: "Private reasoning" } }, { block: { kind: "text", text: "Final" } }]), false);
  expect(events).toHaveLength(1); expect(events[0]).toMatchObject({ title: "Read", subject: "a.md", state: "Done", failed: false });
  expect(events[0].detail).toContain("Verified text"); expect(JSON.stringify(events)).not.toContain("Private reasoning");
});

test("missing tool responses and failed commands are never marked complete", () => {
  const call: NativeBlockEnvelope = { block: { kind: "tool_use", call_id: "c", action: "run_shell", payload: { command: "check" } } };
  expect(workbenchTimeline(thread([call], true), false)[0].pending).toBe(true);
  expect(workbenchTimeline(thread([call]), false)[0].state).toBe("No response recorded");
  const result: NativeBlockEnvelope = { block: { kind: "tool_result", call_id: "c", ok: true, result: { exit_code: 1, stderr: "Failed check" } } };
  expect(workbenchTimeline(thread([call, result]), false)[0].failed).toBe(true);
});

test("file review groups versions by real path and preserves read-only distinction", () => {
  const file: WorkbenchResource = { id: "read", kind: "file", title: "a.md", path: "docs/a.md", body: "old", seenAt: 1, operation: "read_file" };
  const edit = { ...file, id: "edit", kind: "diff", body: "-old\n+new", seenAt: 2, operation: "edit_file" };
  const files = workbenchFiles([edit, file, { ...file, id: "other", path: "notes/a.md" }]);
  expect(files).toHaveLength(2); expect(files[0].changed).toBe(true); expect(files[0].versions).toHaveLength(2);
  expect(files[0].diff?.id).toBe("edit"); expect(files[1].status).toBe("read"); expect(files[1].changed).toBe(false);
  expect(workbenchFiles([])).toEqual([]); expect(workbenchTimeline(null, true)).toEqual([]);
});
