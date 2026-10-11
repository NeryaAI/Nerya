import { test, expect } from "@playwright/test";
import { blankDraft, buildDraftFromSchedule, buildSchedulePayload, isPaused, cadenceFor } from "../../lib/automationDraft";
import { describeCron } from "../../lib/format";
import { wallTimeInZone, schedulePreviewPayload } from "../../lib/scheduleEditor";
import { linearWorkflow, validPositions } from "../../lib/workflowLayout";
import { loadRunEvidence } from "../../lib/runEvidence";
import { taskRunApi, type TaskRun } from "../../lib/taskRuns";
import type { TriggerSchedule } from "../../lib/clientApi";
import type { WorkflowGraph, WorkflowNode } from "../../lib/workflowTypes";

const translate = (key: string) => key;
const original: TriggerSchedule = {
  id: "scheduled-report", kind: "agent.task", enabled: true, session_kind: "agent", session_mode: "reuse", session_id: "old-session",
  cron: "0 9 * * *", timezone: "Asia/Shanghai", anchor_at: 1770000000,
  starts_at: "2026-01-01T00:00:00Z", ends_at: "2027-01-01T00:00:00Z",
  payload: { title: "Actual task", source_request: "Old request", prompt: "Actual instruction", custom_evidence: { revision: "kept" } },
  delivery_targets: [{ kind: "gateway", platform: "telegram", channel: "qa-channel", chat_id: "qa-recipient" } as never, { kind: "webhook", url: "https://example.invalid/fixture" }],
  execution: { required_files: ["fixture.md"], custom_flag: "preserved" },
};

test("new task definitions are paused and the edited goal is the actual instruction", () => {
  expect(blankDraft().enabled).toBe(false);
  expect(isPaused({ ...original, enabled: false, paused: false })).toBe(true);
  const draft = buildDraftFromSchedule(original);
  expect(draft.sourceRequest).toBe("Actual instruction");
  expect(draft.originalSourceRequest).toBe("Old request");
  draft.sourceRequest = "New verified instruction";
  const saved = buildSchedulePayload(draft, translate);
  expect(saved.payload?.prompt).toBe("New verified instruction");
  expect(saved.payload?.custom_evidence).toEqual({ revision: "kept" });
  expect(saved.delivery_targets).toEqual(original.delivery_targets);
  expect(saved.execution).toEqual(original.execution);
  expect(saved.starts_at).toBe(original.starts_at);
  expect(saved.ends_at).toBe(original.ends_at);
  expect(saved.anchor_at).toBe(original.anchor_at);
});

test("prompt override is explicit and stale overrides cannot be saved", () => {
  const draft = { ...buildDraftFromSchedule(original), generatedPrompt: "Explicit override" };
  expect(buildSchedulePayload(draft, translate).payload?.prompt).toBe("Actual instruction");
  draft.usePromptOverride = true;
  expect(buildSchedulePayload(draft, translate).payload?.prompt).toBe("Explicit override");
  const reopened = buildDraftFromSchedule(buildSchedulePayload(draft, translate));
  expect(reopened.usePromptOverride).toBe(true);
  expect(reopened.sourceRequest).toBe("Actual instruction");
  expect(reopened.generatedPrompt).toBe("Explicit override");
  draft.overrideAcknowledged = false;
  expect(() => buildSchedulePayload(draft, translate)).toThrow(/override/);
});

test("clearing delivery and switching session mode clears saved bindings", () => {
  const draft = buildDraftFromSchedule(original);
  draft.deliveryEdited = true; draft.deliveryKind = "none"; draft.sessionMode = "ephemeral";
  const saved = buildSchedulePayload(draft, translate);
  expect(saved.delivery_targets).toEqual([]);
  expect(saved.session_id).toBeNull(); expect(saved.session_ids).toEqual([]);
});

test("wall-time editing uses the schedule's time zone and requires server resolution", () => {
  expect(wallTimeInZone("2026-10-06T01:00:00Z", "Asia/Shanghai")).toBe("2026-10-06T09:00");
  const draft = buildDraftFromSchedule({ ...original, cron: null, run_at: "2026-10-06T01:00:00Z" });
  expect(draft.runAt).toBe("2026-10-06T09:00");
  expect(schedulePreviewPayload(draft).wall_time).toBe("2026-10-06T09:00");
  expect(schedulePreviewPayload(draft).timezone).toBe("Asia/Shanghai");
  expect(cadenceFor({ ...original, cron: null, run_at: "2026-10-06T01:00:00Z" }, "en-GB")).toContain("09:00:00");
  expect(describeCron("0 9 * * 1-5")?.key).toBe("weekdaysAt");
  expect(describeCron("0 9 * * 1")?.params).toEqual({ time: "09:00", weekday: "1" });
  const unsolved = { ...draft, runAtInstant: undefined };
  expect(() => buildSchedulePayload(unsolved, translate)).toThrow();
});

test("compact flow never turns branching, disconnection or notes into a false chain", () => {
  const node = (id: string): WorkflowNode => ({ id, kind: "script", title: id, resource: id, subtitle: "", config: {}, binding: { file: id, path: null }, editable: true, position: { x: 0, y: 0 } });
  const graph: WorkflowGraph = { id: "graph", nodes: [node("a"), node("b"), node("c")], edges: [{ id: "ab", source: "a", target: "b", origin: "static", relation: "call", label: "" }, { id: "bc", source: "b", target: "c", origin: "static", relation: "call", label: "" }] };
  expect(linearWorkflow(graph)?.map(item => item.id)).toEqual(["a", "b", "c"]);
  expect(linearWorkflow({ ...graph, edges: graph.edges.slice(0, 1) })).toBeNull();
  expect(linearWorkflow({ ...graph, edges: [...graph.edges, { ...graph.edges[0], id: "ac", target: "c" }] })).toBeNull();
  expect(linearWorkflow({ ...graph, edges: graph.edges.map(edge => ({ ...edge, origin: "annotation" })) })).toBeNull();
  expect(validPositions({ safe: { x: 10, y: 20 }, bad: { x: Infinity, y: 0 }, wrong: "0" })).toEqual({ safe: { x: 10, y: 20 } });
});

test("terminal run events drain all pages without truncation or duplicates", async () => {
  const get = taskRunApi.get, events = taskRunApi.events;
  try {
    taskRunApi.get = async () => ({ ok: true, run: { run_id: "run-fixture", execution_status: "succeeded" } as TaskRun });
    taskRunApi.events = async (_id, after = 0) => ({ ok: true, events: [{ seq: after + 1, kind: "tool.result", text: "Recorded fixture" }], next_seq: after + 1, has_more: after < 6 });
    let result = await loadRunEvidence("run-fixture", new AbortController().signal);
    expect(result.events).toHaveLength(4); expect(result.hasMore).toBe(true);
    result = await loadRunEvidence("run-fixture", new AbortController().signal, result);
    expect(result.events).toHaveLength(7); expect(result.hasMore).toBe(false);
    taskRunApi.events = async (_id, after = 0) => ({ ok: true, events: [], next_seq: after, has_more: true });
    await expect(loadRunEvidence("run-fixture", new AbortController().signal)).rejects.toThrow(/cursor/);
  } finally { taskRunApi.get = get; taskRunApi.events = events; }
});
