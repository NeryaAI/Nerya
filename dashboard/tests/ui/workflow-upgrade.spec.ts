import { test, expect } from "@playwright/test";
import { workflowUpgradeFixture, captureWorkflow } from "./workflow-upgrade-fixture";
import { chooseOption } from "./choice-control";

test("automation edits the effective goal, saves a disabled task, and validates without executing", async ({ page }, info) => {
  const fixture = await workflowUpgradeFixture(page);
  await page.goto("/workflows");
  const detail = page.getByTestId("automation-task-detail");
  await expect(detail).toContainText("Actual effective instruction");
  await detail.getByRole("button", { name: "Edit", exact: true }).click();
  const editor = page.getByTestId("automation-task-editor");
  await expect(editor.getByRole("textbox", { name: "What should this task do?", exact: true })).toHaveValue("Actual effective instruction");
  await editor.getByRole("textbox", { name: "What should this task do?", exact: true }).fill("Updated actual task instruction");
  await editor.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(detail).toContainText("Updated actual task instruction");
  expect(fixture.writes[0].body.payload.prompt).toBe("Updated actual task instruction");
  await page.getByRole("button", { name: "Create task", exact: true }).first().click();
  await editor.getByLabel("Task name", { exact: true }).fill("Timezone test task");
  await editor.getByLabel("What should this task do?", { exact: true }).fill("Inspect data without external actions.");
  await editor.getByRole("combobox", { name: "When to run", exact: true }).selectOption("once");
  await editor.getByRole("combobox", { name: "Time zone", exact: true }).fill("Asia/Shanghai");
  await editor.getByLabel("Date and time", { exact: true }).fill("2027-01-02T09:00");
  await editor.getByRole("button", { name: "Validate and preview", exact: true }).click();
  await expect(editor).toContainText("Next scheduled times");
  await expect(editor.getByRole("checkbox", { name: /Enable future scheduled/ })).not.toBeChecked();
  await expect(page.getByText("Saved report", { exact: true })).toHaveCount(0, { timeout: 10000 });
  await captureWorkflow(page, info, "automation-editor-desktop.png", 1360);
  await editor.getByRole("button", { name: "Save as paused", exact: true }).click();
  await expect(detail).toContainText("Timezone test task");
  const saved = fixture.writes.find(write => write.path === "/triggers/schedules/add")!;
  expect(saved.body.enabled).toBe(false); expect(saved.body.run_at).toBe("2027-01-02T01:00:00Z");
  expect(fixture.writes.every(write => ["/triggers/schedules/add", "/triggers/schedules/update"].includes(write.path))).toBeTruthy();
  expect(fixture.errors).toEqual([]);
});

test("failed schedule reads retain rows, recover automatically, and filters keep the correct selection", async ({ page }) => {
  const fixture = await workflowUpgradeFixture(page);
  await page.goto("/workflows");
  const detail = page.getByTestId("automation-task-detail");
  await expect(detail).toHaveAttribute("data-task-id", "report");
  fixture.setScheduleFailure(true);
  await page.getByRole("button", { name: "Refresh", exact: true }).first().click();
  await expect(page.getByTestId("resource-read-error")).toContainText("last successful snapshot");
  await expect(detail).toHaveAttribute("data-task-id", "report");
  fixture.setScheduleFailure(false);
  await expect(page.getByTestId("resource-read-error")).toHaveCount(0, { timeout: 15_000 });
  await page.getByRole("combobox", { name: "All", exact: true }).selectOption("paused");
  await expect(detail).toHaveAttribute("data-task-id", "paused");
  expect(fixture.writes).toEqual([]); expect(fixture.errors).toEqual([]);
});

test("run history opens in place, keeps old identity, drains terminal event pages and evicts stale statuses", async ({ page }, info) => {
  const fixture = await workflowUpgradeFixture(page);
  await page.goto("/workflows");
  await page.getByRole("tab", { name: "Run history", exact: true }).click();
  const history = page.getByTestId("task-run-history");
  await expect(history.locator("tbody tr")).toHaveCount(2);
  await history.locator("tbody tr").last().getByRole("button").click();
  await expect(page.getByTestId("task-run-detail")).toHaveAttribute("data-run-id", "run-older");
  await expect(page.getByTestId("run-workspace")).toContainText("Recorded result run-older");
  await expect.poll(() => fixture.eventReads.some(cursor => cursor >= 5)).toBeTruthy();
  fixture.setRunFailure(true);
  await page.getByTestId("run-workspace").getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByTestId("task-run-detail").getByTestId("resource-read-error")).toBeVisible();
  await expect(page.getByTestId("run-workspace")).toContainText("Recorded result run-older");
  fixture.setRunFailure(false);
  await expect(page.getByTestId("task-run-detail").getByTestId("resource-read-error")).toHaveCount(0, { timeout: 15_000 });
  await page.screenshot({ path: info.outputPath("run-result-desktop.png"), fullPage: true });
  fixture.runs[0].execution_status = "running";
  await history.getByRole("combobox").selectOption("running");
  await expect(history.locator("tbody tr")).toHaveCount(1);
  fixture.runs[0].execution_status = "succeeded";
  await history.getByRole("button", { name: "Refresh", exact: true }).first().click();
  await expect(history.locator("tbody tr")).toHaveCount(0);
  expect(fixture.writes).toEqual([]); expect(fixture.errors).toEqual([]);
});

test("strategy compact flow, node side panel and personal layout do not create candidates", async ({ page }, info) => {
  const fixture = await workflowUpgradeFixture(page);
  await page.goto("/strategies?strategy_id=alpha&proposal_id=prp_fixture");
  const compact = page.getByTestId("compact-workflow");
  await expect(compact).toBeVisible();
  await expect(compact.locator("li")).toHaveCount(3);
  await compact.getByRole("button").nth(1).click();
  await expect(page.getByTestId("workflow-editor-dialog")).toHaveAttribute("data-side-panel", "true");
  await page.keyboard.press("Escape");
  await chooseOption(page.getByRole("combobox", { name: "Display mode" }), "canvas");
  const grip = page.getByRole("button", { name: "Move main.py", exact: true });
  await grip.focus(); await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("button", { name: "Reset layout", exact: true })).toBeVisible();
  expect(fixture.writes).toEqual([]);
  await page.screenshot({ path: info.outputPath("strategy-canvas-layout.png"), fullPage: true });
  await chooseOption(page.getByRole("combobox", { name: "Display mode" }), "auto");
  await page.getByRole("tab", { name: "Review", exact: true }).click();
  await expect(page.getByTestId("review-plan-panel")).toBeVisible();
  await expect(compact.locator("li")).toHaveCount(2);
  await captureWorkflow(page, info, "review-plan-desktop.png");
  expect(fixture.errors).toEqual([]);
});

test("historical review retains frozen evidence across a failed refresh", async ({ page }, info) => {
  const fixture = await workflowUpgradeFixture(page);
  await page.goto("/strategies?strategy_id=alpha&proposal_id=prp_fixture&workflow_log=evolution&workflow_run=old");
  const activity = page.getByTestId("workflow-review-activity");
  await expect(activity).toContainText("Review old: keep the strategy unchanged");
  fixture.setReviewFailure(true);
  await activity.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(activity.getByTestId("resource-read-error")).toContainText("last successful snapshot");
  await expect(activity).toContainText("Review old: keep the strategy unchanged");
  fixture.setReviewFailure(false);
  await expect(activity.getByTestId("resource-read-error")).toHaveCount(0, { timeout: 20_000 });
  await activity.getByRole("tab", { name: "Recorded activity", exact: true }).click();
  await expect(page.getByTestId("workflow-invocation-canvas")).toHaveAttribute("data-invocation-id", "old");
  await expect(page.getByTestId("workflow-invocation-conversation")).toContainText("Recorded conversation old");
  await page.screenshot({ path: info.outputPath("review-history-desktop.png"), fullPage: true });
  expect(fixture.writes).toEqual([]); expect(fixture.errors).toEqual([]);
});

test("Chinese narrow-screen automation editor remains readable and never runs on save preview", async ({ page }, info) => {
  const fixture = await workflowUpgradeFixture(page, "zh");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/workflows");
  await page.getByRole("button", { name: "创建任务", exact: true }).first().click();
  const editor = page.getByTestId("automation-task-editor");
  await editor.getByLabel("任务名称", { exact: true }).fill("市场观察任务");
  await editor.getByLabel("需要这个任务做什么？", { exact: true }).fill("整理带时间戳的行情证据，不下单。");
  await expect(editor.getByRole("button", { name: "保存为暂停任务", exact: true })).toBeEnabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 2)).toBeTruthy();
  await page.screenshot({ path: info.outputPath("automation-editor-mobile-zh.png"), fullPage: true });
  expect(fixture.writes).toEqual([]); expect(fixture.errors).toEqual([]);
});

test("waiting questions are handled in the exact run without leaking another turn's interaction", async ({ page }, info) => {
  const fixture = await workflowUpgradeFixture(page);
  fixture.runs[0].execution_status = "awaiting_input";
  const answers: Record<string, unknown>[] = [];
  let pending = true;
  await page.route("**/api/proxy/agent/sessions/view?**", async route => {
    await route.fulfill({ json: { ok: true, session_id: "report-session", revision: "1", observed_at: 1,
      pending_interactions: pending ? [
        { interaction_id: "current-question", session_id: "report-session", turn_id: "turn-run-latest", revision: 7, kind: "question", state: "pending", payload: { title: "Choose the observation window", message: "This belongs to the selected run." } },
        { interaction_id: "another-question", session_id: "report-session", turn_id: "other-turn", revision: 8, kind: "question", state: "pending", payload: { title: "UNRELATED TURN MUST NOT APPEAR" } },
      ] : [], queue: { count: 0, paused: false }, approvals: [], agents: [], result_refs: [], available_actions: { send: true } } });
  });
  await page.route("**/api/proxy/agent/interactions/respond", async route => {
    answers.push(route.request().postDataJSON()); pending = false;
    fixture.runs[0].execution_status = "succeeded";
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto("/workflows");
  await page.getByRole("tab", { name: "Run history", exact: true }).click();
  await page.getByTestId("task-run-history").locator("tbody tr").first().getByRole("button").click();
  const attention = page.getByTestId("run-attention");
  await expect(attention.getByTestId("user-interaction")).toHaveCount(1);
  await expect(attention).toContainText("Choose the observation window");
  await expect(attention).not.toContainText("UNRELATED TURN");
  await attention.locator("textarea").fill("Use the last seven completed days.");
  await page.screenshot({ path: info.outputPath("run-question-in-place.png"), fullPage: true });
  await attention.locator('button[type="submit"]').click();
  await expect(attention).toHaveCount(0);
  expect(answers).toHaveLength(1);
  expect(answers[0]).toMatchObject({ interaction_id: "current-question", session_id: "report-session", expected_revision: 7, action: "answer", text: "Use the last seven completed days." });
  expect(fixture.writes).toEqual([]); expect(fixture.errors).toEqual([]);
});

test("manual run requires confirmation and opens only the returned run identity", async ({ page }) => {
  const fixture = await workflowUpgradeFixture(page);
  await page.goto("/workflows");
  await page.getByTestId("automation-task-detail").getByRole("button", { name: "Run now", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Create a separate run", exact: true });
  await expect(dialog).toContainText("It is not a delivery retry");
  expect(fixture.writes).toEqual([]);
  await dialog.getByRole("button", { name: "Run now", exact: true }).click();
  await expect(page.getByTestId("task-run-detail")).toHaveAttribute("data-run-id", "run-latest");
  expect(fixture.writes).toHaveLength(1);
  expect(fixture.writes[0].path).toBe("/triggers/schedules/run_now");
  expect(fixture.writes[0].body.client_request_id).toBeTruthy();
  expect(fixture.errors).toEqual([]);
});

test("review suggestions open their recorded comparison in place without promoting the candidate", async ({ page }, info) => {
  const fixture = await workflowUpgradeFixture(page);
  const requests: string[] = [];
  await page.route("**/api/proxy/strategies/runtime/tuning/record?**", async route => {
    await route.fulfill({ json: { ok: true, strategy_id: "alpha", run_id: "old", record: { strategy_id: "alpha", run_id: "old", status: "ok", proposal_id: "prp_review_fixture" }, audit: { subagent_output: { summary: "Investigate execution costs", proposed_changes: [] }, conversation: [] } } });
  });
  await page.route("**/api/proxy/evolution/**", async route => {
    const path = new URL(route.request().url()).pathname;
    requests.push(path);
    if (path !== "/api/proxy/evolution/proposals/prp_review_fixture") throw new Error("Inspection must not mutate the proposal");
    await route.fulfill({ json: {
      id: "prp_review_fixture", kind: "strategy_tuning_proposal", state: "pending_review", target: "strategies/alpha", strategy_id: "alpha", summary: "Reduce signal churn",
      rationale_md: "Only the recorded window supports this candidate.", test_plan_md: "Run an independent held-out comparison before application.",
      validation: { ok: true, blockers: [] },
      backtest_comparison: { status: "complete", summary: "Recorded comparison on the same fixture window.", metrics_delta: [{ key: "trade_count", before: 12, after: 9, delta: -3, direction: "flat" }] },
      post_apply_monitor: { status: "not_applied", summary: "This candidate has not been applied. No realized outcome exists.", observations: [] },
    } });
  });
  await page.goto("/strategies?strategy_id=alpha&proposal_id=prp_fixture&workflow_log=evolution&workflow_run=old");
  await page.getByRole("button", { name: "Inspect suggested change here", exact: true }).click();
  const proposal = page.getByTestId("workflow-proposal-detail");
  await expect(proposal).toContainText("Reduce signal churn");
  await expect(proposal.getByTestId("review-backtest-comparison").getByRole("cell", { name: "12", exact: true })).toBeVisible();
  await expect(proposal.getByTestId("review-post-apply")).toContainText("has not been applied");
  await page.screenshot({ path: info.outputPath("review-comparison-in-place.png"), fullPage: true });
  expect(requests.every(path => path === "/api/proxy/evolution/proposals/prp_review_fixture")).toBe(true);
  expect(fixture.writes).toEqual([]); expect(fixture.errors).toEqual([]);
});

test("ambiguous local time reveals an explicit occurrence choice and preserves the chosen UTC instant", async ({ page }) => {
  const fixture = await workflowUpgradeFixture(page);
  await page.route("**/api/proxy/triggers/schedules/preview", async route => {
    const input = route.request().postDataJSON().schedule;
    if (input.fold === undefined) {
      await route.fulfill({ status: 400, json: { ok: false, error: "ambiguous_local_time" } }); return;
    }
    await route.fulfill({ json: { ok: true, read_only: true, schedule: { cron: null, every_seconds: null, run_at: "2026-11-01T06:30:00Z", timezone: "America/New_York" }, occurrences: [{ utc: "2026-11-01T06:30:00Z", local: "2026-11-01T01:30:00-05:00" }], interval_unanchored: false } });
  });
  await page.goto("/workflows");
  await page.getByRole("button", { name: "Create task", exact: true }).first().click();
  const editor = page.getByTestId("automation-task-editor");
  await editor.getByRole("textbox", { name: "Task name", exact: true }).fill("Repeated hour review");
  await editor.getByRole("textbox", { name: "What should this task do?", exact: true }).fill("Observe without trading.");
  await editor.getByRole("combobox", { name: "When to run", exact: true }).selectOption("once");
  await editor.getByRole("combobox", { name: "Time zone", exact: true }).fill("America/New_York");
  await editor.getByLabel("Date and time", { exact: true }).fill("2026-11-01T01:30");
  await editor.getByRole("button", { name: "Validate and preview", exact: true }).click();
  await expect(editor.getByRole("alert")).toContainText("occurs twice");
  await editor.getByRole("combobox", { name: "Repeated-hour handling", exact: true }).selectOption("1");
  await editor.getByRole("button", { name: "Save as paused", exact: true }).click();
  await expect(page.getByTestId("automation-task-detail")).toContainText("1:30:00");
  const saved = fixture.writes.find(write => write.path === "/triggers/schedules/add")!;
  expect(saved.body.run_at).toBe("2026-11-01T06:30:00Z");
  expect(saved.body.enabled).toBe(false);
  expect(fixture.errors).toEqual([]);
});
