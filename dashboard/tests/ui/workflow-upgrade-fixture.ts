import type { Page, Route, TestInfo } from "@playwright/test";
import { parityFixture } from "./command-fixture";
import type { TriggerSchedule } from "../../lib/clientApi";
import type { TaskRun } from "../../lib/taskRuns";
import type { WorkflowView, WorkflowNode } from "../../lib/workflowTypes";

const fixtureTitles: Record<string, string> = { "strategy:alpha": "Alpha workflow", "scheduler:trading": "Trading schedule", "script:main.py": "main.py", "agent:runtime": "Strategy Agent", "evidence:review": "Run evidence", "scheduler:tuning": "Review schedule", "agent:tuner": "strategy_tuner", "proposal:tuning": "Change proposal", "validation:tuning": "Validation & replay" };
const node = (id: string, kind: WorkflowNode["kind"], config: unknown, binding: WorkflowNode["binding"], x = 0): WorkflowNode => ({ id, kind, config, binding, title: fixtureTitles[id] || id, subtitle: id, resource: binding.file || id, editable: true, position: { x, y: 80 } });

export async function captureWorkflow(page: Page, info: TestInfo, filename: string, height = 1050) {
  const before = page.viewportSize();
  if (before && before.width > 600) await page.setViewportSize({ width: before.width, height });
  // The app scrolls its workspace, not document.body. Capture the top of that
  // real surface instead of accidentally clipping the title after form focus.
  await page.evaluate(() => {
    for (const root of document.querySelectorAll<HTMLElement>('[data-testid="automation-task-editor"],[data-testid="strategy-workflow-panel"],#main-content')) {
      for (let element: HTMLElement | null = root; element; element = element.parentElement) element.scrollTop = 0;
    }
    for (const element of document.querySelectorAll<HTMLElement>("main,main *")) {
      if (element.scrollHeight > element.clientHeight && ["auto", "scroll"].includes(getComputedStyle(element).overflowY)) element.scrollTop = 0;
    }
    document.querySelector("main")?.scrollTo(0, 0);
    window.scrollTo(0, 0);
  });
  await page.screenshot({ path: info.outputPath(filename), fullPage: true });
  if (before) await page.setViewportSize(before);
}
export function workflowFixture(): WorkflowView {
  const config = { enabled: false, review_plan: { scope: "alpha · paper", focus: "Review evidence and execution costs", validation_plan: "Compare the same held-out window", next_review: "Wait for sufficient closed trades" }, objectives: ["risk_adjusted_return"], tuning_prompt: "", proposal_policy: { allowed_targets: ["main.py", "strategy.yml"], forbidden_targets: ["accounts/*", "secrets/*"] } };
  const nodes = [node("strategy:alpha", "strategy", { title: "Alpha workflow" }, { file: null, path: [] }),
    node("scheduler:trading", "scheduler", { type: "interval", every_seconds: 300, enabled: false, timezone: "Asia/Shanghai" }, { file: null, path: ["schedule"] }),
    { ...node("script:main.py", "script", {}, { file: "main.py", path: null }, 320), content: "# @nerya.title Collect closed candles\n# @nerya.description Read timestamped observations and publish the signal.\n# @nerya.input Configured market\n# @nerya.output Observations\ndef run(ctx):\n    return ctx.result.hold(reason='Fixture, no trading')\n" },
    node("agent:runtime", "agent", { agent_profile: { title: "Decision Agent", role: "Explain the observed signal without placing orders." }, agent_execution: { capabilities: "inherit" } }, { file: null, path: ["$agent"] }, 640)];
  const evolutionNodes = [node("evidence:review", "script", { runs: 200, max_age_hours: 168, min_closed_trades: 10 }, { file: null, path: ["tuning", "lookback"] }),
    node("scheduler:tuning", "scheduler", { type: "cron", cron: "0 9 * * 1", enabled: false, timezone: "Asia/Shanghai" }, { file: null, path: ["tuning", "schedule"] }),
    { ...node("agent:tuner", "agent", { name: "strategy_tuner" }, { file: "subagents/strategy_tuner.agent.md", path: null }, 320), content: "Review only attributable evidence. No change is a valid outcome." },
    node("proposal:tuning", "proposal", config, { file: null, path: ["$tuning"] }),
    node("validation:tuning", "validation", { require_backtest: true, require_operator_approval: true }, { file: null, path: ["tuning", "guardrails"] })];
  const edge = (source: string, target: string) => ({ id: `${source}-${target}`, source, target, origin: "manifest" as const, relation: "dispatch", label: "" });
  return { ok: true, strategy_id: "alpha", revision: "fixture-version-one", legacy: false, can_edit: true,
    manifest: { strategy_id: "alpha", title: "Alpha workflow", mode: "paper", tuning: config },
    source: { proposal_id: "prp_fixture", state: "draft", omitted_files: [] }, metadata: { version: 1, nodes: {}, edges: [] },
    strategy: { id: "strategy", nodes, edges: [edge("scheduler:trading", "script:main.py"), edge("script:main.py", "agent:runtime")] },
    evolution: { id: "evolution", nodes: evolutionNodes, edges: [edge("evidence:review", "agent:tuner")] } };
}

export async function workflowUpgradeFixture(page: Page, language = "en") {
  const base = await parityFixture(page, { language });
  const writes: Array<{ path: string; body: any }> = [];
  const schedules: TriggerSchedule[] = [
    { id: "report", kind: "agent.task", enabled: true, session_kind: "agent", session_mode: "reuse", session_id: "report-session", cron: "0 9 * * *", timezone: "Asia/Shanghai", payload: { title: "Morning review", source_request: "Old original request", prompt: "Actual effective instruction" }, delivery_targets: [], execution: {} },
    { id: "paused", kind: "script.task", enabled: false, session_kind: "script", every_seconds: 3600, timezone: "UTC", payload: { title: "Paused archive", script_id: "fixture_script", args: {} } },
  ];
  const stamp = Date.parse("2026-10-05T00:00:00Z") / 1000;
  const makeRun = (id: string, index: number): TaskRun => ({ run_id: id, task_kind: "scheduled_agent", task_id: "report", session_id: "report-session", source_revision: "version-one", turn_id: `turn-${id}`,
    admission_status: "accepted", execution_status: "succeeded", business_status: "reported", delivery_status: "not_requested", trigger_kind: "schedule", created_at: stamp - index * 60, updated_at: stamp, revision: 1,
    snapshot: { title: "Morning review", mode: "analysis" }, result: { final_text: `Recorded result ${id}. No external action occurred.` } });
  const runs = [makeRun("run-latest", 0), makeRun("run-older", 1)];
  const workflow = workflowFixture();
  let failSchedules = false, failReview = false, failRun = false;
  const eventReads: number[] = [];
  await page.route("**/api/proxy/**", async (route: Route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace(/^\/api\/proxy/, "");
    const input = request.method() === "POST" ? request.postDataJSON() : null;
    let result: unknown;
    if (path.startsWith("/triggers/")) {
      if (path === "/triggers/schedules" && request.method() === "GET") {
        if (failSchedules) { await route.fulfill({ status: 503, json: { ok: false, error: "Temporary schedule read error" } }); return; }
        result = { schedules };
      } else if (path === "/triggers/schedules/status") result = { ok: true, schedules: schedules.map(schedule => ({ id: schedule.id, next_due_at: stamp + 3600, last_fired_ts: stamp })) };
      else if (path === "/triggers/routes") result = { routes: [{ id: "fixture_event", match: { kind: "price.changed" }, action: { target: "agent" }, enabled: true }] };
      else if (path === "/triggers/schedules/preview") result = { ok: true, read_only: true, schedule: { cron: input.schedule.cron, every_seconds: input.schedule.every_seconds, run_at: input.schedule.wall_time ? "2027-01-02T01:00:00Z" : null, timezone: input.schedule.timezone }, occurrences: [{ utc: "2027-01-02T01:00:00Z", local: "2027-01-02T09:00:00+08:00" }], interval_unanchored: false };
      else if (request.method() === "POST") {
        writes.push({ path, body: input });
        if (path === "/triggers/schedules/update") { const index = schedules.findIndex(schedule => schedule.id === input.id); schedules[index] = { ...schedules[index], ...input }; }
        if (path === "/triggers/schedules/add") schedules.push(input);
        result = { ok: true, run_id: "run-latest", session_id: "report-session" };
      }
    } else if (path === "/agent/runs") {
      const state = url.searchParams.get("state");
      result = { ok: true, runs: runs.filter(run => !state || run.execution_status === state), next_cursor: null };
    } else if (path.startsWith("/agent/runs/")) {
      const id = path.split("/")[3];
      if (failRun) { await route.fulfill({ status: 503, json: { ok: false, error: "Temporary run read error" } }); return; }
      if (path.endsWith("/events")) {
        const after = Number(url.searchParams.get("after_seq") || 0); eventReads.push(after);
        result = { ok: true, events: after < 6 ? [{ kind: "tool.result", seq: after + 1, tool_name: "read_file", call_id: `call-${after}`, result: `Recorded page ${after + 1}` }] : [], next_seq: Math.min(after + 1, 6), has_more: after < 5 };
      } else result = { ok: true, run: runs.find(run => run.run_id === id) || runs[0] };
    } else if (path.startsWith("/financial/")) {
      if (request.method() !== "GET") { writes.push({ path, body: input }); result = { ok: false, error: "Fixture never approves money" }; }
      else result = { ok: true, task: { task_kind: "scheduled_agent", task_id: "report", source_revision: "version-one", security_revision: "security-one" }, grants: [] };
    } else if (path === "/strategies/runtime/workflows") result = { ok: true, workflows: [{ key: "alpha", strategy_id: "alpha", proposal_id: "prp_fixture", title: "Alpha workflow", mode: "paper", status: "draft", state: "draft", counts: { script: 1, agent: 1 } }] };
    else if (path === "/strategies/runtime/workflow") result = workflow;
    else if (path === "/strategies/runtime/workflow/propose") { writes.push({ path, body: input }); result = { ok: true, workflow: { ...workflow, revision: "fixture-version-two" } }; }
    else if (path === "/strategies/runtime/tuning/history") result = { ok: true, strategy_id: "alpha", runs: ["new", "old"].map(id => ({ run_id: id, status: "ok", started_at: `2026-10-0${id === "new" ? 5 : 4}T00:00:00Z`, reason: `Review ${id}` })), has_more: false };
    else if (path === "/strategies/runtime/tuning/record") {
      if (failReview) { await route.fulfill({ status: 503, json: { ok: false, error: "Temporary review read error" } }); return; }
      const id = url.searchParams.get("run_id");
      result = { ok: true, strategy_id: "alpha", run_id: id, record: { run_id: id, strategy_id: "alpha", status: "ok", snapshot: { evidence_scope: { package_hash: "frozen-v1", window_started_at: "2026-09-28T00:00:00Z", window_ended_at: "2026-10-05T00:00:00Z", selected_run_ids: ["script-1"], selected_agent_task_ids: [], excluded_run_counts: { lookback_limit: 2 } } } }, audit: { subagent_output: { summary: `Review ${id}: keep the strategy unchanged`, proposed_changes: [], validation_plan: "Observe more samples" }, conversation: [{ kind: "text", text: `Recorded conversation ${id}` }] } };
    }
    if (result === undefined) { await route.fallback(); return; }
    await route.fulfill({ json: result });
  });
  return { ...base, writes, schedules, runs, eventReads, workflow,
    setScheduleFailure: (value: boolean) => { failSchedules = value; }, setReviewFailure: (value: boolean) => { failReview = value; }, setRunFailure: (value: boolean) => { failRun = value; } };
}
